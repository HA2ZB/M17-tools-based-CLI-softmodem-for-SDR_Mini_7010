import argparse
import os
import sys
import time
import msvcrt
import subprocess
import yaml
import re


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "m17_config.yaml")


class RXFrameDisplay:
    """Display decoder reports, never infer CRC validity or missing frame IDs."""

    def __init__(self):
        self.pending = ""

    def feed(self, text, final=False):
        parts = re.split(r"[\r\n]", self.pending + text)
        self.pending = parts.pop()
        if final:
            parts.append(self.pending)
            self.pending = ""
        for line in parts:
            line = line.strip()
            if line.startswith("SRC:") and "DEST:" in line and "CRC:" in line:
                line = re.sub(r"(DEST:\s*)(ALL|BROADCAST)(?=\s*,)",
                              r"\1broadcast", line)
                print("[RX LSF] " + line +
                      " | CRC field only; validation not reported; "
                      "frame ID not provided by decoder", flush=True)
            elif line == "EOS":
                print("[RX] End of stream (EOS)", flush=True)
            elif re.match(r"^(?:FN|FRAME|Frame)(?:\s*[:=]|\s+\d)", line):
                print("[RX frame] " + line +
                      " | no general stream-frame CRC", flush=True)
            elif "checksum error" in line.lower() or "frame sequence error" in line.lower():
                print("[RX error] " + line, flush=True)
        # Malformed/unterminated diagnostic output must not grow memory forever.
        self.pending = self.pending[-4096:]


def pluto_rx_display_worker(cfg, stop, connection, armed):
    """Same RX lifecycle as pluto_duplex, with a read-only decoder-log display."""
    import signal
    import uuid
    from pathlib import Path
    from types import SimpleNamespace
    from m17_rx_usb import Decoder, receive

    class DisplayDecoder(Decoder):
        def __init__(self, executable, audio, wav_path, log_path):
            self.reader = None
            self.display = RXFrameDisplay()
            super().__init__(executable, audio, wav_path, log_path)
            try:
                self.reader = open(log_path, "rb")
            except BaseException:
                self.close()
                raise

        def show_reports(self, final=False):
            if self.reader:
                while True:
                    data = self.reader.read(65536)
                    if not data:
                        break
                    self.display.feed(data.decode("utf-8", errors="replace"))
                if final:
                    self.display.feed("", final=True)

        def check(self):
            self.show_reports()
            super().check()

        def close(self):
            try:
                super().close()
            finally:
                if self.reader:
                    try:
                        self.show_reports(final=True)
                    finally:
                        self.reader.close()
                        self.reader = None

    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        if not armed.wait(5):
            return
        p = cfg["pluto"]
        directory = Path("receiver-output")
        directory.mkdir(exist_ok=True)
        a = SimpleNamespace(
            uri=p.get("uri"), libiio_dll=p["libiio_dll"],
            sample_rate=4000000, center_hz=438858300, target_hz=438800000,
            block_size=262144, detect_ratio=0.25,
            audio_device=str(p.get("rx_audio_device", "JBL TUNE 305C USB-C")),
            demod=os.path.join(cfg["paths"]["m17_tools"], "m17-demod.exe"),
            wav=None, log=str(directory / ("duplex-" + uuid.uuid4().hex + ".log")))
        receive(a, stop=stop, report=connection.send, decoder_factory=DisplayDecoder)
        connection.send("DONE")
    except BaseException as exc:
        connection.send("ERROR: " + str(exc))
    finally:
        connection.close()


def pluto_rx_display_session(cfg):
    from pluto_live import LiveSession
    return LiveSession(cfg, worker_target=pluto_rx_display_worker)


def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_endpoint_volume(dev_id):
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    from comtypes import CLSCTX_ALL

    enumerator = AudioUtilities.GetDeviceEnumerator()
    endpoint = enumerator.GetDevice(dev_id)
    interface = endpoint.Activate(
        IAudioEndpointVolume._iid_,
        CLSCTX_ALL,
        None
    )
    return interface.QueryInterface(IAudioEndpointVolume)


def restore_audio_levels(cfg):
    from pycaw.pycaw import AudioUtilities

    devices = AudioUtilities.GetAllDevices()
    items = [
        ("TX microphone", cfg["audio"]["tx_microphone"]),
        ("TX output", cfg["audio"]["tx_output"]),
        ("RX input", cfg["audio"]["rx_input"]),
    ]

    print("M17 station check")
    print("=" * 50)

    for label, item in items:
        wanted = item["device_contains"]
        target = float(item["level_percent"])

        found = None
        for dev in devices:
            name = getattr(dev, "FriendlyName", "")
            state = str(getattr(dev, "state", ""))
            if "Active" in state and wanted in name:
                found = dev
                break

        if not found:
            print(f"[WARN] {label}: not found / not active: {wanted}")
            continue

        vol = get_endpoint_volume(found.id)
        old = vol.GetMasterVolumeLevelScalar() * 100
        vol.SetMasterVolumeLevelScalar(target / 100.0, None)
        vol.SetMute(0, None)
        new = vol.GetMasterVolumeLevelScalar() * 100

        print(f"[OK] {label}: {found.FriendlyName}")
        print(f"     {old:.1f}% -> {new:.1f}%")

    radio = cfg["radio"]
    print()
    print(
        f"[CHECK] {radio['model']} MENU {radio['pkt_mic_menu']} "
        f"PKT MIC = {radio['pkt_mic_value']}"
    )
    print("=" * 50)
    print()


class M17TRX:
    def __init__(self, cfg):
        self.cfg = cfg
        self.mode = "RX"
        self.proc = None
        self.ptt = None

        self.call = cfg["callsign"]
        self.dest = cfg["destination"]
        self.ptt_com = cfg["ptt"]["com_port"]

        self.rx_audio = cfg["audio"]["rx_input"]["device_contains"]
        self.rx_out = cfg["audio"]["rx_output"]["waveaudio_device"]
        self.tx_audio = cfg["audio"]["tx_microphone"]["device_contains"]
        self.tx_out = cfg["audio"]["tx_output"]["waveaudio_device"]
        
        self.m17_tools = cfg["paths"]["m17_tools"]
        self.sox = cfg["paths"]["sox"]
        self.ffplay = cfg["paths"].get("ffplay", "ffplay.exe")

        self.m17_mod = os.path.join(self.m17_tools, "m17-mod.exe")
        self.m17_demod = os.path.join(self.m17_tools, "m17-demod.exe")

    def rx_cmd(self):
        return (
            f'"{self.sox}" -q -t waveaudio "{self.rx_audio}" '
            f'-r 48000 -e signed-integer -b 16 -c 1 -t raw - vol -1 '
            f'| "{self.m17_demod}" -l '
            f'| "{self.sox}" -q -t raw -r 8000 -e signed-integer -b 16 -c 1 - '
            f'-t waveaudio {self.rx_out}'
        )
    
    def tx_cmd(self):
        return (
            f'"{self.sox}" -q -t waveaudio "{self.tx_audio}" '
            f'-r 8000 -e signed-integer -b 16 -c 1 -t raw - '
            f'| "{self.m17_mod}" -S {self.call} -i -C 0 '
            f'| "{self.sox}" -q -t raw -r 48000 -e signed-integer -b 16 -c 1 - '
            f'-t waveaudio {self.tx_out}'
        )

    def screen(self):
        os.system("cls")
        print("========================================")
        print("          M17 CLI TRANSCEIVER")
        print("========================================")
        print(f"Call : {self.call}")
        print(f"Dest : {self.dest}")
        print(f"Mode : {self.mode}")
        print(f"PTT  : {'ON' if self.ptt else 'OFF'}")
        print("----------------------------------------")
        print("SPACE  RX/TX switch")
        print("ESC    Exit")
        print("========================================")
        print()

    def start_process(self, cmd):
        return subprocess.Popen(
            cmd,
            shell=True,
            stdin=subprocess.DEVNULL,
        )

    def stop_process(self):
        if self.proc and self.proc.poll() is None:
            subprocess.run(
                ["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(0.3)
        self.proc = None

    def ptt_on(self):
        import serial

        if self.ptt:
            return
        self.ptt = serial.Serial(self.ptt_com, 9600, rtscts=False)
        self.ptt.rts = True
        time.sleep(0.15)

    def ptt_off(self):
        if self.ptt:
            try:
                self.ptt.rts = False
                self.ptt.close()
            except Exception:
                pass
        self.ptt = None
        time.sleep(0.15)

    def start_rx(self):
        self.stop_process()
        self.ptt_off()
        self.mode = "RX"
        self.screen()
        self.proc = self.start_process(self.rx_cmd())

    def start_tx(self):
        self.stop_process()
        self.mode = "TX"
        self.screen()
        self.ptt_on()
        self.screen()
        self.proc = self.start_process(self.tx_cmd())

    def toggle(self):
        if self.mode == "RX":
            self.start_tx()
        else:
            self.start_rx()

    def shutdown(self):
        self.stop_process()
        self.ptt_off()
        print("Stopped.")
        sys.exit(0)

    def run(self):
        self.start_rx()

        while True:
            if self.proc and self.proc.poll() is not None:
                self.start_rx()

            if msvcrt.kbhit():
                key = msvcrt.getch()

                if key == b" ":
                    self.toggle()

                elif key == b"\x1b":
                    self.shutdown()

            time.sleep(0.05)


class PlutoTXControl:
    """Live TX control; waiting opens no audio, serial or IIO resources."""

    def __init__(self, cfg, session_factory=None):
        self.cfg = cfg
        self.session_factory = session_factory
        self.session = None
        self.exiting = False
        self.failed = False
        self.microphone = cfg.get("pluto", {}).get("tx_microphone", {}).get(
            "device_contains", "Windows default microphone"
        )
        self.tx_requested = False

    def screen(self):
        print("M17 PLUTO LIVE TX")
        print(f"Microphone: {self.microphone}; Windows levels unchanged")
        print("State:", self.session.state if self.session else "WAITING")
        print("SPACE start/stop; ESC / Ctrl+C orderly stop and exit")

    def toggle(self):
        if self.session:
            self.session.request_stop()
            self.tx_requested = False
        elif not self.exiting:
            factory = self.session_factory
            if factory is None:
                from pluto_live import LiveSession
                factory = LiveSession
            self.session = factory(self.cfg)
            self.tx_requested = True
        self.screen()

    def shutdown(self):
        self.exiting = True
        self.tx_requested = False
        if self.session:
            self.session.request_stop()

    def run(self):
        self.screen()
        while True:
            try:
                if self.session and self.session.poll():
                    self.session = None
                    self.tx_requested = False
                    self.screen()
                if self.exiting and self.session is None:
                    break
                if msvcrt.kbhit():
                    key = msvcrt.getch()
                    if key == b" ":
                        self.toggle()
                    elif key in (b"\x1b", b"\x03"):
                        self.shutdown()
                time.sleep(0.05)
            except KeyboardInterrupt:
                self.shutdown()
            except Exception as exc:
                # poll cleans its own failures; cover unrelated UI failures too.
                if self.session and not getattr(self.session, "cleaned", False):
                    try:
                        self.session.emergency()
                    except Exception as off_error:
                        print(f"Pluto emergency: {off_error}", file=sys.stderr)
                print(f"Pluto TX error: {exc}", file=sys.stderr)
                self.session = None
                self.exiting = True
                self.failed = True
        print("Stopped.")
        return int(self.failed)


def discover_pluto_usb(dll):
    """Discover a single compatible USB panel without changing radio settings.

    Uses the same libiio 0.25 DLL as RX/TX. Context/device inspection only:
    no channel writes, buffers, audio or PTT operations take place here.
    """
    import ctypes as C
    from pathlib import Path
    import re

    path = Path(dll).resolve()
    with os.add_dll_directory(str(path.parent)):
        lib = C.CDLL(str(path))
        p, s = C.c_void_p, C.c_char_p
        signatures = {
            "iio_library_get_version": (None, [C.POINTER(C.c_uint), C.POINTER(C.c_uint), s]),
            "iio_create_scan_context": (p, [s, C.c_uint]),
            "iio_scan_context_get_info_list": (C.c_ssize_t, [p, C.POINTER(C.POINTER(p))]),
            "iio_context_info_get_uri": (s, [p]),
            "iio_context_info_list_free": (None, [C.POINTER(p)]),
            "iio_scan_context_destroy": (None, [p]),
            "iio_create_context_from_uri": (p, [s]),
            "iio_context_find_device": (p, [p, s]),
            "iio_context_destroy": (None, [p]),
        }
        for name, (restype, argtypes) in signatures.items():
            function = getattr(lib, name)
            function.restype, function.argtypes = restype, argtypes
        major, minor, tag = C.c_uint(), C.c_uint(), C.create_string_buffer(8)
        lib.iio_library_get_version(C.byref(major), C.byref(minor), tag)
        if (major.value, minor.value) != (0, 25):
            raise RuntimeError("USB discovery requires libiio 0.25")
        scan = lib.iio_create_scan_context(b"usb", 0)
        if not scan:
            raise OSError("Cannot create libiio USB discovery context")
        infos = C.POINTER(p)()
        matches, unavailable = set(), set()
        try:
            count = lib.iio_scan_context_get_info_list(scan, C.byref(infos))
            if count < 0:
                raise OSError(f"libiio USB discovery failed: {count}")
            for index in range(count):
                raw_uri = lib.iio_context_info_get_uri(infos[index])
                if not raw_uri:
                    continue
                uri = raw_uri.decode("utf-8")
                if not re.fullmatch(r"usb:[0-9]+\.[0-9]+\.[0-9]+", uri):
                    continue
                context = lib.iio_create_context_from_uri(raw_uri)
                if not context:
                    unavailable.add(uri)
                    continue
                try:
                    if all(lib.iio_context_find_device(context, name) for name in (
                        b"ad9361-phy", b"cf-ad9361-lpc", b"cf-ad9361-dds-core-lpc"
                    )):
                        matches.add(uri)
                finally:
                    lib.iio_context_destroy(context)
        finally:
            if infos:
                lib.iio_context_info_list_free(infos)
            lib.iio_scan_context_destroy(scan)
        if unavailable:
            raise OSError("Cannot inspect USB device(s): " + ", ".join(sorted(unavailable))
                          + "; close other SDR applications or specify --uri")
        if not matches:
            raise OSError("No SDR Mini / Pluto-compatible USB panel found; check USB connection and driver")
        if len(matches) > 1:
            raise ValueError("Multiple SDR Mini / Pluto-compatible USB panels found: "
                             + ", ".join(sorted(matches)) + "; select one with --uri")
        return matches.pop()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="M17 FT-817 or half-duplex Pluto USB transceiver")
    parser.add_argument("--mode", choices=("ft817", "pluto"), default="ft817",
                        help="default: ft817; pluto starts RX; Space switches RX/TX")
    parser.add_argument("--uri", help="native USB URI override, e.g. usb:2.10.5; otherwise discover automatically")
    parser.add_argument("--audio-device", help="Pluto RX JBL output index or unique name")
    parser.add_argument("--callsign", help="Pluto source callsign")
    parser.add_argument("--can", type=int, choices=range(16), help="Pluto CAN 0..15")
    parser.add_argument("--libiio-dll", help="path to Windows libiio 0.25 DLL")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    cfg = load_config()
    if args.mode == "pluto":
        pluto = cfg.setdefault("pluto", {})
        for key, value in (("uri", args.uri), ("callsign", args.callsign),
                           ("can", args.can), ("libiio_dll", args.libiio_dll)):
            if value is not None:
                pluto[key] = value
        if args.audio_device is not None:
            pluto["rx_audio_device"] = args.audio_device
        if not pluto.get("uri"):
            try:
                pluto["uri"] = discover_pluto_usb(pluto["libiio_dll"])
            except (OSError, ValueError, RuntimeError, AttributeError, KeyError) as exc:
                print(f"Pluto USB discovery error: {exc}", file=sys.stderr)
                return 1
            print(f"SDR Mini / Pluto USB detected: {pluto['uri']}")
        from pluto_duplex import DuplexControl
        return DuplexControl(cfg, rx_factory=pluto_rx_display_session).run()

    restore_audio_levels(cfg)

    trx = M17TRX(cfg)

    try:
        trx.run()
    except KeyboardInterrupt:
        trx.shutdown()
    except Exception as e:
        try:
            trx.shutdown()
        finally:
            print(e)


if __name__ == "__main__":
    sys.exit(main())
