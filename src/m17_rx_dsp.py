"""Stateful Mini RX DSP; normalized complex I/Q in, 48 kHz PCM16 out."""
import math
import numpy as np
from scipy import signal


class Resampler:
    """Causal polyphase FIR with bounded history and rational clock phase."""
    def __init__(self, source, target):
        g = math.gcd(source, target)
        self.up, self.down = target // g, source // g
        m = max(self.up, self.down)
        if m > 4096:
            raise ValueError('Sample-rate ratio too complex (maximum reduced factor 4096)')
        self.identity = m == 1
        if self.identity:
            return
        self.taps = signal.firwin(20 * m + 1, 1 / m, window=('kaiser', 8.6)) * self.up
        # A whole number of down input samples keeps output clock alignment.
        self.keep = math.ceil((len(self.taps) - 1) / self.up / self.down) * self.down
        self.history = np.zeros(self.keep)
        self.pending = np.empty(0)

    def process(self, x):
        if self.identity:
            return np.asarray(x).copy()
        x = np.concatenate((self.pending, x))
        n = len(x) // self.down * self.down
        self.pending = x[n:].copy()
        if not n:
            return np.empty(0, dtype=x.dtype)
        joined = np.concatenate((self.history, x[:n]))
        y = signal.upfirdn(self.taps, joined, up=self.up, down=self.down)
        start = self.keep * self.up // self.down
        self.history = joined[-self.keep:].copy()
        return y[start:start + n * self.up // self.down]


class Channelizer:
    def __init__(self, rate, offset, order='IQ'):
        self.rate, self.offset, self.order = rate, offset, order
        self.index = 0
        self.resampler = Resampler(rate, 96000)

    def process(self, iq):
        x = np.asarray(iq, dtype=np.complex128)
        if self.order == 'QI':
            x = 1j * x.conj()
        # Absolute integer clock avoids block-dependent oscillator drift.
        n = np.arange(len(x), dtype=np.int64) + self.index
        phase = np.remainder(n * (self.offset / self.rate), 1.0)
        self.index += len(x)
        return self.resampler.process(x * np.exp(-2j * np.pi * phase))


class FMReceiver:
    """Future IIO adapter calls process(normalized_complex_block)."""
    def __init__(self, rate, offset, order='IQ', cfo=0, counts_per_hz=7168/800):
        self.channel = Channelizer(rate, offset, order)
        self.sos = signal.butter(8, 8000, fs=96000, output='sos')
        self.state = np.zeros((len(self.sos), 2), dtype=complex)
        self.previous = None
        self.index = 0
        self.cfo, self.scale = cfo, counts_per_hz
        self.audio = Resampler(96000, 48000)
        self.clipped = 0

    def process(self, iq):
        y = self.channel.process(iq)
        if not len(y):
            return np.empty(0, dtype='<i2')
        n = np.arange(len(y), dtype=np.int64) + self.index
        self.index += len(y)
        y *= np.exp(-2j * np.pi * np.remainder(n * self.cfo / 96000, 1))
        y, self.state = signal.sosfilt(self.sos, y, zi=self.state)
        previous = y[0] if self.previous is None else self.previous
        d = np.angle(y * np.conj(np.r_[previous, y[:-1]])) * (96000 / (2*np.pi))
        self.previous = y[-1]
        pcm = self.audio.process(d) * self.scale
        self.clipped += int(np.count_nonzero((pcm < -32768) | (pcm > 32767)))
        return np.rint(np.clip(pcm, -32768, 32767)).astype('<i2')
