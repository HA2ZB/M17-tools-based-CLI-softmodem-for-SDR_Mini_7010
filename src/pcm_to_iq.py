"""Offline 48 kHz signed PCM counts -> little-endian complex64 FM.

No modem, audio device, PTT or IIO access. See docs/OFFLINE-IQ.md.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import wave

import numpy as np
from scipy.signal import firwin, lfilter


class Interpolator:
    """Causal integer FIR interpolation; state survives every process call."""
    def __init__(self, factor):
        if not isinstance(factor, int) or not 1 <= factor <= 64:
            raise ValueError("integer interpolation factor must be 1..64")
        self.factor = factor
        self.taps = (np.array([1.0]) if factor == 1 else
                     firwin(32 * factor + 1, 0.8 / factor,
                            window=("kaiser", 8.6)) * factor)
        self.state = np.zeros(len(self.taps) - 1)

    def process(self, x):
        x = np.asarray(x)
        if not len(x):
            return np.empty(0, dtype=x.dtype)
        if self.factor == 1:
            return x.copy()
        up = np.zeros(len(x) * self.factor, dtype=x.dtype)
        up[::self.factor] = x
        y, self.state = lfilter(self.taps, [1.0], up, zi=self.state)
        return y


class Converter:
    def __init__(self, hz_per_count, iq_rate=192000, amplitude=0.5,
                 order="pcm-first"):
        if not math.isfinite(hz_per_count) or hz_per_count == 0:
            raise ValueError("hz_per_count must be finite and nonzero")
        if iq_rate % 48000 or not 48000 <= iq_rate <= 3072000:
            raise ValueError("iq_rate must be 48000 * integer (1..64)")
        if not math.isfinite(amplitude) or not 0 < amplitude <= 1:
            raise ValueError("amplitude must be in (0, 1]")
        if order not in ("pcm-first", "fm-first"):
            raise ValueError("unknown processing order")
        self.scale, self.rate, self.amplitude = hz_per_count, iq_rate, amplitude
        self.order, self.phase = order, 0.0
        self.interpolator = Interpolator(iq_rate // 48000)
        self.closed = False

    def fm(self, x, rate):
        deviation = np.asarray(x, dtype=np.float64) * self.scale
        if not np.all(np.isfinite(deviation)) or np.any(np.abs(deviation) >= rate / 2):
            raise ValueError("nonfinite or Nyquist-exceeding instantaneous deviation")
        phase = self.phase + np.cumsum(deviation * (2 * np.pi / rate))
        if len(phase):
            self.phase = float(phase[-1] % (2 * np.pi))
        return self.amplitude * np.exp(1j * phase)

    def process(self, counts):
        if self.closed:
            raise ValueError("converter already finished")
        x = np.asarray(counts, dtype=np.float64)
        if x.ndim != 1 or not np.all(np.isfinite(x)):
            raise ValueError("input must be a finite mono vector")
        if self.order == "pcm-first":
            return self.fm(self.interpolator.process(x), self.rate)
        return self.interpolator.process(self.fm(x, 48000))

    def finish(self):
        """Emit full FIR tail, with zero deviation outside the input interval."""
        if self.closed:
            raise ValueError("converter already finished")
        n = len(self.interpolator.taps) - 1
        if self.order == "pcm-first":
            tail = self.process(np.zeros(n // self.interpolator.factor))
        else:
            # Zero complex extension is the conventional I/Q FIR tail.
            tail = self.interpolator.process(np.zeros(n // self.interpolator.factor,
                                                     dtype=np.complex128))
        self.closed = True
        return tail


def blocks(path, kind, block_size):
    if kind == "wav":
        with wave.open(str(path), "rb") as f:
            if (f.getnchannels(), f.getsampwidth(), f.getframerate(), f.getcomptype()) != (1, 2, 48000, "NONE"):
                raise ValueError("WAV must be uncompressed mono signed PCM16 at 48000 Hz")
            while True:
                raw = f.readframes(block_size)
                if not raw:
                    break
                yield np.frombuffer(raw, dtype="<i2")
    else:
        with open(path, "rb") as f:
            while True:
                raw = f.read(block_size * 2)
                if not raw:
                    break
                if len(raw) % 2:
                    raise ValueError("truncated s16le sample")
                yield np.frombuffer(raw, dtype="<i2")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("input", type=Path)
    p.add_argument("output", type=Path)
    p.add_argument("--input-format", choices=["wav", "s16le"], required=True)
    p.add_argument("--hz-per-count", type=float, required=True,
                   help="signed Hz per raw int16 count; no peak normalization")
    p.add_argument("--iq-rate", type=int, default=192000)
    p.add_argument("--amplitude", type=float, default=0.5)
    p.add_argument("--block-size", type=int, default=4096)
    p.add_argument("--order", choices=["pcm-first", "fm-first"], default="pcm-first")
    a = p.parse_args()
    metadata = Path(str(a.output) + ".json")
    if a.block_size <= 0:
        p.error("block-size must be positive")
    if a.output.exists() or metadata.exists() or a.input.resolve() == a.output.resolve():
        p.error("output and metadata must be new files")
    try:
        c = Converter(a.hz_per_count, a.iq_rate, a.amplitude, a.order)
        count, output_count, peak, rails = 0, 0, 0, 0
        digest = hashlib.sha256()
        with a.input.open("rb") as f:
            for raw in iter(lambda: f.read(1048576), b""):
                digest.update(raw)
        with a.output.open("xb") as out:
            for x in blocks(a.input, a.input_format, a.block_size):
                count += len(x)
                peak = max(peak, int(np.max(np.abs(x.astype(np.int32)))))
                rails += int(np.count_nonzero((x == -32768) | (x == 32767)))
                z = c.process(x)
                out.write(z.astype("<c8").tobytes())
                output_count += len(z)
            if not count:
                raise ValueError("empty input")
            z = c.finish()
            out.write(z.astype("<c8").tobytes())
            output_count += len(z)
        info = dict(input=str(a.input), input_sha256=digest.hexdigest(),
                    input_format=a.input_format, input_rate=48000, input_samples=count,
                    input_peak_counts=peak, input_rail_samples=rails,
                    hz_per_count=a.hz_per_count, iq_rate=a.iq_rate,
                    amplitude=a.amplitude, order=a.order, block_size=a.block_size,
                    output_format="cf32_le interleaved I,Q", output_samples=output_count,
                    fir_taps=len(c.interpolator.taps),
                    fir_delay_output_samples=(len(c.interpolator.taps)-1)//2,
                    tail_output_samples=len(z), initial_phase=0,
                    numpy_version=np.__version__)
        metadata.write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    except (ValueError, OSError, wave.Error) as e:
        p.exit(1, f"conversion failed: {e}; any partial output must be discarded\n")


if __name__ == "__main__":
    main()
