"""Small ctypes adapter for the native libiio 0.25 USB backend.

push acknowledges transfer, not RF completion. drain uses the explicitly
bounded four-buffer queue plus a guard; destroy is never used as a flush.
"""
import ctypes as C
import os
from pathlib import Path
import re
import time

RATE = 2304000
BUFFER = 76800
KERNEL_BUFFERS = 4


class DataFormat(C.Structure):
    _fields_ = [("length", C.c_uint), ("bits", C.c_uint), ("shift", C.c_uint),
                ("is_signed", C.c_bool), ("is_fully_defined", C.c_bool),
                ("is_be", C.c_bool), ("with_scale", C.c_bool),
                ("scale", C.c_double), ("repeat", C.c_uint)]


class NativeIIO:
    def __init__(self, dll, uri):
        if not isinstance(uri, str) or not re.fullmatch(r"usb:[0-9]+\.[0-9]+\.[0-9]+", uri):
            raise ValueError("Explicit USB URI required (--uri usb:BUS.ADDRESS.INTERFACE); use iio_info -S usb")
        self.ctx = self.buffer = None
        self._dll_dir = os.add_dll_directory(str(Path(dll).resolve().parent))
        self.lib = C.CDLL(str(Path(dll).resolve()))
        p, s, n, b = C.c_void_p, C.c_char_p, C.c_size_t, C.c_bool
        signatures = {
            "iio_library_get_version": (None, [C.POINTER(C.c_uint), C.POINTER(C.c_uint), s]),
            "iio_create_context_from_uri": (p, [s]),
            "iio_context_set_timeout": (C.c_int, [p, C.c_uint]),
            "iio_context_destroy": (None, [p]),
            "iio_context_find_device": (p, [p, s]),
            "iio_device_find_channel": (p, [p, s, b]),
            "iio_channel_attr_read": (C.c_ssize_t, [p, s, p, n]),
            "iio_channel_attr_write": (C.c_ssize_t, [p, s, s]),
            "iio_channel_enable": (None, [p]),
            "iio_channel_get_data_format": (C.POINTER(DataFormat), [p]),
            "iio_device_set_kernel_buffers_count": (C.c_int, [p, C.c_uint]),
            "iio_device_create_buffer": (p, [p, n, b]),
            "iio_buffer_start": (p, [p]),
            "iio_buffer_first": (p, [p, p]),
            "iio_buffer_step": (C.c_ssize_t, [p]),
            "iio_buffer_end": (p, [p]),
            "iio_buffer_push": (C.c_ssize_t, [p]),
            "iio_buffer_destroy": (None, [p]),
        }
        for name, (restype, args) in signatures.items():
            f = getattr(self.lib, name)
            f.restype, f.argtypes = restype, args
        major, minor, tag = C.c_uint(), C.c_uint(), C.create_string_buffer(8)
        self.lib.iio_library_get_version(C.byref(major), C.byref(minor), tag)
        if (major.value, minor.value) != (0, 25):
            raise RuntimeError("This adapter requires libiio 0.25")
        self.ctx = self.lib.iio_create_context_from_uri(uri.encode())
        if not self.ctx:
            raise OSError(f"Cannot open {uri}; rediscover with iio_info -S usb")
        try:
            self.check(self.lib.iio_context_set_timeout(self.ctx, 2000))
            self.phy = self.device("ad9361-phy")
            self.tx = self.device("cf-ad9361-dds-core-lpc")
            self.lo = self.channel(self.phy, "altvoltage1")
        except BaseException:
            self.close()
            raise

    @staticmethod
    def check(value):
        if value < 0:
            raise OSError(f"libiio error {value}")
        return value

    def device(self, name):
        value = self.lib.iio_context_find_device(self.ctx, name.encode())
        if not value:
            raise OSError(f"Missing IIO device: {name}")
        return value

    def channel(self, device, name):
        value = self.lib.iio_device_find_channel(device, name.encode(), True)
        if not value:
            raise OSError(f"Missing output channel: {name}")
        return value

    def read(self, channel, attr):
        buf = C.create_string_buffer(256)
        self.check(self.lib.iio_channel_attr_read(channel, attr.encode(), buf, len(buf)))
        return buf.value.decode().strip()

    def power(self, off):
        self.check(self.lib.iio_channel_attr_write(self.lo, b"powerdown", b"1" if off else b"0"))
        if self.read(self.lo, "powerdown") != str(int(off)):
            raise OSError("TX LO powerdown readback failed")

    def validate(self):
        checks = [(self.lo, "powerdown", 1), (self.lo, "frequency", 438800000),
                  (self.channel(self.tx, "voltage0"), "sampling_frequency", RATE),
                  (self.channel(self.phy, "voltage0"), "hardwaregain", -25)]
        for ch, attr, expected in checks:
            actual = self.read(ch, attr)
            if float(actual.split()[0]) != expected:
                raise ValueError(f"Unsafe TX setting {attr}: {actual}; expected {expected}; TX not started")

    def set_tx_rate(self):
        # RX/TX share the clock: change only with TX LO confirmed off.
        if self.read(self.lo, "powerdown") != "1":
            raise OSError("TX LO must be off before sample-rate change")
        ch = self.channel(self.phy, "voltage0")
        self.check(self.lib.iio_channel_attr_write(ch, b"sampling_frequency", str(RATE).encode()))
        if int(self.read(ch, "sampling_frequency")) != RATE:
            raise OSError("TX PHY sample-rate readback failed")
        # Reapply the known TX workpoint; the panel can reboot at 2.4 GHz/-10 dB.
        # Keep LO off throughout, then verify both PHY and TX core before TX.
        self.check(self.lib.iio_channel_attr_write(self.lo, b"frequency", b"438800000"))
        self.check(self.lib.iio_channel_attr_write(ch, b"hardwaregain", b"-25"))
        self.validate()
        print(f"TX readback: rate={RATE}, frequency=438800000, gain=-25; LO off")

    def prepare(self):
        self.validate()
        self.check(self.lib.iio_device_set_kernel_buffers_count(self.tx, KERNEL_BUFFERS))
        i, q = self.channel(self.tx, "voltage0"), self.channel(self.tx, "voltage1")
        for ch in (i, q):
            fmt = self.lib.iio_channel_get_data_format(ch).contents
            if (fmt.length, fmt.bits, fmt.shift, fmt.is_signed, fmt.is_be, fmt.repeat) != (16, 16, 0, True, False, 1):
                raise OSError("Expected le:S16/16>>0 TX channel format")
        self.lib.iio_channel_enable(i)
        self.lib.iio_channel_enable(q)
        self.buffer = self.lib.iio_device_create_buffer(self.tx, BUFFER, False)
        if not self.buffer:
            raise OSError("Cannot create noncyclic TX buffer")
        start = self.lib.iio_buffer_start(self.buffer)
        if (self.lib.iio_buffer_step(self.buffer) != 4 or
                self.lib.iio_buffer_first(self.buffer, i) != start or
                self.lib.iio_buffer_first(self.buffer, q) != start + 2 or
                self.lib.iio_buffer_end(self.buffer) - start != BUFFER * 4):
            raise OSError("Expected interleaved I16/Q16 TX scan layout")

    def push(self, data):
        if len(data) != BUFFER * 4:
            raise ValueError("TX requires a complete buffer")
        C.memmove(self.lib.iio_buffer_start(self.buffer), data, len(data))
        if self.check(self.lib.iio_buffer_push(self.buffer)) != len(data):
            raise OSError("Short IIO push")

    def drain(self):
        time.sleep((KERNEL_BUFFERS + 1) * BUFFER / RATE + 0.1)

    def close(self):
        if self.buffer:
            self.lib.iio_buffer_destroy(self.buffer)
            self.buffer = None
        if self.ctx:
            self.lib.iio_context_destroy(self.ctx)
            self.ctx = None
