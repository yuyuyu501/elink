import asyncio
import ctypes as C
from types import SimpleNamespace

import pytest

from elink.core.display import WindowsDisplay, DevMode, DisplayPath, Header, dpi_choices, stream_size
from elink.core.input import RecordingInput
from elink.core.transport import HostServer, Client, request
from elink.models import ValidationError


class FakeDisplay:
    def __init__(self):
        self.current = [1920, 1080]
        self.refresh = 60
        self.scale = 100
        self.changes = []

    def snapshot(self, device=None):
        return dict(device='test-display', current=list(self.current),
                    resolutions=[[2560, 1440], [1920, 1080], [1280, 720]],
                    display_modes=[[2560, 1440, 60], [1920, 1080, 60], [1920, 1080, 144], [1280, 720, 60]],
                    current_refresh=self.refresh, refresh_rates=[60, 144],
                    scale=self.scale, scales=[100, 125, 150], recommended_scale=100, scale_error='')

    def apply(self, change, device=None):
        self.changes.append(change)
        self.current = change['resolution']
        self.refresh = change.get('refresh', 60)
        self.scale = change['scale']
        return self.snapshot()


def test_windows_display_abi_and_dpi_relative_ranges():
    assert C.sizeof(DevMode) == 220
    assert C.sizeof(DisplayPath) == 72 and C.sizeof(Header) == 20
    assert dpi_choices(-2, 1, 3) == ([100, 125, 150, 175, 200, 225], 175, 150)
    for invalid in [(1, 1, 2), (-20, 0, 0), (-2, 99, 100)]:
        with pytest.raises(ValidationError):
            dpi_choices(*invalid)


@pytest.mark.parametrize('source,expected', [((2560, 1440), (1920, 1080)),
    ((1920, 1200), (1728, 1080)), ((1080, 1920), (606, 1080)), ((800, 600), (800, 600))])
def test_stream_preserves_display_aspect_without_exceeding_encoder_budget(source, expected):
    assert stream_size(*source) == expected


class NativeHarness(WindowsDisplay):
    """Exercise real apply/rollback logic without changing the test machine."""
    def __init__(self, *, reject_mode=False, fail_scale=False):
        self.current = [1920, 1080]
        self.scale = 100
        self.calls = []
        self.fail_scale = fail_scale
        def change(device, ptr, _, flags, unused):
            mode = C.cast(ptr, C.POINTER(DevMode)).contents
            self.calls.append((mode.width, mode.height, flags))
            if flags == 2:
                return -2 if reject_mode else 0
            self.current = [mode.width, mode.height]
            return 0
        self.user = SimpleNamespace(ChangeDisplaySettingsExW=change)

    def snapshot(self, device=None):
        return dict(device='test-display', current=list(self.current),
                    resolutions=[[1920, 1080], [1280, 720]],
                    display_modes=[[1920, 1080, 60], [1280, 720, 60]],
                    current_refresh=60, refresh_rates=[60],
                    scale=self.scale, scales=[100, 125])

    def mode(self, device, index=0xffffffff):
        return DevMode(width=self.current[0], height=self.current[1], frequency=60)

    def modes(self, device):
        return [DevMode(width=1280, height=720, frequency=60)]

    def set_scale(self, device, scale):
        if self.fail_scale and scale == 125:
            raise ValidationError('DPI rejected')
        self.scale = scale


def test_native_apply_validates_before_mutation_and_rolls_back_partial_failure():
    backend = NativeHarness()
    for change in [dict(device='other'), dict(device='test-display', resolution=[9999, 9999]),
                   dict(device='test-display', scale=500), dict(device='test-display', resolution=[True, 720])]:
        with pytest.raises(ValidationError):
            backend.apply(change)
        assert not backend.calls
    change = dict(device='test-display', resolution=[1280, 720], scale=125)
    result = backend.apply(change)
    assert result['current'] == [1280, 720] and result['scale'] == 125
    assert backend.calls == [(1280, 720, 2), (1280, 720, 0)]
    failed = NativeHarness(fail_scale=True)
    with pytest.raises(ValidationError, match='已恢复'):
        failed.apply(change)
    assert failed.current == [1920, 1080] and failed.scale == 100
    rejected = NativeHarness(reject_mode=True)
    with pytest.raises(ValidationError, match='原显示设置未改变'):
        rejected.apply(change)
    assert rejected.current == [1920, 1080] and rejected.calls == [(1280, 720, 2)]


def test_display_api_bound_to_active_session_and_old_host_rejected(tmp_path):
    async def run():
        display = FakeDisplay()
        host = HostServer(tmp_path / 'host', synthetic=True, backend_factory=RecordingInput, display_backend=display)
        client = Client(tmp_path / 'client', play_audio=False)
        try:
            port = await host.start('127.0.0.1', 0)
            await client.connect(f'127.0.0.1:{port}', dict(width=640, height=360, audio=False))
            assert (await client.display_settings())['scales'] == [100, 125, 150]
            with pytest.raises(ValidationError):
                await request(client.endpoint, client.fp, 'POST', '/v1/session/not-owner/display',
                              payload=dict(device='test-display', resolution=[1280, 720], scale=125))
            assert not display.changes
            result = await client.display_settings(dict(device='test-display', resolution=[1280, 720], scale=125))
            assert result['current'] == [1280, 720] and result['scale'] == 125
            assert len(display.changes) == 1
            client.display_supported = False
            with pytest.raises(ValidationError, match='升级'):
                await client.display_settings()
            await client.disconnect()
            assert display.current == [1920, 1080] and display.scale == 100
            with pytest.raises(ValidationError, match='断开'):
                await client.display_settings()
        finally:
            await client.disconnect()
            await host.stop()
    asyncio.run(run())


def test_display_reconfigure_can_keep_new_mode(tmp_path):
    async def run():
        display = FakeDisplay()
        host = HostServer(tmp_path / 'host', synthetic=True, backend_factory=RecordingInput, display_backend=display)
        client = Client(tmp_path / 'client', play_audio=False)
        try:
            port = await host.start('127.0.0.1', 0)
            await client.connect(f'127.0.0.1:{port}', dict(width=640, height=360, audio=False))
            await client.display_settings(dict(device='test-display', resolution=[1280, 720], scale=125))
            await client.disconnect(restore_display=False)
            assert display.current == [1280, 720] and display.scale == 125
        finally:
            await client.disconnect()
            await host.stop()
    asyncio.run(run())


def test_display_change_releases_capture_on_its_worker_and_recovers_after_error():
    from elink.core.media import DesktopTrack
    import threading
    async def run():
        track = DesktopTrack(640, 360, 30, synthetic=True)
        events = []
        track.camera = SimpleNamespace(release=lambda: events.append(('release', threading.get_ident())))
        track.last = object()
        def fail():
            events.append(('apply', threading.get_ident()))
            assert track.camera is None and track.last is None
            raise ValidationError('mode rejected')
        try:
            with pytest.raises(ValidationError):
                await track.change_display(fail)
            frame = await track.recv()
            assert frame.width == 640
            assert events[0][0] == 'release' and events[1][0] == 'apply'
            assert events[0][1] == events[1][1] != threading.get_ident()
        finally:
            track.stop()
            await asyncio.wrap_future(track.release_future)
    asyncio.run(run())
