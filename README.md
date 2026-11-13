# M17 CLI Softmodem for SDR Mini 7010

A lightweight command-line framework for operating **M17 voice** with a
**Pluto-compatible SDR Mini 7010** under **Windows 11**, using the official
**M17-tools** applications as the modem engine.

The radio uses native USB IIO for reception and transmission, with
**Space-controlled half-duplex operation** from a single console.

## Background

This project grew out of a working M17 CLI framework for the Yaesu
FT-817 / FT-818. The SDR version connects the same external modem tools
to a Pluto-compatible Zynq-7010 board through software FM conversion
and native USB sample streaming.

M17-tools provides framing, coding, filtered 4-FSK baseband generation
and decoding. Python handles device configuration, audio, sample
conversion and RX/TX switching.

The current focus is a local **70 cm M17 voice radio**. The existing
hotspot provides an independent RF test endpoint.

## Tested Configuration

- Windows 11, x64 Python environment
- Pluto-compatible SDR Mini 7010 with Zynq-7010
- Native Windows **libiio 0.25** and a working USB driver
- M17-tools `m17-mod.exe` and `m17-demod.exe`
- SoX with Windows `waveaudio` support
- JBL TUNE 305C USB-C microphone and playback device
- Independent RTL-SDR / SDR++ receiver for TX observation
- Existing M17 hotspot and DroidStar for end-to-end speech checks

The board is used as a Pluto-compatible AD936x device. Its exact fitted
RF-chip variant has not been independently verified.

## Features

- Live USB reception with automatic signal detection and frequency-offset estimation
- Decoded speech playback through a configurable audio output
- Received source callsign, destination and stream information in the console
- Live microphone transmission through the external M17 modulator
- Space-controlled RX/TX switching with exclusive ownership of the board
- Orderly M17 closure, at least 200 ms of zero I/Q and verified TX LO shutdown
- Automatic discovery of a single compatible USB board, with URI override
- YAML configuration and command-line overrides
- Separate receive-only USB CLI
- Retained FT-817 / FT-818 operating mode

## Installation

### 1. Install M17-tools

Build or install [M17-tools](https://github.com/M17-Project/m17-tools)
and make `m17-mod.exe` and `m17-demod.exe` available.

Set their directory in `paths.m17_tools` in `src/m17_config.yaml`.
The local decoder used during validation reports version **2.2**.

### 2. Install SoX and libiio

Install SoX with the Windows `waveaudio` driver and set `paths.sox`.

Install the Windows x64 **libiio 0.25** runtime and configure
`pluto.libiio_dll`. The USB adapter checks this version at startup.
Verify that the board appears in the USB scan:

```powershell
& 'C:\libiio\Windows-VS-2022-x64\iio_info.exe' -S usb
```

### 3. Install Python packages

From the repository root, using a working Windows x64 Python environment:

```powershell
python -m pip install -r requirements-live.txt
```

The dependency file pins NumPy, SciPy, PyYAML and sounddevice.
For the retained FT-817 mode, also install:

```powershell
python -m pip install pyserial pycaw comtypes
```

## Radio Configuration

The current operating points are:

| Setting | RX | TX |
| --- | --- | --- |
| Sample rate | 4,000,000 samples/s | 2,304,000 samples/s |
| RF center / LO | 438,858,300 Hz | 438,800,000 Hz |
| Wanted channel | 438,800,000 Hz | 438,800,000 Hz |
| Gain control | Slow-attack AGC | -25 dB hardware gain |

The receiver tunes the wanted channel digitally from its separate RF
center frequency. It uses a 262,144-sample USB block and 200 kHz RF bandwidth.

The controller explicitly changes and checks the sample rate whenever
it opens an RX or TX session. Before transmission, the TX adapter also
checks the frequency, gain, disabled LO and sample layout. Prepare the
panel with the listed TX frequency and gain before starting a TX test.
A failed check stops the operation.

Use a dummy load or a suitably attenuated measurement path for initial
TX checks, with an independent receiver to observe the signal.

## Configuration

The CLI loads the published station configuration from:

```text
src/m17_config.yaml
```

Adjust these values for your station:

- `callsign`, or the Pluto-specific `pluto.callsign` override
- `pluto.can`, from 0 to 15
- `paths.m17_tools` and `paths.sox`
- `pluto.libiio_dll`
- `pluto.uri`, or leave it null for automatic USB discovery
- `pluto.tx_microphone.device_contains`
- `pluto.rx_audio_device`, using an output index or a unique device name

The remaining `audio`, `ptt` and `radio` settings configure FT-817 mode.
The supplied file contains the reference station's settings, including
its Windows paths and device names.

List audio devices before choosing the playback output:

```powershell
python .\src\m17_rx_usb.py --list-audio
```

Windows may list the same physical output through several host APIs.
A device index selects one entry explicitly. Recheck indices and USB URIs
after device reconnection.

## Transceiver Controls

Start the Pluto transceiver from the repository root:

```powershell
python .\src\m17_trx.py --mode pluto --audio-device 6 --callsign HA2ZB --can 0 --libiio-dll 'C:\libiio\Windows-VS-2022-x64\libiio.dll'
```

Replace the callsign and audio index with your own values. With a null
configured URI, the program discovers one compatible USB board. To select
a particular board, add `--uri usb:BUS.ADDRESS.INTERFACE` using the scan result.

| Key | Function |
| --- | --- |
| Space in RX | Close RX and start microphone TX |
| Space in TX | Close TX and return to RX |
| Space during a transition | Ignored |
| Esc / Ctrl+C | Request orderly shutdown and exit |

Operation sequence:

```text
Program start
    -> Open RX and verify 4 MS/s settings
    -> Receive and play decoded speech

Space
    -> Stop RX and release its USB buffer and connection
    -> Open TX and verify 2.304 MS/s settings
    -> Start microphone transmission

Space
    -> Stop microphone and deliver EOF to m17-mod
    -> Send remaining M17 samples and closing sequence
    -> Send at least 200 ms of zero I/Q and drain TX
    -> Switch TX LO off and verify readback
    -> Release TX USB connection
    -> Reopen RX

Esc / Ctrl+C
    -> Complete orderly closure of the active branch
    -> Exit
```

The console displays the current state and available controls. RX shutdown
must succeed before TX starts. Returning to RX requires successful TX
closure and release of its worker process. Errors trigger bounded emergency
shutdown with a separate LO-off attempt.

The microphone chain uses 2048-byte SoX buffers and 400 ms of initial PCM
buffering. The tested modulator scale is **+800/7168 Hz per PCM count**,
with `m17-mod` running without `-i` on the Pluto path.

## Receive-Only Operation

For independent USB reception, use the separate CLI:

```powershell
New-Item -ItemType Directory -Force .\receiver-output | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
python .\src\m17_rx_usb.py `
  --uri usb:2.11.5 --libiio-dll 'C:\libiio\Windows-VS-2022-x64\libiio.dll' `
  --sample-rate 4000000 --center-hz 438858300 --target-hz 438800000 `
  --audio-device 6 --demod 'C:\M17\m17-demod.exe' `
  --log ".\receiver-output\rx-$stamp.log"
```

Use the current URI and audio index. Add `--wav` with a new filename to
save decoded speech. Ctrl+C closes the receiver. Close other programs
using the board before opening either CLI.

The retained FT-817 mode starts with `python .\src\m17_trx.py --mode ft817`
and uses the configured audio interface, Windows audio levels and RTS PTT.

## Repository Structure

```text
src/
    m17_trx.py          Main CLI, configuration and received stream display
    m17_config.yaml    Station configuration
    pluto_duplex.py    Half-duplex state control
    pluto_live.py      Microphone TX worker and watchdog
    pluto_iio.py       Native USB TX adapter
    mini_iio_rx.py     Native USB RX adapter
    m17_rx_usb.py      Receive-only CLI, decoder and audio playback
    m17_rx_dsp.py      Receive DSP
    pcm_to_iq.py      Stateful FM conversion and offline converter CLI
requirements-live.txt
requirements-rx.txt
requirements-offline.txt
```

## Acknowledgements

This project builds on [M17-tools](https://github.com/M17-Project/m17-tools).
The [M17 specification](https://github.com/M17-Project/M17_spec) and
[gr-m17](https://github.com/M17-Project/gr-m17) supported the signal-path
review. The reviewed M17-tools source revision was
`806ec78e8249a474ab2f37eab9bfb55c9228456a`.

## Known Limitations

- Live hardware operation has been checked on Windows 11 with the local
  SDR Mini and audio devices. Other boards and drivers need validation.
- The operator confirmed USB/JBL reception, live microphone TX reception
  on an independent SDR, and speech received through the hotspot and
  reflector on DroidStar's phone speaker.
- A playback-queue failure was corrected using fixed 20 ms PCM blocks
  and bounded backpressure. The operator confirmed successful JBL reception
  afterward, with received stream information displayed.
- The full RX -> TX -> RX cycle in one CLI session, repeated switching,
  shutdown timing and sustained operation still need documented panel checks.
- The latest relevant hardware-free suite passed **49 tests**, covering
  RX/TX control, shutdown, DSP continuity and audio buffering.
- Initial frame recovery remains under investigation. Printed CRC fields
  carry the decoder's reporting limits. RF BER requires a known TX-bit reference.
- The receiver currently retains its first acquired frequency-offset
  estimate. Automatic tracking and reacquisition remain future work.
- The PCM scale was verified against the local modulator waveform. Its
  build commit and calibrated RF deviation have not been established.
- Pluto transmission currently uses the modulator's broadcast destination.
  The YAML destination field does not select a Pluto TX destination.

## License

Licensed under the **GNU Affero General Public License v3.0 (AGPL-3.0)**.
See [LICENSE](LICENSE) for details.

This is an independent project built around M17-tools. It was developed
by the author with iterative assistance from AI-based coding tools.
