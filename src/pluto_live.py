"""Live microphone TX. All audio and IIO work lives outside the keyboard loop."""
import multiprocessing as mp
import os
import queue
from collections import deque
import signal
import subprocess
import threading
import time

from pluto_iio import NativeIIO, RATE, BUFFER, KERNEL_BUFFERS


class StreamInterpolator:
    """Same causal FIR as Converter, evaluated with polyphase convolution."""
    def __init__(self, original):
        import numpy as np
        self.taps, self.factor = original.taps, original.factor
        self.history = np.zeros((len(self.taps) - 1) // self.factor)

    def process(self, x):
        import numpy as np
        from scipy.signal import upfirdn
        if not len(x):
            return np.empty(0)
        joined = np.concatenate((self.history, x))
        offset = len(self.history) * self.factor
        out = upfirdn(self.taps, joined, up=self.factor)
        self.history = joined[-len(self.history):].copy()
        return out[offset:offset + len(x) * self.factor]


def commands(cfg):
    p = cfg["pluto"]
    call = p.get("callsign", cfg.get("callsign", ""))
    can = p.get("can", 0)
    if (not isinstance(call, str) or not 1 <= len(call) <= 9 or
            any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-/ ." for c in call) or
            not isinstance(can, int) or not 0 <= can <= 15):
        raise ValueError("Pluto callsign and CAN (0..15) are required")
    mic = p["tx_microphone"]["device_contains"]
    if not mic:
        raise ValueError("Explicit Pluto microphone required")
    return ([cfg["paths"]["sox"], "-q", "--buffer", "2048", "--input-buffer", "2048", "-t", "waveaudio", mic,
             "-r", "8000", "-e", "signed-integer", "-b", "16", "-c", "1", "-t", "raw", "-"],
            [os.path.join(cfg["paths"]["m17_tools"], "m17-mod.exe"), "-S", call, "-C", str(can)])


def transmit(cfg, stop, report, sink_factory=NativeIIO, popen=subprocess.Popen):
    """Bounded PCM queue; source termination delivers EOF directly to m17-mod.

    No shell, no fixed sample count, no files. A daemon watcher stops only
    SoX on a normal PTT release; m17-mod remains alive through its EOF/EOT.
    """
    import numpy as np
    from pcm_to_iq import Converter

    sink = mic = mod = None
    finished = threading.Event()
    tx_blocks = 0
    max_feed_gap = max_push = max_pcm_wait = 0.0
    empty_queue_reads = 0
    chunks = queue.Queue(maxsize=12)  # bounded PCM queue, 400 ms at nominal rate
    try:
        if stop.is_set():
            return
        mic_cmd, mod_cmd = commands(cfg)
        p = cfg["pluto"]
        sink = sink_factory(p["libiio_dll"], p.get("uri"))
        if p.get("half_duplex"):
            sink.set_tx_rate()
        sink.prepare()  # validates with LO OFF before opening microphone
        if stop.is_set():
            return
        mic = popen(mic_cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        mod = popen(mod_cmd, stdin=mic.stdout, stdout=subprocess.PIPE,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        mic.stdout.close()  # only m17-mod owns the read end

        def stop_source():
            while not finished.wait(0.02):
                if stop.is_set():
                    if mic.poll() is None:
                        mic.terminate()  # close waveaudio + pipe; NOT the modulator
                    return

        def read_pcm():
            try:
                while True:
                    data = mod.stdout.read(3200)
                    chunks.put(data)
                    if not data:
                        return
            except Exception as exc:
                chunks.put(exc)

        threading.Thread(target=stop_source, daemon=True).start()
        threading.Thread(target=read_pcm, daemon=True).start()
        # Prefill 400 ms before RF; short bursts may reach EOF first.
        initial = deque()
        for _ in range(12):
            data = chunks.get(timeout=3)
            if isinstance(data, Exception):
                raise data
            initial.append(data)
            if not data:
                break
        conv = Converter(800 / 7168, RATE)
        conv.interpolator = StreamInterpolator(conv.interpolator)
        pending = bytearray()
        enabled = False
        last_push = None
        carry = b""

        def send(data):
            nonlocal enabled, last_push, tx_blocks, max_feed_gap, max_push
            pending.extend(data)
            while len(pending) >= BUFFER * 4:
                if not enabled:
                    sink.validate()  # check again immediately before LO-on
                    sink.power(False)
                    enabled = True
                    report("TX")
                gap = time.monotonic() - last_push if last_push is not None else 0
                budget = (KERNEL_BUFFERS - 1) * BUFFER / RATE
                max_feed_gap = max(max_feed_gap, gap)
                if gap > budget:
                    raise TimeoutError(f"TX feed gap {gap*1000:.0f} ms (budget {budget*1000:.0f} ms); possible underrun")
                push_start = time.perf_counter()
                sink.push(bytes(pending[:BUFFER * 4]))
                max_push = max(max_push, time.perf_counter() - push_start)
                tx_blocks += 1
                last_push = time.monotonic()
                del pending[:BUFFER * 4]
                report("heartbeat")

        while True:
            if initial:
                data = initial.popleft()
            else:
                if enabled and chunks.empty():
                    empty_queue_reads += 1
                pcm_wait_start = time.perf_counter()
                data = chunks.get(timeout=0.25 if enabled else 3)
                if enabled:
                    max_pcm_wait = max(max_pcm_wait, time.perf_counter() - pcm_wait_start)
            if isinstance(data, Exception):
                raise data
            if not data:
                break
            carry += data
            n = len(carry) & ~1
            z = conv.process(np.frombuffer(carry[:n], dtype="<i2")).astype("<c8")
            carry = carry[n:]
            iq = np.empty((len(z), 2), dtype="<i2")
            iq[:, 0] = np.rint(z.real * 32752)
            iq[:, 1] = np.rint(z.imag * 32752)
            send(iq.tobytes())
        if carry:
            raise ValueError("Truncated PCM16 from m17-mod")
        if not stop.is_set():
            raise OSError("Microphone/modulator ended before PTT release")
        if mod.wait(timeout=2) != 0:
            raise OSError("m17-mod failed while closing M17 stream")
        mic.wait(timeout=2)
        report("EOF/EOT drained")
        # Match tested stream: no Converter.finish() carrier extension.
        # Six full zero buffers AFTER the partially filled useful buffer.
        if pending:
            send(bytes(BUFFER * 4 - len(pending)))
        for _ in range(6):
            send(bytes(BUFFER * 4))
        report("200 ms zero IQ sent")
        sink.drain()
        report("drained")
    except queue.Empty as exc:
        raise TimeoutError("PCM starvation/start timeout; TX aborted") from exc
    finally:
        finished.set()
        # LO-off precedes any potentially blocking destruction/child cleanup.
        try:
            if sink is not None:
                sink.power(True)
                report("LO off")
                if tx_blocks:
                    report(f"Timing: {tx_blocks} blocks; max feed gap {max_feed_gap*1000:.1f} ms; "
                           f"max USB push {max_push*1000:.1f} ms; max PCM wait {max_pcm_wait*1000:.1f} ms; "
                           f"empty PCM queue reads {empty_queue_reads}")
        finally:
            for proc in (mic, mod):
                if proc is not None:
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait(timeout=2)
            if mod is not None:
                mod.stdout.close()
            if sink is not None:
                sink.close()


def worker(cfg, stop, connection, armed):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        # No hardware until the parent owns this session and polls it. An
        # interrupt during Process.start cannot leave an orphan transmitting.
        if not armed.wait(5):
            return
        transmit(cfg, stop, connection.send)
        connection.send("DONE")
    except BaseException as exc:
        connection.send("ERROR: " + str(exc))
    finally:
        connection.close()


def off_worker(cfg):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    sink = NativeIIO(cfg["pluto"]["libiio_dll"], cfg["pluto"].get("uri"))
    try:
        sink.power(True)
    finally:
        sink.close()


class LiveSession:
    """Nonblocking parent API, with startup, stall and graceful-stop deadlines."""
    def __init__(self, cfg, worker_target=None):
        self.cfg = cfg
        self.context = mp.get_context("spawn")
        self.stop = self.context.Event()
        self.armed = self.context.Event()
        self.reader, sender = self.context.Pipe(duplex=False)
        self.process = self.context.Process(target=worker_target or worker, args=(cfg, self.stop, sender, self.armed))
        self.deadline = time.monotonic() + 15
        self.state = "STARTING"
        self.stopping = False
        self.cleaned = False
        self.done_received = False
        self.process.start()
        sender.close()

    def request_stop(self):
        if not self.stopping:
            self.stopping = True
            self.stop.set()
            self.state = "STOPPING"
            self.deadline = time.monotonic() + 8

    def emergency(self):
        # Further Ctrl+C must not interrupt the independent LO-off attempt.
        previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            self._emergency()
        finally:
            signal.signal(signal.SIGINT, previous)

    def _emergency(self):
        # Emergency ONLY: kill worker and children, then independent bounded LO-off.
        if self.process.is_alive():
            try:
                subprocess.run(["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                pass
            finally:
                if self.process.is_alive():
                    self.process.kill()
        self.process.join(timeout=1)
        off = self.context.Process(target=off_worker, args=(self.cfg,))
        off.start()
        off.join(timeout=5)
        if off.is_alive():
            off.kill()
            off.join(timeout=1)
            raise OSError("Emergency LO-off timed out; verify powerdown manually / disconnect TX")
        if off.exitcode:
            raise OSError("Emergency LO-off failed; verify powerdown manually / disconnect TX")

    def poll(self):
        try:
            self.armed.set()
            while not getattr(self, "done_received", False) and self.reader.poll():
                message = self.reader.recv()
                if message == "DONE":
                    self.done_received = True
                    break
                if message.startswith("ERROR:"):
                    raise OSError(message)
                if message != "heartbeat":
                    print("Pluto:", message)
                    if not self.stopping:
                        self.state = message
                if not self.stopping:
                    self.deadline = time.monotonic() + 4
            if getattr(self, "done_received", False):
                self.process.join(timeout=0)
                if not self.process.is_alive():
                    if self.process.exitcode != 0:
                        raise OSError("Worker failed after closure")
                    self.reader.close()
                    self.cleaned = True
                    return True
            elif not self.process.is_alive():
                raise OSError("Worker exited without successful closure")
            if time.monotonic() > self.deadline:
                raise TimeoutError("TX worker deadline expired")
        except (OSError, EOFError, TimeoutError):
            try:
                self.emergency()
            finally:
                self.reader.close()
                self.cleaned = True
            raise
        return False
