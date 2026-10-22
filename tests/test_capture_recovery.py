import asyncio
import json
import os
from types import SimpleNamespace

import av
import pytest

from elink.core.media import DesktopTrack
from elink.platforms.windows.capture import desktop_unavailable


@pytest.mark.parametrize('exc', [PermissionError('desktop denied'), av.error.PermissionError(1, 'denied'),
                                __import__('comtypes').COMError(-2147024891, 'denied', None) if os.name == 'nt' else
                                PermissionError('unavailable')])
def test_native_desktop_unavailable_errors_are_recoverable(exc):
    assert desktop_unavailable(exc)
    assert not desktop_unavailable(RuntimeError('unsupported video processor'))
    assert not desktop_unavailable(av.error.InvalidDataError(22, 'invalid data'))
    external = av.error.ExternalError(542398533, 'external error')
    assert not desktop_unavailable(external)
    assert desktop_unavailable(external, reading=True)


@pytest.mark.skipif(os.name != 'nt', reason='Windows desktop switching')
@pytest.mark.parametrize('started', [False, True])
@pytest.mark.parametrize('external', [False, True])
def test_desktop_pause_recovers_without_disabling_gpu_or_reusing_timestamps(monkeypatch, started, external):
    from elink.core import gpu_capture, cursor
    denied, closed, states = [not started], [], []

    class Capture:
        def __init__(self, width, height, draw_mouse):
            self.draw_mouse = draw_mouse
        def read(self):
            if denied[0]:
                if external:
                    raise av.error.ExternalError(542398533, 'desktop switch')
                raise av.error.PermissionError(1, 'desktop switch')
            return av.VideoFrame(64, 36, 'nv12')
        def close(self):
            closed.append(True)

    monkeypatch.setattr(gpu_capture, 'D3D11Capture', Capture)
    monkeypatch.setattr(cursor, 'WindowsCursor', lambda: SimpleNamespace(visible=lambda: True))

    async def scenario():
        track = DesktopTrack(64, 36, 60, capture_backend='gpu')
        track.capture_state = lambda paused, exc=None: states.append(paused)
        try:
            first = await track.recv()
            denied[0] = True
            frames = [first]
            for _ in range(4):
                frames.append(await track.recv())
            assert track.capture_paused and not track.gpu_capture_disabled
            assert states == [True] and closed
            assert len(track.profile.snapshot()['failures']) == 1
            denied[0] = False
            for _ in range(4):
                frames.append(await track.recv())
                if not track.capture_paused:
                    break
            assert not track.capture_paused and states == [True, False]
            assert all(a.pts < b.pts and a is not b for a, b in zip(frames, frames[1:]))
            assert track.profile.snapshot()['counters']['capture_pause_frames'] >= 3
        finally:
            track.stop()
            track.release_future.result(5)

    asyncio.run(scenario())


@pytest.mark.skipif(os.name != 'nt', reason='Windows capture retry with real RTC transport')
def test_sixteen_second_desktop_denial_keeps_session_and_logs_both_roles(tmp_path, monkeypatch):
    from elink.core.input_session import RecordingInput
    from elink.core.transport import HostServer, Client
    from test_independent import until
    denied = [False]
    original = DesktopTrack._capture

    def capture(track):
        if denied[0]:
            raise av.error.PermissionError(1, 'simulated secure desktop')
        return original(track)

    monkeypatch.setattr(DesktopTrack, '_capture', capture)

    async def scenario():
        host = HostServer(tmp_path / 'host', synthetic=True, backend_factory=RecordingInput)
        client = Client(tmp_path / 'controller', play_audio=False)
        try:
            port = await host.start('127.0.0.1', 0)
            await client.connect(f'127.0.0.1:{port}', dict(width=640, height=360, fps=30, bitrate=2, audio=False))
            await until(lambda: client.mailbox.received >= 3)
            video = next(t for t in host.tracks if isinstance(t, DesktopTrack))
            # Simulate permission loss inside Windows capture, keeping the RTC
            # path real and the test independent of the interactive desktop.
            video.synthetic = False
            denied[0] = True
            await until(lambda: client.profile.snapshot()['metadata'].get('host_capture_paused'))
            pc, received = client.pc, client.mailbox.received
            await asyncio.sleep(16.1)
            assert client.pc is pc and host.pc and client.mailbox.received > received + 20
            for peer in (host, client):
                report = json.loads(peer.diagnostics.path.read_text(encoding='utf-8'))
                assert 'capture_paused' in report['reason']
            report = await client.performance_report()
            assert any(f['stage'] == 'capture_paused' for f in report['host']['failures'])
            assert any(f['stage'] == 'host_capture_paused' for f in report['client']['failures'])
            video.synthetic, denied[0] = True, False
            await until(lambda: not client.profile.snapshot()['metadata'].get('host_capture_paused'))
            await until(lambda: client.mailbox.received > received + 25)
            assert client.pc is pc
        finally:
            await client.disconnect()
            await host.stop()

    asyncio.run(scenario())
