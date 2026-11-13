"""Receive-only libiio 0.25 adapter. No TX buffer or LO enable operation."""
import ctypes as C
import numpy as np
from pluto_iio import NativeIIO


def unpack_scan(raw, step, offsets, formats):
    if step < 4 or not raw or len(raw) % step:
        raise OSError('Invalid/empty IIO RX scan length or stride')
    values = []
    for offset, fmt in zip(offsets, formats):
        length, bits, shift, signed, be, repeat, scaled = fmt
        if length != 16 or not signed or repeat != 1 or scaled or not 1 <= bits <= 16 or shift + bits > 16 or shift < 0:
            raise OSError(f'Unsupported RX format: {fmt}')
        if offset < 0 or offset + 2 > step:
            raise OSError('Invalid RX channel offset')
        v = np.ndarray((len(raw)//step,), dtype='>u2' if be else '<u2', buffer=raw,
                       offset=offset, strides=(step,)).astype(np.int32)
        v = (v >> shift) & ((1 << bits)-1)
        v = (v ^ (1 << (bits-1))) - (1 << (bits-1))
        values.append(v / float(1 << (bits-1)))
    if len(values) != 2 or offsets[0] == offsets[1]:
        raise OSError('Expected distinct I/Q channels')
    return values[0] + 1j*values[1]


class MiniRX(NativeIIO):
    def __init__(self, dll, uri):
        self.ctx = self.buffer = None
        try:
            super().__init__(dll, uri)
        except BaseException:
            try:
                self.close()
            except Exception:
                pass
            raise

    def power(self, off):
        if not off:
            raise ValueError('RX mode cannot enable TX LO')
        super().power(True)

    def input_channel(self, device, name):
        ch = self.lib.iio_device_find_channel(device, name.encode(), False)
        if not ch:
            raise OSError(f'Missing RX channel: {name}')
        return ch

    def write(self, ch, attr, value):
        self.check(self.lib.iio_channel_attr_write(ch, attr.encode(), str(value).encode()))

    def prepare_rx(self, rate, center, block):
        self.power(True)
        lib = self.lib
        lib.iio_buffer_refill.restype = C.c_ssize_t
        lib.iio_buffer_refill.argtypes = [C.c_void_p]
        lib.iio_buffer_cancel.restype = None
        lib.iio_buffer_cancel.argtypes = [C.c_void_p]
        lib.iio_channel_disable.restype = None
        lib.iio_channel_disable.argtypes = [C.c_void_p]
        lib.iio_device_get_channels_count.restype = C.c_uint
        lib.iio_device_get_channels_count.argtypes = [C.c_void_p]
        lib.iio_device_get_channel.restype = C.c_void_p
        lib.iio_device_get_channel.argtypes = [C.c_void_p, C.c_uint]
        self.rx = self.device('cf-ad9361-lpc')
        phy = self.input_channel(self.phy, 'voltage0')
        lo = self.channel(self.phy, 'altvoltage0')
        self.write(phy, 'sampling_frequency', rate)
        self.write(phy, 'rf_bandwidth', 200000)
        self.write(phy, 'gain_control_mode', 'slow_attack')
        self.write(lo, 'frequency', center)
        self.write(lo, 'powerdown', 0)
        self.rate = int(self.read(phy, 'sampling_frequency'))
        if self.rate != rate or int(self.read(lo, 'frequency')) != center:
            raise OSError('RX sample rate/LO readback differs from requested value')
        for index in range(lib.iio_device_get_channels_count(self.rx)):
            lib.iio_channel_disable(lib.iio_device_get_channel(self.rx, index))
        channels = [self.input_channel(self.rx, name) for name in ('voltage0', 'voltage1')]
        if int(self.read(channels[0], 'sampling_frequency')) != self.rate:
            raise OSError('RX core and PHY sample rates disagree')
        self.formats = []
        for ch in channels:
            f = lib.iio_channel_get_data_format(ch).contents
            self.formats.append((f.length, f.bits, f.shift, f.is_signed, f.is_be, f.repeat, f.with_scale))
            lib.iio_channel_enable(ch)
        self.check(lib.iio_device_set_kernel_buffers_count(self.rx, 4))
        self.buffer = lib.iio_device_create_buffer(self.rx, block, False)
        if not self.buffer:
            raise OSError('Cannot create RX buffer')
        start = lib.iio_buffer_start(self.buffer)
        self.step = lib.iio_buffer_step(self.buffer)
        self.offsets = [lib.iio_buffer_first(self.buffer, ch)-start for ch in channels]
        self.capacity = lib.iio_buffer_end(self.buffer)-start
        if self.capacity != block*self.step:
            raise OSError('Unexpected RX buffer capacity')
        unpack_scan(bytes(self.step), self.step, self.offsets, self.formats)
        print(f'RX readback: rate={self.rate}, center={self.read(lo, "frequency")}, '
              f'bandwidth={self.read(phy, "rf_bandwidth")}, AGC={self.read(phy, "gain_control_mode")}; '
              f'I=voltage0 Q=voltage1 offsets={self.offsets} stride={self.step} formats={self.formats}; '
              'normalization=2**(bits-1); TX LO off')

    def read_block(self):
        size = self.check(self.lib.iio_buffer_refill(self.buffer))
        if size <= 0 or size > self.capacity or size % self.step:
            raise OSError(f'Malformed IIO RX refill: {size} bytes')
        raw = C.string_at(self.lib.iio_buffer_start(self.buffer), size)
        return unpack_scan(raw, self.step, self.offsets, self.formats)

    def cancel(self):
        if self.buffer:
            self.lib.iio_buffer_cancel(self.buffer)

    def close(self):
        try:
            if self.ctx and getattr(self, 'lo', None):
                self.power(True)
        finally:
            try:
                super().close()
            finally:
                if getattr(self, '_dll_dir', None):
                    self._dll_dir.close()
