"""Bounded receive adapters for aiortc 1.14.0. Overflow requests a fresh video IDR."""
import asyncio
import queue

from aiortc.jitterbuffer import JitterBuffer


class LatestQueue(asyncio.Queue):
    async def put(self, item):
        if self.full():
            self.get_nowait()
        self.put_nowait(item)


class DecodeQueue(queue.Queue):
    def __init__(self, video, request_keyframe):
        super().__init__(maxsize=4)
        self.video = video
        self.request_keyframe = request_keyframe
        self.waiting_keyframe = False

    def put(self, item, block=True, timeout=None):
        with self.mutex:
            if item is None:
                self.queue.clear()
            elif self._qsize() >= self.maxsize:
                self.queue.clear()
                self.waiting_keyframe = self.video
                if self.video:
                    self.request_keyframe()
            if item is not None and self.waiting_keyframe:
                from aiortc.codecs.h264 import H264Encoder
                if not any(nal and nal[0] & 31 == 5 for nal in H264Encoder._split_bitstream(item[1].data)):
                    return
                self.waiting_keyframe = False
            self._put(item)
            self.unfinished_tasks += 1
            self.not_empty.notify()


def tune_receiver(receiver):
    loop = asyncio.get_running_loop()
    video = receiver.kind == "video" if hasattr(receiver, "kind") else receiver._RTCRtpReceiver__kind == "video"
    def request_keyframe():
        async def send():
            for ssrc in list(receiver._RTCRtpReceiver__active_ssrc):
                await receiver._send_rtcp_pli(ssrc)
        loop.call_soon_threadsafe(lambda: asyncio.create_task(send()))
    receiver._RTCRtpReceiver__decoder_queue = DecodeQueue(video, request_keyframe)
    # Capacity counts RTP packets. Large desktop IDRs can exceed aiortc's default 128.
    receiver._RTCRtpReceiver__jitter_buffer = JitterBuffer(capacity=2048, is_video=True) if video else JitterBuffer(capacity=16, prefetch=1)


def tune_track(track):
    track._queue = LatestQueue(maxsize=1 if track.kind == "video" else 3)
