import asyncio
import struct
from types import SimpleNamespace

import pytest

from elink.core.congestion import NetworkBudget
from elink.core.profiling import PipelineProfile


def test_isolated_loss_or_rtt_spike_does_not_reduce_target_and_persistent_loss_does():
    now = [0.0]
    budget = NetworkBudget(30_000_000, clock=lambda: now[0])
    budget.feedback(.1, .02)
    now[0] = 1
    budget.feedback(0, .02)
    assert budget.media_bps == 30_000_000
    for stamp in (2, 3, 4):
        now[0] = stamp
        budget.feedback(.1, .02)
    assert budget.media_bps == 24_000_000
    for stamp in range(5, 14):
        now[0] = stamp
        budget.feedback(0, .02)
    assert budget.media_bps == budget.target_bps == 30_000_000


def test_congestion_uses_rtt_increase_not_geographical_base_latency():
    now = [0.0]
    budget = NetworkBudget(20_000_000, clock=lambda: now[0])
    budget.feedback(0, .15)
    now[0] = 3
    budget.feedback(0, .15)
    assert budget.media_bps == 20_000_000
    for stamp in (4, 5, 6):
        now[0] = stamp
        budget.feedback(0, .25)
    assert budget.media_bps == 16_000_000


def test_audio_and_rtcp_bypass_blocked_video_wait_without_dropping_video(monkeypatch):
    from elink.core import qos
    sent, entered = [], asyncio.Event()
    import threading
    released = threading.Event()
    def wait(deadline):
        loop.call_soon_threadsafe(entered.set)
        released.wait(2)
    monkeypatch.setattr(qos, 'wait_until', wait)
    class Transport:
        async def _send_rtp(self, data):
            sent.append(data)
    transport = Transport()
    pc = SimpleNamespace(getTransceivers=lambda: [SimpleNamespace(sender=SimpleNamespace(
        transport=transport, kind='audio', _ssrc=99))])
    audio = struct.pack('!BBHII', 128, 111, 1, 100, 99) + b'audio'
    rtcp = bytes([128, 201, 0, 1, 0, 0, 0, 1])
    async def run():
        nonlocal loop
        loop = asyncio.get_running_loop()
        qos.install_rtp_pacer(pc, 1)
        video = asyncio.create_task(transport._send_rtp(b'x' * 10000))
        await asyncio.wait_for(entered.wait(), 2)
        try:
            await asyncio.wait_for(transport._send_rtp(audio), .5)
            await asyncio.wait_for(transport._send_rtp(rtcp), .5)
            assert sent == [audio, rtcp]
        finally:
            released.set()
            await video
        assert sent[-1] == b'x' * 10000
    loop = None
    asyncio.run(run())


def test_event_history_is_bounded_and_snapshots_are_isolated():
    profile = PipelineProfile()
    for index in range(100):
        profile.event('large_frame', bytes=index)
    snapshot = profile.snapshot()
    assert len(snapshot['events']) == 64
    assert snapshot['events'][0]['bytes'] == 36
    snapshot['events'][0]['bytes'] = -1
    assert profile.snapshot()['events'][0]['bytes'] == 36


def test_real_sender_receiver_reports_drive_budget_and_keep_pli_handling():
    from aiortc import RTCPeerConnection
    from aiortc.rtp import RtcpRrPacket, RtcpReceiverInfo, RtcpPsfbPacket
    from elink.core.media import DesktopTrack
    from elink.core.sending import install_video_pipeline
    now = [0.0]
    profile = PipelineProfile()
    budget = NetworkBudget(20_000_000, profile, lambda: now[0])
    async def run():
        pc = RTCPeerConnection()
        track = DesktopTrack(64, 36, 60, synthetic=True)
        try:
            sender = pc.addTrack(track)
            install_video_pipeline(sender, profile, budget)
            for stamp in (0, 1, 2):
                now[0] = stamp
                packet = RtcpRrPacket(ssrc=7, reports=[RtcpReceiverInfo(
                    ssrc=sender._ssrc, fraction_lost=32, packets_lost=3,
                    highest_sequence=30, jitter=0, lsr=0, dlsr=0)])
                await sender._handle_rtcp_packet(packet)
            assert budget.media_bps == 16_000_000
            await sender._handle_rtcp_packet(RtcpPsfbPacket(fmt=1, ssrc=7, media_ssrc=sender._ssrc))
            assert sender._RTCRtpSender__force_keyframe
            assert profile.snapshot()['counters']['rtcp_keyframe_requests'] == 1
        finally:
            track.stop()
            track.release_future.result(5)
            await pc.close()
    asyncio.run(run())


def test_encoder_applies_sustained_budget_and_recovers_with_independent_idrs():
    import fractions
    import av
    import numpy as np
    from elink.core.codecs import DesktopEncoder, CodecPolicy
    from elink.core.media import bgr_pixels
    encoder = DesktopEncoder(CodecPolicy(fps=30, bitrate=2_000_000, encoder='libx264'))
    now = [0.0]
    budget = NetworkBudget(2_000_000, clock=lambda: now[0])
    def encode(index, space=5, color_range=1):
        reference = av.VideoFrame.from_ndarray(np.full((180, 320, 3), (30, 90, 190), np.uint8), format='bgr24')
        frame = reference.reformat(format='nv12', dst_colorspace='ITU709' if space == 1 else 'ITU601',
                                   dst_color_range=color_range)
        frame.colorspace, frame.color_range = space, color_range
        frame.pts, frame.time_base = index * 3000, fractions.Fraction(1, 90000)
        frame.opaque = dict(network_budget=budget)
        return list(encoder._encode_frame(frame, False))
    encode(0)
    initial = encoder.codec
    budget.feedback(.1)
    encode(1)
    assert encoder.codec is initial and encoder.codec.bit_rate == 2_000_000
    now[0] = 2
    budget.feedback(.1)
    nals = encode(2)
    assert encoder.codec is not initial and encoder.codec.bit_rate == 1_600_000
    assert any(nal[0] & 31 == 5 for nal in nals)
    now[0] = 3
    budget.feedback(0)
    now[0] = 6
    budget.feedback(0)
    now[0] = 8
    budget.feedback(0)
    nals = encode(3, 1, 2)  # Also checks a capture-backend/color-metadata change.
    assert encoder.codec.bit_rate == 2_000_000
    decoder = av.CodecContext.create('h264', 'r')
    decoded = decoder.decode(av.Packet(b''.join(b'\0\0\0\1' + nal for nal in nals)))[0]
    assert int(decoded.colorspace) == 1 and int(decoded.color_range) == 2
    assert np.max(np.abs(bgr_pixels(decoded)[40, 40].astype(int) - [30, 90, 190])) <= 5
