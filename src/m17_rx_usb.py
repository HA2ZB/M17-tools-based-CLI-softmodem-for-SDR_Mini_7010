"""Mini native USB receive-only CLI; no PTT or transmitter path."""
import argparse
import math
from collections import deque
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
import wave
import numpy as np
from scipy import signal
from m17_rx_dsp import FMReceiver, Channelizer


class LiveAcquisition:
    """Bounded one-second history, quarter-second checks, full history replay."""
    def __init__(self, rate, offset, threshold=0.25):
        self.rate, self.offset, self.threshold = rate, offset, threshold
        self.blocks = deque()
        self.samples = self.since = 0
        self.channel = Channelizer(rate, offset, 'IQ')
        self.sos = signal.butter(8, 10000, fs=96000, output='sos')
        self.state = np.zeros((4, 2), complex)
        self.previous = None
        self.histogram = np.zeros(24001)
        self.input_energy = self.channel_energy = 0.0

    def push(self, block):
        self.blocks.append(block.copy())
        self.samples += len(block)
        self.since += len(block)
        self.input_energy += float(np.sum(block.real**2+block.imag**2))*96000/self.rate
        y = self.channel.process(block)
        if len(y):
            y, self.state = signal.sosfilt(self.sos, y, zi=self.state)
            power = np.abs(y)**2
            self.channel_energy += float(power.sum())
            previous = y[0] if self.previous is None else self.previous
            hz = np.angle(y*np.conj(np.r_[previous, y[:-1]]))*96000/(2*np.pi)
            self.previous = y[-1]
            bins = np.rint(hz).astype(int)+12000
            valid = (bins >= 0) & (bins < 24001)
            self.histogram += np.bincount(bins[valid], weights=power[valid], minlength=24001)
        while self.samples > self.rate:
            excess = self.samples-self.rate
            first = self.blocks.popleft()
            cut = min(excess, len(first))
            self.samples -= cut
            if cut < len(first):
                self.blocks.appendleft(first[cut:].copy())
        if self.since < self.rate//4:
            return None
        self.since = 0
        h = self.histogram
        total = float(h.sum())
        cfo = float(np.searchsorted(np.cumsum(h), total/2)-12000)
        detected = (self.input_energy > 0 and total > 0 and abs(cfo) <= 5000 and
                    self.channel_energy/self.input_energy >= self.threshold)
        self.histogram = np.zeros(24001)
        self.input_energy = self.channel_energy = 0.0
        if not detected:
            return None
        blocks = list(self.blocks)
        self.blocks.clear()
        return cfo, blocks


class CallbackAudio:
    """Nonblocking DAC callback with bounded backpressure in decoder drain only."""
    def __init__(self, device):
        import sounddevice as sd
        matches = [i for i, d in enumerate(sd.query_devices())
                   if d['max_output_channels'] and
                   (str(i) == device if device.isdecimal() else device.casefold() in d['name'].casefold())]
        if len(matches) != 1:
            raise ValueError(f'Audio selector must match one output, found {matches}; use --list-audio')
        self.pending = bytearray()
        self.remainder = bytearray()
        self.queue = queue.Queue(maxsize=50)  # 1 s of fixed 20 ms PCM blocks
        self.error = None
        def callback(outdata, frames, timing, status):
            if status:
                self.error = f'Audio callback: {status}'
            while len(self.pending) < frames*2:
                try:
                    self.pending.extend(self.queue.get_nowait())
                except queue.Empty:
                    break
            n = min(len(self.pending), frames*2)
            outdata[:] = bytes(self.pending[:n])+bytes(frames*2-n)
            del self.pending[:n]
        self.stream = sd.RawOutputStream(device=matches[0], samplerate=8000, channels=1,
                                         dtype='int16', blocksize=160, callback=callback)
        try:
            self.stream.start()
        except BaseException:
            self.stream.close()
            raise

    def check(self):
        if self.error or not self.stream.active:
            raise OSError(self.error or 'Audio output stopped')

    def write(self, data):
        self.check()
        self.remainder.extend(data)
        while len(self.remainder) >= 320:
            self.enqueue(bytes(self.remainder[:320]))
            del self.remainder[:320]

    def enqueue(self, data):
        # Pipe reads can be short or arrive in a replay burst. Let the DAC
        # consume queued audio before declaring a stall. USB/DSP never waits here.
        deadline = time.monotonic() + 0.5
        while True:
            self.check()
            try:
                self.queue.put(data, timeout=0.02)
                return
            except queue.Full as exc:
                if time.monotonic() >= deadline:
                    raise OSError('Audio output stalled (no queue progress for 500 ms)') from exc

    def close(self):
        try:
            if self.remainder and not self.error and self.stream.active:
                self.enqueue(bytes(self.remainder))
                self.remainder.clear()
            deadline = time.monotonic()+1.25
            while (not self.queue.empty() or self.pending) and self.stream.active and time.monotonic() < deadline:
                time.sleep(0.01)
        finally:
            self.stream.abort()
            self.stream.close()


class Decoder:
    def __init__(self, executable, audio, wav_path, log_path, popen=subprocess.Popen):
        self.audio = audio
        self.errors = queue.Queue()
        self.input = queue.Queue(maxsize=16)
        self.wav = self.proc = self.log = None
        self.threads = []
        self.stopped = threading.Event()
        self.output_ended = threading.Event()
        try:
            self.log = open(log_path, 'xb')
            if wav_path:
                # Exclusive creation; wave owns the supplied file through close below.
                self.wav_file = open(wav_path, 'xb')
                self.wav = wave.open(self.wav_file, 'wb')
                self.wav.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
            self.proc = popen([str(executable), '-l', '-d'], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=self.log, bufsize=0)
            def feed():
                while not self.stopped.is_set():
                    try:
                        data = self.input.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    if data is None:
                        self.proc.stdin.close()
                        return
                    view = memoryview(data)
                    while view:
                        n = self.proc.stdin.write(view)
                        if not n:
                            raise OSError('Decoder stdin closed')
                        view = view[n:]
            def drain():
                carry = b''
                while True:
                    data = self.proc.stdout.read(320)
                    if not data:
                        if carry:
                            raise OSError('Decoder returned incomplete PCM16 sample')
                        self.output_ended.set()
                        return
                    carry += data
                    n = len(carry) & ~1
                    if n:
                        if self.wav:
                            self.wav.writeframesraw(carry[:n])
                        self.audio.write(carry[:n])
                    carry = carry[n:]
            def guarded(fn):
                try:
                    fn()
                except Exception as exc:
                    self.errors.put(exc)
            for fn in (feed, drain):
                t = threading.Thread(target=guarded, args=(fn,), daemon=True)
                self.threads.append(t)
                t.start()
        except BaseException:
            self.close()
            raise

    def check(self):
        if hasattr(self.audio, 'check'):
            self.audio.check()
        try:
            error = self.errors.get_nowait()
        except queue.Empty:
            error = None
        if error:
            raise OSError(f'Decoder/playback: {error}') from error
        if self.proc.poll() is not None:
            raise OSError(f'Decoder exited unexpectedly: {self.proc.returncode}; see decoder log')
        if self.output_ended.is_set():
            raise OSError('Decoder stdout closed unexpectedly; see decoder log')

    def write(self, data):
        self.check()
        # Never wait on decoder stdin in the USB reader.
        try:
            self.input.put_nowait(data)
        except queue.Full as exc:
            raise OSError('Decoder stalled (bounded PCM queue full)') from exc

    def close(self):
        if self.proc:
            try:
                self.input.put_nowait(None)
                self.proc.wait(timeout=2)
            except (queue.Full, subprocess.TimeoutExpired):
                print('RX shutdown: decoder drain deadline/full queue; terminating decoder', file=sys.stderr)
                self.proc.kill()
                self.proc.wait(timeout=2)
            self.stopped.set()
            for t in self.threads:
                t.join(timeout=2)
            for pipe in (self.proc.stdin, self.proc.stdout):
                if pipe:
                    pipe.close()
            try:
                error = self.errors.get_nowait()
                print(f'RX shutdown: {error}', file=sys.stderr)
            except queue.Empty:
                pass
        if self.wav:
            self.wav.close()
            self.wav_file.close()
        if self.log:
            self.log.close()


class IQPump:
    """Dedicated USB reader; bounded backlog, no decoder/DAC waits."""
    def __init__(self, source):
        self.source = source
        self.blocks = queue.Queue(maxsize=32)  # 2.1 s at default rate/block; covers history replay
        self.errors = queue.Queue()
        self.stopped = threading.Event()
        def read():
            try:
                while not self.stopped.is_set():
                    block = source.read_block()
                    if self.stopped.is_set():
                        return
                    if not len(block):
                        raise OSError('Empty USB RX read')
                    try:
                        self.blocks.put_nowait(block)
                    except queue.Full as exc:
                        raise OSError('USB I/Q backlog full; DSP cannot keep up') from exc
            except BaseException as exc:
                if not self.stopped.is_set():
                    self.errors.put(exc)
        self.thread = threading.Thread(target=read, daemon=True)
        self.thread.start()

    def get(self):
        deadline = time.monotonic()+3
        while True:
            try:
                raise self.errors.get_nowait()
            except queue.Empty:
                pass
            try:
                return self.blocks.get(timeout=0.05)
            except queue.Empty:
                if time.monotonic() > deadline:
                    raise TimeoutError('USB RX reader stalled (3 s)')

    def close(self):
        self.stopped.set()
        if hasattr(self.source, 'cancel'):
            self.source.cancel()
        self.thread.join(timeout=3)
        if self.thread.is_alive():
            raise OSError('USB RX cancellation failed; reader did not stop')


def receive(a, source_factory=None, audio_factory=CallbackAudio, decoder_factory=Decoder, stop=None, report=None):
    if source_factory is None:
        from mini_iio_rx import MiniRX
        source_factory = MiniRX
    source = audio = decoder = pump = None
    try:
        audio = audio_factory(a.audio_device)
        decoder = decoder_factory(a.demod, audio, a.wav, a.log)
        source = source_factory(a.libiio_dll, a.uri)
        source.prepare_rx(a.sample_rate, a.center_hz, a.block_size)
        offset = a.target_hz-a.center_hz
        if abs(offset)+12000 >= source.rate/2:
            raise ValueError('Target outside actual RX Nyquist band')
        acquisition = LiveAcquisition(source.rate, offset, a.detect_ratio)
        pump = IQPump(source)
        rx = None
        print('RX searching; Ctrl+C stops; TX LO disabled')
        if report:
            report('RX')
        while stop is None or not stop.is_set():
            decoder.check()
            if report:
                report("heartbeat")
            block = pump.get()
            if rx is None:
                found = acquisition.push(block)
                if found is None:
                    continue
                cfo, blocks = found
                print(f'RX acquired: CFO={cfo:+.0f} Hz; replay={sum(map(len, blocks))/source.rate:.3f} s')
                rx = FMReceiver(source.rate, offset, 'IQ', cfo)
            else:
                blocks = (block,)
            for saved in blocks:
                pcm = rx.process(saved)
                if len(pcm):
                    decoder.write(pcm.tobytes())
    finally:
        # Free USB before waiting on any decoder/audio resource.
        try:
            try:
                if pump:
                    pump.close()
            finally:
                if source:
                    source.close()
        finally:
            try:
                if decoder:
                    decoder.close()
            finally:
                if audio:
                    audio.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--list-audio', action='store_true')
    p.add_argument('--uri')
    p.add_argument('--libiio-dll', default='C:/libiio/Windows-VS-2022-x64/libiio.dll')
    p.add_argument('--center-hz', type=int)
    p.add_argument('--target-hz', type=int)
    p.add_argument('--sample-rate', type=int, default=4000000)
    p.add_argument('--block-size', type=int, default=262144)
    p.add_argument('--detect-ratio', type=float, default=0.25)
    p.add_argument('--audio-device', default='JBL TUNE 305C USB-C')
    p.add_argument('--demod', default='C:/M17/m17-demod.exe')
    p.add_argument('--wav', help='New decoded 8 kHz WAV path (optional)')
    p.add_argument('--log', default='receiver-output/mini-usb-decoded.log', help='New decoder log path')
    a = p.parse_args()
    if a.list_audio:
        import sounddevice as sd
        print(sd.query_devices())
        return 0
    if not a.uri or not re.fullmatch(r'usb:[0-9]+\.[0-9]+\.[0-9]+', a.uri) or not a.center_hz or not a.target_hz:
        p.error('Explicit --uri usb:BUS.ADDRESS.INTERFACE, --center-hz and --target-hz required')
    if not 96000 <= a.sample_rate <= 61440000 or not 125 <= a.block_size <= a.sample_rate//4 or not 0 < a.detect_ratio < 1:
        p.error('Invalid rate/block/detector ratio; block must be 125 samples .. 250 ms')
    if min(a.center_hz, a.target_hz) <= 0 or abs(a.target_hz-a.center_hz)+12000 >= a.sample_rate/2:
        p.error('Invalid RF frequency or target outside RX Nyquist band')
    if max(a.sample_rate, 96000)//math.gcd(a.sample_rate, 96000) > 4096:
        p.error('Unsupported rational sample-rate ratio')
    if not Path(a.demod).is_file():
        p.error('Missing m17-demod executable')
    try:
        receive(a)
    except KeyboardInterrupt:
        print('RX stopped; USB released')
    except Exception as exc:
        p.exit(1, f'USB RX: {exc}\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
