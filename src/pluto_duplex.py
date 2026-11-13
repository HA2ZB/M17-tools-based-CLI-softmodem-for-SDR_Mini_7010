"""Exclusive, process-isolated native USB RX/TX control."""
import os
from pathlib import Path
import signal
from types import SimpleNamespace
import uuid

from pluto_live import LiveSession


def rx_worker(cfg, stop, connection, armed):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        if not armed.wait(5):
            return
        from m17_rx_usb import receive
        p = cfg['pluto']
        directory = Path('receiver-output')
        directory.mkdir(exist_ok=True)
        a = SimpleNamespace(uri=p.get('uri'), libiio_dll=p['libiio_dll'],
                            sample_rate=4000000, center_hz=438858300,
                            target_hz=438800000, block_size=262144, detect_ratio=0.25,
                            audio_device=str(p.get('rx_audio_device', 'JBL TUNE 305C USB-C')),
                            demod=os.path.join(cfg['paths']['m17_tools'], 'm17-demod.exe'),
                            wav=None, log=str(directory / ('duplex-' + uuid.uuid4().hex + '.log')))
        receive(a, stop=stop, report=connection.send)
        # receive returned only after buffer/context, decoder and audio close.
        connection.send('DONE')
    except BaseException as exc:
        connection.send('ERROR: ' + str(exc))
    finally:
        connection.close()


def rx_session(cfg):
    return LiveSession(cfg, worker_target=rx_worker)


class DuplexControl:
    def __init__(self, cfg, rx_factory=rx_session, tx_factory=LiveSession):
        self.cfg = cfg
        cfg['pluto']['half_duplex'] = True
        self.rx_factory, self.tx_factory = rx_factory, tx_factory
        self.session = None
        self.state = 'RX_STARTING'
        self.exiting = self.failed = False

    def screen(self):
        print('Pluto state:', self.state)
        if self.exiting or self.failed:
            print('SPACE ignored; ESC / Ctrl+C: orderly exit pending')
        elif self.state == 'RX':
            print('SPACE: stop RX and start microphone TX; ESC / Ctrl+C: exit')
        elif self.state == 'TX':
            print('SPACE: close M17 TX and return to RX; ESC / Ctrl+C: close TX and exit')
        else:
            print('SPACE ignored during transition; ESC / Ctrl+C: orderly stop and exit')

    def start(self):
        self.session = self.rx_factory(self.cfg)
        self.screen()

    def toggle(self):
        if not self.exiting and self.state in ('RX', 'TX'):
            self.state = 'TX_STARTING' if self.state == 'RX' else 'RETURNING_RX'
            self.session.request_stop()
        self.screen()

    def shutdown(self):
        self.exiting = True
        if self.session:
            self.session.request_stop()
        self.screen()

    def tick(self):
        if not self.session:
            return
        if self.session.poll():
            self.session = None
            if self.exiting:
                self.state = 'STOPPED'
            elif self.state == 'TX_STARTING':
                self.session = self.tx_factory(self.cfg)
            elif self.state == 'RETURNING_RX':
                self.state = 'RX_STARTING'
                self.session = self.rx_factory(self.cfg)
            else:
                raise OSError('Unexpected branch completion')
            self.screen()
        elif not self.exiting:
            ready = self.session.state
            if self.state == 'RX_STARTING' and ready == 'RX':
                self.state = 'RX'
                self.screen()
            elif self.state == 'TX_STARTING' and ready == 'TX':
                self.state = 'TX'
                self.screen()

    def run(self):
        import msvcrt
        import time
        try:
            self.start()
        except KeyboardInterrupt:
            self.shutdown()
        except Exception as exc:
            self.fail(exc)
        while self.session or not self.exiting:
            try:
                # Read keys before poll: exit takes precedence over reopening RX.
                if msvcrt.kbhit():
                    key = msvcrt.getch()
                    if key == b' ':
                        self.toggle()
                    elif key in (b'\x1b', b'\x03'):
                        self.shutdown()
                self.tick()
                time.sleep(0.02)
            except KeyboardInterrupt:
                self.shutdown()
            except Exception as exc:
                self.fail(exc)
        return int(self.failed)

    def fail(self, exc):
        print('Pluto duplex error:', exc)
        if self.session and not getattr(self.session, 'cleaned', False):
            try:
                self.session.emergency()
            except Exception as cleanup:
                print('Pluto emergency:', cleanup)
        self.session = None
        self.failed = self.exiting = True
        self.state = 'ERROR'
        self.screen()
