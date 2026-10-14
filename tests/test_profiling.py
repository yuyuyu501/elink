import asyncio
from concurrent.futures import ThreadPoolExecutor
import math
from types import SimpleNamespace

import av
import numpy as np
import pytest
from aiohttp import web
from aiortc.jitterbuffer import JitterFrame
from aiortc.mediastreams import MediaStreamError

from elink.core.buffers import DecodeQueue, LatestQueue
from elink.core.media import FrameMailbox, bgr_pixels
from elink.core.profiling import PipelineProfile
from elink.core.transport import Client, HostServer
from elink.models import ValidationError


def test_profile_bounded_quantiles_invalid_samples_and_independent_sessions():
    profile = PipelineProfile(capacity=100)
    for value in range(200):
        profile.observe('decode', value)
    for value in (-1, math.nan, math.inf):
        profile.observe('decode', value)
    stage = profile.snapshot()['stages']['decode']
    assert stage == dict(samples=100, total=200, p50_ms=149, p95_ms=194, p99_ms=198, max_ms=199)
    assert not PipelineProfile().snapshot()['stages']
    for index in range(40):
        profile.observe(str(index), 1)
    assert len(profile.snapshot()['stages']) == 32
    for value in (0, -1, True, 1.5):
        with pytest.raises(ValueError):
            PipelineProfile(value)


def test_profile_thread_safety_clock_and_snapshot_isolation():
    now = [10.0]
    profile = PipelineProfile(clock=lambda: now[0])
    profile.mark('interval')
    now[0] += .02
    profile.mark('interval')
    profile.elapsed('work', 10)
    def record(_):
        for _ in range(100):
            profile.observe('thread', 2)
            profile.count('frames')
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(record, range(4)))
    snapshot = profile.snapshot()
    assert snapshot['stages']['interval']['p50_ms'] == 20
    assert snapshot['stages']['work']['p50_ms'] == 20
    assert snapshot['counters']['frames'] == snapshot['stages']['thread']['total'] == 400
    snapshot['counters']['frames'] = 0
    assert profile.snapshot()['counters']['frames'] == 400


def test_receive_metrics_count_overflow_and_mailbox_drops():
    profile = PipelineProfile()
    queue = DecodeQueue(True, lambda: None, profile)
    for stamp in range(8):
        queue.put((None, JitterFrame(b'\x00\x00\x01\x41\x01', stamp)))
    keyframe = JitterFrame(b'\x00\x00\x01\x65\x01', 10)
    queue.put((None, keyframe))
    assert queue.get()[1] is keyframe and keyframe._elink_profile is profile
    assert profile.snapshot()['counters']['decode_queue_dropped'] == 8
    mailbox = FrameMailbox(profile)
    mailbox.put('old')
    mailbox.put('new')
    assert mailbox.take() == 'new' and mailbox.take() is None
    assert profile.snapshot()['counters']['mailbox_dropped'] == 1
    assert profile.snapshot()['stages']['mailbox_wait']['total'] == 1
    async def decoded():
        decoded_queue = LatestQueue(maxsize=1)
        decoded_queue.profile = profile
        for i in range(10):
            await decoded_queue.put(i)
        assert await decoded_queue.get() == 9
    asyncio.run(decoded())
    assert profile.snapshot()['counters']['decoded_queue_dropped'] == 9


@pytest.mark.parametrize('fmt', ['yuv420p', 'nv12', 'yuvj420p'])
def test_gpu_consumer_preserves_av_frame_planes_without_rgb_conversion(tmp_path, monkeypatch, fmt):
    from elink.core import transport
    frame = av.VideoFrame(642, 362, fmt)
    class Track:
        kind = 'video'
        received = False
        async def recv(self):
            if self.received:
                raise MediaStreamError()
            self.received = True
            return frame
    def unexpected(*args, **kwargs):
        raise AssertionError('GPU path must not convert to BGR')
    monkeypatch.setattr(transport, 'bgr_pixels', unexpected)
    client = Client(tmp_path, play_audio=False)
    client.set_renderer('gpu')
    asyncio.run(client.consume(Track()))
    assert client.mailbox.take() is frame
    assert client.profile.snapshot()['metadata']['renderer'] == 'opengl-yuv'


@pytest.mark.parametrize('space,full', [(5, False), (1, False), (5, True), (1, True)])
def test_bgr_conversion_and_gpu_parameters_respect_yuv_metadata(space, full):
    from elink.ui.gpu_video import color_parameters
    frame = av.VideoFrame(8, 8, 'yuv420p')
    # Known constant luma/chroma: avoids relying on swscale to generate its own
    # expected result, and exercises full-range metadata on ordinary yuv420p.
    for plane, value in zip(frame.planes, (100, 80, 190)):
        plane.update(bytes([value]) * plane.buffer_size)
    frame.colorspace, frame.color_range = space, 2 if full else 1
    offset, gain, cgain, (rv, gu, gv, bu) = color_parameters(frame)
    y, u, v = (100 / 255 - offset) * gain, (80 - 128) / 255 * cgain, (190 - 128) / 255 * cgain
    reference = np.clip(np.array([y + bu*u, y + gu*u + gv*v, y + rv*v]) * 255, 0, 255)
    assert np.max(np.abs(bgr_pixels(frame)[2, 2].astype(float) - reference)) <= 3
    frame.colorspace = 9  # BT.2020 must fall back, not silently use BT.601.
    with pytest.raises(ValueError):
        color_parameters(frame)


def test_performance_endpoint_requires_current_session_and_owner(tmp_path):
    host = HostServer(tmp_path, synthetic=True)
    host.pc, host.device_id, host.session_id = object(), '127.0.0.1', 'session'
    host.profile.observe('encode', 1.5)
    async def exercise():
        for address, session in [('127.0.0.2', 'session'), ('127.0.0.1', 'old')]:
            with pytest.raises(web.HTTPForbidden):
                await host.performance(SimpleNamespace(remote=address, match_info={'id': session}))
        response = await host.performance(SimpleNamespace(remote='127.0.0.1', match_info={'id': 'session'}))
        assert response.status == 200
        host.pc = None
        with pytest.raises(web.HTTPForbidden):
            await host.performance(SimpleNamespace(remote='127.0.0.1', match_info={'id': 'session'}))
    asyncio.run(exercise())


def test_report_old_peer_failure_and_session_change(tmp_path, monkeypatch):
    from elink.core import transport
    client = Client(tmp_path, play_audio=False)
    client.session_id = 'first'
    client.fp = None
    async def exercise():
        report = await client.performance_report()
        assert report['host'] is None and report['host_error'] is None
        client.performance_supported = True
        async def failed(*args):
            raise OSError('unreachable')
        monkeypatch.setattr(transport, 'request', failed)
        report = await client.performance_report()
        assert report['host_error'] == 'OSError' and report['client']
        async def changed(*args):
            client.session_id = 'second'
            return {}
        monkeypatch.setattr(transport, 'request', changed)
        with pytest.raises(ValidationError):
            await client.performance_report()
    asyncio.run(exercise())
