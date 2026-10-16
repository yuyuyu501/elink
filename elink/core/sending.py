"""Bounded encode/send overlap for the pinned aiortc 1.14 sender API."""
import asyncio
import time

from aiortc.mediastreams import MediaStreamError


class EncodeAhead:
    """Prepare at most one frame while the caller sends the previous frame.

    Never discard encoded frames: later H.264 pictures can reference them.
    A slow transport applies backpressure instead of growing an output queue.
    """
    def __init__(self, encode, profile):
        self.encode, self.profile = encode, profile
        self.task = None
        self.closed = False
        self.handed_at = None

    async def _prepare(self, codec):
        frame = await self.encode(codec)
        return frame, time.perf_counter()

    async def get(self, codec):
        if self.closed:
            raise MediaStreamError
        if self.handed_at is not None:
            self.profile.elapsed('send_frame', self.handed_at)
        if self.task is None:
            self.task = asyncio.create_task(self._prepare(codec))
        started = time.perf_counter()
        frame, ready_at = await self.task
        self.profile.elapsed('encode_ahead_wait', started)
        self.profile.elapsed('encoded_queue_wait', ready_at)
        if self.closed:
            raise MediaStreamError
        self.task = asyncio.create_task(self._prepare(codec))
        self.handed_at = time.perf_counter()
        return frame

    async def close(self):
        self.closed = True
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None


def install_video_pipeline(sender, profile, budget=None):
    if sender.kind != 'video' or hasattr(sender, '_elink_encode_ahead'):
        return False
    pipeline = EncodeAhead(sender._next_encoded_frame, profile)
    original_run = sender._run_rtp

    async def run(codec):
        try:
            return await original_run(codec)
        finally:
            await pipeline.close()

    sender._next_encoded_frame = pipeline.get
    sender._run_rtp = run
    sender._elink_encode_ahead = pipeline
    if budget is not None:
        from aiortc.rtp import RtcpRrPacket, RtcpSrPacket, RtcpPsfbPacket
        original_feedback = sender._handle_rtcp_packet

        async def feedback(packet):
            await original_feedback(packet)
            if isinstance(packet, (RtcpRrPacket, RtcpSrPacket)):
                for report in packet.reports:
                    if report.ssrc == sender._ssrc:
                        budget.feedback(report.fraction_lost / 256, getattr(sender, '_RTCRtpSender__rtt', None))
            elif isinstance(packet, RtcpPsfbPacket) and packet.fmt in (1, 4):
                profile.count('rtcp_keyframe_requests')
                profile.event('recovery_request', kind='PLI' if packet.fmt == 1 else 'FIR')

        sender._handle_rtcp_packet = feedback
    return True
