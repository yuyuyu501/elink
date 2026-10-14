import asyncio
from types import SimpleNamespace

import numpy as np
import pytest

from elink.core.input import RecordingInput
from elink.core.media import DesktopTrack
from elink.core.transport import Client, HostServer, request, settings
from elink.models import ValidationError


@pytest.mark.parametrize('size', [(1280, 720), (1600, 900), (1920, 1080), (2560, 1440), (3840, 2160)])
def test_capture_resizes_pixels_without_changing_source_display(size):
    # A real-size fake desktop exercises conversion, not the synthetic source
    # which would already have the requested stream dimensions.
    pixels = np.full((1440, 2560, 3), 80, dtype=np.uint8)
    track = DesktopTrack(*size, 60)
    track.camera = SimpleNamespace(get_latest_frame=lambda: pixels, release=lambda: None,
                                   _output=SimpleNamespace(desc=SimpleNamespace(
                                       DesktopCoordinates=SimpleNamespace(left=0, top=0))))
    track.cursor = SimpleNamespace(visible=lambda: False)
    track.cursor_mode = 'local'
    try:
        frame = track.capture()
        assert (frame.width, frame.height) == size
        assert frame.format.name == 'yuv420p'
        assert pixels.shape == (1440, 2560, 3) and np.all(pixels == 80)
    finally:
        track.stop()
        track.release_future.result(timeout=5)


def test_old_follow_display_flag_cannot_override_requested_resolution():
    value = settings(dict(width=1280, height=720, follow_display=True))
    assert (value['width'], value['height']) == (1280, 720)
    assert value['follow_display'] is False


@pytest.mark.parametrize('width,height,fps', [(3840, 2160, 120), (2560, 1440, 165), (3842, 2160, 60)])
def test_resolution_rate_exceeding_codec_budget_is_rejected(width, height, fps):
    with pytest.raises(ValidationError):
        settings(dict(width=width, height=height, fps=fps))


def test_supported_high_resolution_rates():
    assert settings(dict(width=3840, height=2160, fps=60))['fps'] == 60
    assert settings(dict(width=2560, height=1440, fps=144))['fps'] == 144


@pytest.mark.parametrize('size', [(2560, 1440), (3840, 2160)])
def test_high_resolution_frames_cross_real_webrtc_connection(tmp_path, size):
    async def run():
        host = HostServer(tmp_path / 'host', synthetic=True, backend_factory=RecordingInput)
        client = Client(tmp_path / 'client', play_audio=False)
        try:
            port = await host.start('127.0.0.1', 0)
            await client.connect(f'127.0.0.1:{port}', dict(width=size[0], height=size[1], fps=30, audio=False))
            from test_independent import until
            await until(lambda: client.mailbox.received >= 3)
            assert client.mailbox.take().shape == (size[1], size[0], 3)
        finally:
            await client.disconnect()
            await host.stop()
    asyncio.run(run())


def test_retired_display_api_rejects_old_clients_and_session_still_streams(tmp_path):
    async def run():
        host = HostServer(tmp_path / 'host', synthetic=True, backend_factory=RecordingInput)
        client = Client(tmp_path / 'client', play_audio=False)
        try:
            port = await host.start('127.0.0.1', 0)
            await client.connect(f'127.0.0.1:{port}', dict(width=640, height=360, audio=False))
            assert 'display-settings-v1' not in host.host_info()['features']
            for method in ('GET', 'POST'):
                with pytest.raises(ValidationError, match='已移除'):
                    await request(client.endpoint, client.fp, method,
                                  f'/v1/session/{client.session_id}/display',
                                  payload=dict(resolution=[1280, 720], scale=125, refresh=144))
            from test_independent import until
            await until(lambda: client.mailbox.received >= 3)
            assert client.mailbox.take().shape == (360, 640, 3)
        finally:
            await client.disconnect()
            await host.stop()
    asyncio.run(run())
