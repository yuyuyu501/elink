"""Measure real loopback capture/encode/transport/decode rates, optionally Qt output.

No desktop images, input events or pairing identities are saved in the report.
Both peers share this machine's resources; this is not a WAN/game acceptance test.
"""
import argparse
import asyncio
import json
import multiprocessing
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace

import numpy as np

from elink.core import transport
from elink.core.input import RecordingInput
from elink.core.media import DesktopTrack


def motion_track(width, height):
    """Deterministic panning texture: exercises bitrate, unlike a flat test card."""
    rng = np.random.default_rng(20261015)
    tile = rng.integers(0, 256, (max(1, height // 4), max(1, width // 4), 3), dtype=np.uint8)
    pixels = np.repeat(np.repeat(tile, 4, axis=0), 4, axis=1)
    pixels = np.resize(pixels, (height, width, 3))

    class MotionTrack(DesktopTrack):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.synthetic = False
            self.cursor_mode = 'local'
            self.cursor = SimpleNamespace(visible=lambda: False, composite=lambda frame, origin: frame)
            self.camera = SimpleNamespace(
                get_latest_frame=lambda: np.roll(pixels, self.frame_count * 4, axis=1),
                release=lambda: None,
                _output=SimpleNamespace(desc=SimpleNamespace(
                    DesktopCoordinates=SimpleNamespace(left=0, top=0))))
    return MotionTrack


def host_worker(connection, root, source, width, height):
    if source == 'motion':
        transport.DesktopTrack = motion_track(width, height)
    async def run():
        host = transport.HostServer(root, synthetic=source != 'desktop', backend_factory=RecordingInput,
                                    notify=lambda message: print('HOST: ' + message, flush=True))
        try:
            await host.start('127.0.0.1', 0)
            connection.send(('port', host.port))
            while True:
                command = await asyncio.to_thread(connection.recv)
                if command == 'stop':
                    break
                if command == 'profile':
                    connection.send(('profile', peer_snapshot(host)))
        finally:
            await host.stop()
    try:
        asyncio.run(run())
    except Exception as exc:
        connection.send(('error', str(exc)))
    finally:
        connection.close()


class SeparateHost:
    """Separate Python GIL, but still shares the controller's CPU/GPU and NIC."""
    def __init__(self, root, args):
        context = multiprocessing.get_context('spawn')
        self.connection, child = context.Pipe()
        self.child_connection = child
        self.process = context.Process(target=host_worker,
                                       args=(child, root, args.source, args.width, args.height))
        self.profile = SimpleNamespace(snapshot=lambda: self.read('profile'))
        self.port = None

    def read(self, command=None):
        if command:
            self.connection.send(command)
        if not self.connection.poll(20):
            raise TimeoutError('Benchmark host did not respond')
        kind, value = self.connection.recv()
        if kind == 'error':
            raise RuntimeError(value)
        return value

    async def start(self, *args):
        self.process.start()
        self.child_connection.close()
        self.port = await asyncio.to_thread(self.read)
        return self.port

    async def stop(self):
        try:
            if self.process.pid is not None:
                if self.process.is_alive():
                    self.connection.send('stop')
                    await asyncio.to_thread(self.process.join, 10)
                if self.process.is_alive():
                    self.process.terminate()
                    await asyncio.to_thread(self.process.join, 5)
                    raise RuntimeError('Benchmark host required forced cleanup')
        finally:
            self.connection.close()
            self.child_connection.close()


def peer_snapshot(peer):
    report = peer.profile.snapshot()
    pc = peer.pc
    report['connection_state'] = pc.connectionState if pc else 'disconnected'
    return report


def result(args, before, after, seconds, refresh=None):
    rates = {}
    for name, keys in [('host', ('captured_frames', 'encoded_frames', 'encoded_bytes')),
                       ('client', ('decoded_frames', 'presented_frames', 'decode_errors',
                                   'decoded_queue_dropped', 'mailbox_dropped'))]:
        for key in keys:
            count = after[name]['counters'].get(key, 0) - before[name]['counters'].get(key, 0)
            rates[key + '_per_second'] = round(count / seconds, 3)
    byte_rate = rates.pop('encoded_bytes_per_second')
    rates['encoded_mbps'] = (round(byte_rate * 8 / 1e6, 3)
                             if 'encoded_bytes' in after['host']['counters'] else None)
    if 'captured_frames' not in after['host']['counters']:
        rates['captured_frames_per_second'] = None
    if not args.present:
        rates.pop('presented_frames_per_second')
        rates.pop('mailbox_dropped_per_second')  # No GUI consumer in headless mode.
    return dict(config={k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
                measured_seconds=seconds, display_hz=refresh, rates=rates, **after,
                limitations='Local shared-resource loopback. Submission is not physical scanout; '
                             'desktop captures may repeat. No claim about WAN or gaming performance.')


def presentation_issues(window):
    """Do not mistake an unexposed Qt window for a slow video pipeline."""
    return [name for name, bad in (
        ('hidden', not window['visible']), ('minimized', window['minimized']),
        ('unexposed', not window['exposed'])) if bad]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', choices=('motion', 'desktop', 'plain'), default='motion')
    parser.add_argument('--fps', type=int, default=90)
    parser.add_argument('--width', type=int, default=1920)
    parser.add_argument('--height', type=int, default=1080)
    parser.add_argument('--bitrate', type=int, default=20)
    parser.add_argument('--seconds', type=float, default=10)
    parser.add_argument('--warmup', type=float, default=3)
    parser.add_argument('--renderer', choices=('gpu', 'cpu'), default='gpu')
    parser.add_argument('--decoder', choices=('hardware', 'software', 'auto'), default='hardware')
    parser.add_argument('--present', action='store_true')
    parser.add_argument('--separate-host', action='store_true', help='Use a separate process for the host')
    parser.add_argument('--output', type=Path, default=Path('.artifacts/stream-benchmark.json'))
    args = parser.parse_args(argv)
    if not (0 < args.seconds <= 300 and 0 <= args.warmup <= 60):
        parser.error('Require 0 < seconds <= 300 and 0 <= warmup <= 60')
    config = transport.settings(dict(width=args.width, height=args.height, fps=args.fps,
                                     bitrate=args.bitrate, audio=False))
    original_track = transport.DesktopTrack
    if args.source == 'motion':
        transport.DesktopTrack = motion_track(args.width, args.height)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix='stream-bench-') as temp:
            root = Path(temp)
            host = (SeparateHost(root / 'host', args) if args.separate_host else
                    transport.HostServer(root / 'host', synthetic=args.source != 'desktop',
                                         backend_factory=RecordingInput))
            client = transport.Client(root / 'client', play_audio=False,
                                      notify=lambda message: print('CLIENT: ' + message, flush=True))
            client.set_renderer(args.renderer)

            async def connect():
                await host.start('127.0.0.1', 0)
                await client.connect(f'127.0.0.1:{host.port}', config, args.decoder)

            async def close():
                await client.disconnect()
                await host.stop()

            def snapshot():
                return dict(host=host.profile.snapshot(), client=client.profile.snapshot())

            if args.present:
                from PySide6.QtCore import QTimer, Qt
                from PySide6.QtWidgets import QApplication
                from elink.core.runtime import Runtime
                from elink.ui.player import Player
                from elink.ui.theme import apply_theme
                app = QApplication.instance() or QApplication([])
                apply_theme(app)
                runtime = Runtime()
                state = {}

                def window_state():
                    view = state['view']
                    surface = view.gpu_surface
                    handle = view.windowHandle()
                    return dict(visible=view.isVisible(), minimized=view.isMinimized(),
                                exposed=bool(handle and handle.isExposed()),
                                active=view.isActiveWindow(),
                                application_state=app.applicationState().name,
                                surface_visible=surface.isVisible() if surface else None,
                                surface_updates=surface.updatesEnabled() if surface else None,
                                surface_valid=surface.isValid() if surface else None,
                                surface_pending=surface.pending if surface else None,
                                surface_broken=surface.broken if surface else None,
                                error=view.error_label.text())

                def finish():
                    check_window()
                    state['result'] = result(args, state['before'], snapshot(),
                                             time.perf_counter() - state['start'],
                                             state['view'].screen().refreshRate())
                    state['result']['window'] = dict(before=state['window_before'], after=window_state())
                    state['result']['invalid_window_samples'] = state['invalid_window_samples']
                    state['result']['startup_counters'] = {name: value['counters']
                                                          for name, value in state['before'].items()}
                    state['view'].close()
                    app.quit()

                def warm():
                    state.update(before=snapshot(), start=time.perf_counter(), window_before=window_state(),
                                 invalid_window_samples=0)
                    check_window()
                    QTimer.singleShot(round(args.seconds * 1000), finish)

                def check_window():
                    if 'start' in state and presentation_issues(window_state()):
                        state['invalid_window_samples'] += 1

                visibility_timer = QTimer()
                visibility_timer.timeout.connect(check_window)
                visibility_timer.start(250)

                def connected(_):
                    view = Player(runtime, client, controllers=False, preferences={'renderer': args.renderer})
                    state['view'] = view
                    view.setWindowTitle('Elink streaming benchmark')
                    view.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
                    area = view.screen().availableGeometry()
                    view.resize(min(800, area.width()), min(490, area.height()))
                    view.move(area.topLeft())
                    view.show()
                    # Shell startup show-state can otherwise minimize a GUI
                    # child even though QWidget.show() was called.
                    QTimer.singleShot(200, view.showNormal)
                    state['connected_at'] = time.perf_counter()
                    await_first_frame()

                def await_first_frame():
                    # SDP completion is earlier than ICE/DTLS/media startup.
                    # Warm up a running pipeline, not an idle connection.
                    if client.profile.snapshot()['counters'].get('presented_frames', 0):
                        QTimer.singleShot(round(args.warmup * 1000), warm)
                    elif time.perf_counter() - state['connected_at'] > 20:
                        failed('No presented frame within 20 seconds of connection')
                    else:
                        QTimer.singleShot(50, await_first_frame)

                def failed(error):
                    state['error'] = error
                    app.quit()

                try:
                    runtime.submit(connect(), connected, failed)
                    app.exec()
                    if 'result' not in state:
                        raise RuntimeError(state.get('error', 'Benchmark window closed before completion'))
                    measured = state['result']
                finally:
                    try:
                        runtime.submit(close()).result(20)
                    finally:
                        runtime.stop()
            else:
                async def run():
                    try:
                        await connect()
                        deadline = time.perf_counter() + 20
                        while not client.profile.snapshot()['counters'].get('decoded_frames', 0):
                            if time.perf_counter() > deadline:
                                raise RuntimeError('No decoded frame within 20 seconds of connection')
                            await asyncio.sleep(.05)
                        await asyncio.sleep(args.warmup)
                        before, started = snapshot(), time.perf_counter()
                        await asyncio.sleep(args.seconds)
                        return result(args, before, snapshot(), time.perf_counter() - started)
                    finally:
                        await close()
                measured = asyncio.run(run())
            args.output.write_text(json.dumps(measured, indent=2), encoding='utf-8')
            print(json.dumps(measured['rates'], indent=2))
            print(f'Report: {args.output}')
            if measured.get('invalid_window_samples', 0):
                raise RuntimeError('Invalid presentation run: window hidden/minimized/unexposed')
            if measured['rates']['decoded_frames_per_second'] == 0:
                raise RuntimeError('No decoded frames during measurement')
            if args.present and measured['rates']['presented_frames_per_second'] == 0:
                raise RuntimeError('No video frames submitted during measurement; inspect window state')
    finally:
        transport.DesktopTrack = original_track


if __name__ == '__main__':
    main()
