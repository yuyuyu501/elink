import asyncio
import os
from types import SimpleNamespace

import numpy as np
import av
import pytest
from aiortc.mediastreams import MediaStreamError

from elink.core.media import DesktopTrack
from elink.core.pacing import FrameClock, wait_until
from elink.core.profiling import PipelineProfile
from elink.core.sending import EncodeAhead, install_video_pipeline


def test_frame_clock_keeps_absolute_cadence_and_skips_missed_slots():
    now, sleeps = [0.0], []
    def sleep(delay):
        sleeps.append(delay)
        now[0] += delay
    clock = FrameClock(90, lambda: now[0], sleep)
    assert clock.wait() == (0, 0)
    for index in range(1, 20):
        now[0] += .003  # Conversion/encoding does not add to the frame interval.
        assert clock.wait()[1] == 0
        assert now[0] == pytest.approx(index / 90)
    now[0] += .1
    late, skipped = clock.wait()
    assert skipped >= 7
    assert now[0] < clock.deadline <= now[0] + 1 / 90 + 1e-9
    clock.wait()
    assert sleeps[-1] > 0  # No burst of missed frames to catch up.


def test_wait_until_handles_early_wake_and_expired_deadline():
    now = [0.0]
    waits = []
    def sleep(delay):
        waits.append(delay)
        now[0] += delay / 2 if len(waits) == 1 else delay
    wait_until(.01, lambda: now[0], sleep)
    assert waits == pytest.approx([.01, .005])
    wait_until(.005, lambda: now[0], sleep)
    assert len(waits) == 2


def test_capture_waits_off_event_loop_and_pts_follows_completed_capture():
    import threading
    track = DesktopTrack(64, 36, 90, synthetic=True)
    calls = []
    original = track.frame_clock.wait
    def wait():
        calls.append(threading.get_ident())
        return original()
    track.frame_clock.wait = wait
    async def run():
        main = threading.get_ident()
        frames = [await track.recv() for _ in range(4)]
        assert all(thread != main for thread in calls)
        pts = [f.pts for f in frames]
        assert all(a < b for a, b in zip(pts, pts[1:]))
        track.stop()
        with pytest.raises(MediaStreamError):
            await track.recv()
    try:
        asyncio.run(run())
    finally:
        track.stop()
        track.release_future.result(timeout=5)


@pytest.mark.parametrize('channels', [3, 4])
@pytest.mark.skipif(os.name != 'nt', reason='DXCam RGB/BGRA capture buffers')
def test_capture_buffer_lifetime_stride_and_channel_order(channels):
    pixels = np.zeros((40, 132, channels), np.uint8)
    pixels[:, :, :3] = (20, 90, 190)
    if channels == 4:
        pixels[:, :, 3] = 17
    # Negative/non-contiguous strides must not corrupt borrowed PyAV buffers.
    source = pixels[::-1, ::2]
    track = DesktopTrack(66, 40, 60)
    track.cursor_mode = 'local'
    track.cursor = SimpleNamespace(visible=lambda: False)
    track.camera = SimpleNamespace(get_latest_frame=lambda: source, release=lambda: None,
                                   _output=SimpleNamespace(desc=SimpleNamespace(
                                       DesktopCoordinates=SimpleNamespace(left=0, top=0))))
    try:
        frame = track.capture()
        source[:] = 0
        colors = frame.to_ndarray(format='bgr24')[10, 10].astype(int)
        assert np.max(np.abs(colors - [20, 90, 190])) <= 4
    finally:
        track.stop()
        track.release_future.result(timeout=5)


def test_encode_ahead_overlaps_sending_is_bounded_and_keeps_order():
    async def run():
        calls = []
        ready = asyncio.Event()
        async def encode(codec):
            calls.append(len(calls))
            ready.set()
            return calls[-1]
        pipeline = EncodeAhead(encode, PipelineProfile())
        assert await pipeline.get('h264') == 0
        ready.clear()
        await ready.wait()  # Second encode runs while caller is "sending" first.
        assert calls == [0, 1]
        for _ in range(10):
            await asyncio.sleep(0)
        assert calls == [0, 1]  # A blocked sender cannot grow the queue.
        for index in range(1, 6):
            assert await pipeline.get('h264') == index
        await pipeline.close()
        assert pipeline.task is None
        with pytest.raises(MediaStreamError):
            await pipeline.get('h264')
    asyncio.run(run())


def test_encode_ahead_cancellation_and_background_error_are_collected():
    async def run():
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        calls = []
        async def encode(codec):
            calls.append(codec)
            if len(calls) == 1:
                return 0
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
        pipeline = EncodeAhead(encode, PipelineProfile())
        await pipeline.get('h264')
        await entered.wait()
        await pipeline.close()
        assert cancelled.is_set()
        async def failed(codec):
            raise ValueError('encode failed')
        pipeline = EncodeAhead(failed, PipelineProfile())
        with pytest.raises(ValueError, match='encode failed'):
            await pipeline.get('h264')
        await pipeline.close()
    asyncio.run(run())


def test_sender_adapter_cleans_prefetch_on_transport_failure_and_skips_audio():
    class Sender:
        kind = 'video'
        async def _next_encoded_frame(self, codec):
            return codec
        async def _run_rtp(self, codec):
            assert await self._next_encoded_frame(codec) == codec
            raise ConnectionError('transport closed')
    sender = Sender()
    assert install_video_pipeline(sender, PipelineProfile())
    assert not install_video_pipeline(sender, PipelineProfile())
    audio = Sender()
    audio.kind = 'audio'
    assert not install_video_pipeline(audio, PipelineProfile())
    async def run():
        with pytest.raises(ConnectionError):
            await sender._run_rtp('h264')
        assert sender._elink_encode_ahead.closed
        assert sender._elink_encode_ahead.task is None
    asyncio.run(run())


def test_sender_stop_waits_for_prefetch_cleanup_after_rtp_exit():
    async def run():
        calls = []
        pending = asyncio.Event()
        async def encode(codec):
            calls.append(codec)
            if len(calls) > 1:
                await pending.wait()
            return object()
        async def finished(*args):
            return
        sender = SimpleNamespace(kind='video', _next_encoded_frame=encode, _run_rtp=finished, stop=finished)
        install_video_pipeline(sender, PipelineProfile())
        await sender._next_encoded_frame('h264')
        await asyncio.sleep(0)
        task = sender._elink_encode_ahead.task
        assert task is not None and not task.done()
        await sender.stop()
        assert sender._elink_encode_ahead.closed and sender._elink_encode_ahead.task is None
        assert task.done()
    asyncio.run(run())


def test_frame_mailbox_coalesces_notifications_and_detaches():
    from elink.core.media import FrameMailbox
    from concurrent.futures import ThreadPoolExecutor
    mailbox, notified = FrameMailbox(), []
    mailbox.put('before attaching')
    mailbox.set_notifier(lambda: notified.append(True))
    assert len(notified) == 1
    with ThreadPoolExecutor(max_workers=1) as pool:
        list(pool.map(mailbox.put, range(100)))
    assert len(notified) == 1 and mailbox.take() == 99
    mailbox.put('next')
    assert len(notified) == 2 and mailbox.take() == 'next'
    mailbox.set_notifier(None)
    mailbox.put('after close')
    assert len(notified) == 2


def test_nvenc_long_gop_still_emits_independently_decodable_requested_idr():
    import fractions
    from elink.core.codecs import CodecPolicy, DesktopEncoder, make_encoder
    policy = CodecPolicy(fps=30, bitrate=5_000_000, encoder='h264_nvenc')
    try:
        codec = make_encoder('h264_nvenc', 320, 180, policy)
    except (av.FFmpegError, ValueError):
        pytest.skip('NVENC unavailable; exercised on the Windows GPU benchmark host')
    encoder = DesktopEncoder(policy)
    encoder.codec, encoder.name = codec, 'h264_nvenc'
    for index in range(45):
        pixels = np.full((180, 320, 3), index + 20, np.uint8)
        frame = av.VideoFrame.from_ndarray(pixels, format='bgr24').reformat(format='yuv420p')
        frame.pts, frame.time_base = index * 3000, fractions.Fraction(1, 90000)
        nals = list(encoder._encode_frame(frame, index in (0, 44)))
        is_idr = any(nal[0] & 31 == 5 for nal in nals)
        assert is_idr == (index in (0, 44))
        if index == 44:
            decoder = av.CodecContext.create('h264', 'r')
            decoded = decoder.decode(av.Packet(b''.join(b'\0\0\0\1' + nal for nal in nals)))
            assert decoded and (decoded[0].width, decoded[0].height) == (320, 180)


def test_real_rtcp_pli_reaches_prefetched_encoder_and_stream_continues(tmp_path):
    from elink.core.input import RecordingInput
    from elink.core.transport import HostServer, Client
    from test_independent import until
    async def run():
        host = HostServer(tmp_path / 'host', synthetic=True, backend_factory=RecordingInput)
        client = Client(tmp_path / 'client', play_audio=False)
        try:
            port = await host.start('127.0.0.1', 0)
            await client.connect(f'127.0.0.1:{port}', dict(width=640, height=360, fps=90, audio=False))
            await until(lambda: client.mailbox.received > 5)
            sender = next(s for s in host.pc.getSenders() if s.kind == 'video')
            receiver = next(r for r in client.pc.getReceivers() if r.track.kind == 'video')
            before = host.profile.snapshot()['counters'].get('keyframe_requests', 0)
            received = client.mailbox.received
            await receiver._send_rtcp_pli(sender._ssrc)
            await until(lambda: host.profile.snapshot()['counters'].get('keyframe_requests', 0) > before)
            await until(lambda: client.mailbox.received > received + 5)
            assert client.profile.snapshot()['counters'].get('decode_errors', 0) == 0
        finally:
            await client.disconnect()
            await host.stop()
        assert sender._elink_encode_ahead.closed
        assert sender._elink_encode_ahead.task is None
    asyncio.run(run())
