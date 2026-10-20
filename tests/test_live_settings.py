import asyncio
from types import SimpleNamespace

import av
import numpy as np
import pytest

from elink.core import codecs
from elink.core.input import RecordingInput
from elink.core.media import LoopbackTrack
from elink.core.transport import Client, HostServer
from elink.models import ValidationError


def test_legacy_host_rejects_remote_changes_but_keeps_session_and_local_settings(tmp_path):
    async def run():
        host = HostServer(tmp_path / 'host', synthetic=True, backend_factory=RecordingInput)
        info = host.host_info
        host.host_info = lambda: dict(info(), features=['cursor-modes-v1'])
        client = Client(tmp_path / 'client', play_audio=False)
        try:
            port = await host.start('127.0.0.1', 0)
            await client.connect(f'127.0.0.1:{port}', dict(width=640, height=360, audio=False), 'software')
            pc, session = client.pc, client.session_id
            with pytest.raises(ValidationError, match='不支持会话内'):
                await client.reconfigure(dict(client.config, bitrate=10), 'software')
            assert client.config['bitrate'] == 20
            await client.reconfigure(client.config, 'software', 'gpu')
            assert client.renderer == 'gpu'
            assert client.pc is pc and client.session_id == session == host.session_id
        finally:
            await client.disconnect()
            await host.stop()
    asyncio.run(run())


def test_failed_decoder_switch_keeps_existing_decoder(monkeypatch):
    decoder = codecs.DesktopDecoder(codecs.CodecPolicy(decoder='software'))
    frame = av.VideoFrame.from_ndarray(np.full((360, 640, 3), 100, dtype=np.uint8), format='rgb24')
    encoder = codecs.make_encoder('libx264', 640, 360, codecs.CodecPolicy())
    packet = b''.join(bytes(p) for p in encoder.encode(frame.reformat(format='yuv420p')))
    results = []
    encoded = SimpleNamespace(data=packet, timestamp=0, _elink_decoder_switch=('hardware', results.append))
    def failed(*args):
        raise RuntimeError('hardware unavailable')
    monkeypatch.setattr(codecs, 'DesktopDecoder', failed)
    frames = decoder.decode(encoded)
    assert len(frames) == 1 and not decoder.hardware and decoder.mode == 'software'
    assert len(results) == 1 and str(results[0]) == 'hardware unavailable'


def test_audio_toggle_preserves_timestamps_and_discards_old_samples():
    async def run():
        track = LoopbackTrack(synthetic=True, enabled=False)
        try:
            first = await track.recv()
            track.set_enabled(True)
            second = await asyncio.wait_for(track.recv(), 2)
            track._put(np.ones((960, 2), dtype=np.float32))
            track.set_enabled(False)
            third = await track.recv()
            assert first.pts < second.pts < third.pts
            assert not third.to_ndarray().any() and not track.frames
        finally:
            track.stop()
            await asyncio.to_thread(track.thread.join, 1)
    asyncio.run(run())
