import asyncio
from types import SimpleNamespace

import pytest

from elink.core.scaling import encoding_size
from elink.core.transport import HostServer, Client, settings
from elink.core.input import RecordingInput
from elink.models import ValidationError


@pytest.mark.parametrize('size,scale,expected', [
    ((1920, 1080), 67, (1280, 720)), ((3840, 2160), 67, (2560, 1440)),
    ((1920, 1080), 75, (1440, 810)), ((2560, 1440), 67, (1706, 960)),
    ((3840, 2160), 100, (3840, 2160)), ((1920, 1080), 50, (960, 540)),
    ((3840, 2160), 50, (1920, 1080))])
def test_encoded_size_and_codec_budget(size, scale, expected):
    config = settings(dict(width=size[0], height=size[1], render_scale=scale))
    assert encoding_size(config) == expected
    assert (config['width'], config['height']) == size
    assert config['follow_display'] is False


def test_validation_uses_encoded_macroblocks_without_silently_changing_fps():
    assert settings(dict(width=3840, height=2160, render_scale=67, fps=120))['fps'] == 120
    for scale in (True, 0, 66, 1.0, '67'):
        with pytest.raises(ValidationError):
            settings(dict(render_scale=scale))


@pytest.mark.parametrize('scale,size', [(75, (1440, 810)), (67, (1280, 720)), (50, (960, 540))])
def test_scaled_pixels_really_cross_webrtc_without_changing_output_dimensions(tmp_path, scale, size):
    async def run():
        host = HostServer(tmp_path / 'host', synthetic=True, backend_factory=RecordingInput)
        client = Client(tmp_path / 'client', play_audio=False)
        client.set_renderer('gpu')
        try:
            port = await host.start('127.0.0.1', 0)
            await client.connect(f'127.0.0.1:{port}', dict(width=1920, height=1080, render_scale=scale, audio=False))
            from test_independent import until
            await until(lambda: client.mailbox.received >= 3)
            frame = client.mailbox.take()
            assert (frame.width, frame.height) == size
            assert client.profile.snapshot()['metadata']['output_width'] == 1920
            assert host.profile.snapshot()['metadata']['encoded_width'] == size[0]
        finally:
            await client.disconnect()
            await host.stop()
    asyncio.run(run())


def test_old_host_and_cpu_renderer_cannot_silently_ignore_super_resolution(tmp_path, monkeypatch):
    from elink.core import transport
    async def fake_request(*args, **kwargs):
        return dict(protocol='elink', version=2, features=[])
    async def fingerprint(*args):
        return ''
    monkeypatch.setattr(transport, 'request', fake_request)
    monkeypatch.setattr(transport, 'fingerprint', fingerprint)
    client = Client(tmp_path, play_audio=False)
    async def run():
        with pytest.raises(ValidationError, match='GPU'):
            await client.connect('127.0.0.1', dict(render_scale=67))
        client.set_renderer('gpu')
        with pytest.raises(ValidationError, match='主机不支持'):
            await client.connect('127.0.0.1', dict(render_scale=67))
        assert client.pc is None
    asyncio.run(run())


def test_preferences_keep_scale_and_renderer_consistent():
    from PySide6.QtWidgets import QApplication
    from elink.ui.controls import StreamOptions
    app = QApplication.instance() or QApplication([])
    options = StreamOptions()
    options.render_scale.setCurrentIndex(options.render_scale.findData(50))
    options.upscaler.setCurrentIndex(options.upscaler.findData('fsr'))
    assert options.renderer.currentData() == 'gpu'
    restored = StreamOptions()
    restored.restore(options.values())
    assert restored.render_scale.currentData() == 50 and restored.renderer.currentData() == 'gpu'
    assert restored.upscaler.currentData() == 'fsr' and restored.upscaler.isEnabled()
    restored.renderer.setCurrentIndex(restored.renderer.findData('cpu'))
    assert restored.render_scale.currentData() == 100
    assert not restored.upscaler.isEnabled()
    options.deleteLater()
    restored.deleteLater()
    app.processEvents()
