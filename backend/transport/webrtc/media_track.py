"""WebRTC 媒体轨道。

一条轨道 = 「一个 SSRC + 一种编码 + 一组 RTP 序号」，负责：

- 发送：``Packet`` -> RTP 包（分片、marker、时间戳）；
- 接收：RTP 包 -> ``Packet``（重组 FU-A、按 marker 切 AU）；
- 统计：序号连续性、抖动（喂给 QoS 与 RTCP）。

传输细节（SRTP 加密、DTLS 握手）由底层栈负责，本层不感知。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from backend.logging.logger import get_logger
from backend.media.container.rtp import (
    H264RtpDepacketizer,
    H264RtpPacketizer,
    RtpPacket,
    RtpSessionState,
)
from backend.media.packet import Packet
from backend.media.stream import CodecId

logger = get_logger(__name__)


@dataclass
class TrackStats:
    packets_sent: int = 0
    bytes_sent: int = 0
    packets_received: int = 0
    packets_lost: int = 0
    frames_sent: int = 0
    frames_received: int = 0
    last_activity: float = field(default_factory=time.monotonic)

    def summary(self) -> str:
        return (
            f"发送 {self.frames_sent} 帧/{self.packets_sent} 包 "
            f"接收 {self.frames_received} 帧/{self.packets_received} 包 "
            f"丢 {self.packets_lost}"
        )


class MediaTrack:
    """一路媒体轨道（当前支持 H.264）。"""

    def __init__(
        self,
        *,
        payload_type: int = 96,
        ssrc: int = 0,
        codec: CodecId = CodecId.H264,
        mid: str = "0",
        max_payload: int = 1200,
    ) -> None:
        if codec is not CodecId.H264:
            logger.warning("轨道 %s 只实现了 H.264 的 RTP 打包，当前按 H.264 处理", codec.value)
        self.payload_type = payload_type
        self.ssrc = ssrc
        self.codec = codec
        self.mid = mid

        self.packetizer = H264RtpPacketizer(
            payload_type=payload_type, ssrc=ssrc, max_payload=max_payload
        )
        self.depacketizer = H264RtpDepacketizer(payload_type=payload_type)
        self.session = RtpSessionState()
        self.stats = TrackStats()

    # ---- 发送 ----
    def send_packet(self, packet: Packet) -> list[RtpPacket]:
        rtp_packets = self.packetizer.packetize(packet)
        self.stats.frames_sent += 1
        self.stats.packets_sent += len(rtp_packets)
        self.stats.bytes_sent += sum(item.size for item in rtp_packets)
        self.stats.last_activity = time.monotonic()
        return rtp_packets

    # ---- 接收 ----
    def receive_rtp(self, data: bytes) -> Packet | None:
        rtp = RtpPacket.unpack(data)
        self.session.observe(rtp.sequence, rtp.timestamp)
        self.stats.packets_received += 1
        self.stats.packets_lost = self.session.lost
        self.stats.last_activity = time.monotonic()

        packet = self.depacketizer.push(rtp)
        if packet is not None:
            self.stats.frames_received += 1
        return packet

    # ---- 统计 ----
    @property
    def loss_rate(self) -> float:
        return self.session.loss_rate

    @property
    def jitter_ms(self) -> float:
        """RTP 抖动的毫秒表示（时钟 90kHz）。"""
        return self.session.jitter / 90.0

    def describe(self) -> str:
        return f"track[{self.mid}] ssrc=0x{self.ssrc:08X} pt={self.payload_type} {self.stats.summary()}"

    def close(self) -> None:
        self.depacketizer.reset()


# Native tracks are separate from the packetizer above: aiortc owns RTP/RTCP.
import asyncio
import queue
import threading
from collections import deque

import av
from aiortc import MediaStreamTrack
from aiortc.codecs.h264 import H264Encoder
from aiortc.mediastreams import MediaStreamError

from backend.media.frame import Frame


class DesktopTrack(MediaStreamTrack):
    kind = "video"

    def __init__(self, capture, failure):
        super().__init__()
        self.capture, self.failure = capture, failure
        self.loop = asyncio.get_running_loop()
        self.ready = asyncio.Event()
        self._closed = threading.Event()
        self._latest = None
        self._lock = threading.Lock()
        self.capture_ms = deque(maxlen=4096)
        self.frames = 0
        self.origin_pts = None
        self._worker = threading.Thread(target=self._capture, name="rtc-capture", daemon=True)
        self._worker.start()

    def _capture(self):
        try:
            self.capture.open()
            from backend.qos.pacing import FrameClock
            clock = FrameClock(self.capture.config.fps)
            started = time.perf_counter()
            while not self._closed.is_set():
                clock.wait()
                begin = time.perf_counter()
                frame = self.capture.read()
                self.capture_ms.append((time.perf_counter() - begin) * 1000)
                frame.pts = round((time.perf_counter() - started) * 90000)
                self.capture.cursor_state = {"mode": self.capture.cursor_applied_mode,
                                            "visible": self.capture.cursor_visible,
                                            "pts": frame.pts - (self.origin_pts or 0)}
                with self._lock:
                    self._latest = frame
                self.frames += 1
                self.loop.call_soon_threadsafe(self.ready.set)
        except Exception as exc:
            if not self._closed.is_set():
                self.failure(exc)
        finally:
            self.capture.close()
            self.loop.call_soon_threadsafe(self.ready.set)

    async def recv(self):
        while not self._closed.is_set():
            await self.ready.wait()
            self.ready.clear()
            with self._lock:
                frame, self._latest = self._latest, None
            if frame is not None:
                if self.origin_pts is None:
                    self.origin_pts = frame.pts
                frame = frame.with_data(frame.data, pts=frame.pts - self.origin_pts)
                return frame.to_av()
        raise MediaStreamError

    def stop(self):
        if not self._closed.is_set():
            self.failure(RuntimeError("WebRTC 视频发送已停止"))
        self._closed.set()
        self.ready.set()
        super().stop()

    async def close(self):
        self.stop()
        await asyncio.to_thread(self._worker.join, 5)


class SelectedEncoder(H264Encoder):
    def __init__(self, adapter, stats):
        super().__init__()
        self.adapter = adapter
        self.stats = stats
        self._opened = False

    @property
    def target_bitrate(self):
        return self.adapter.target_bitrate

    @target_bitrate.setter
    def target_bitrate(self, value):
        if hasattr(self, "adapter"):
            self.adapter.target_bitrate = max(300000, min(value, self.adapter.config.bitrate_kbps * 1000))

    def encode(self, image, force_keyframe=False):
        if not self._opened:
            self.adapter.open()
            self._opened = True
        self.adapter.force_keyframe = force_keyframe
        begin = time.perf_counter()
        frame = Frame.from_av(image, native=True)
        packets = self.adapter.encode(frame)
        self.stats.extra.setdefault("encode_ms", deque(maxlen=4096)).append(
            (time.perf_counter() - begin) * 1000)
        self.stats.frames_encoded += 1
        self.stats.bytes_written += sum(p.size for p in packets)
        self.stats.keyframes += sum(p.keyframe for p in packets)
        payloads = []
        for packet in packets:
            payloads.extend(self._packetize(self._split_bitstream(packet.data)))
        return payloads, frame.pts


class LatestQueue(asyncio.Queue):
    async def put(self, item):
        if self.full():
            self.get_nowait()
        self.put_nowait(item)


class DecodeQueue(queue.Queue):
    """Recover at IDR after overflow; never drop an arbitrary reference frame."""

    def __init__(self, decode, request_keyframe):
        super().__init__(maxsize=4)
        self.decode = decode
        self.request_keyframe = request_keyframe
        self.waiting_keyframe = False

    def put(self, item, block=True, timeout=None):
        with self.mutex:
            if item is None:
                self.queue.clear()
            elif self._qsize() >= self.maxsize:
                self.queue.clear()
                self.waiting_keyframe = True
                self.request_keyframe()
            if item is not None and self.waiting_keyframe:
                if not any(n and n[0] & 31 == 5 for n in H264Encoder._split_bitstream(item[1].data)):
                    return
                self.waiting_keyframe = False
                item[1]._elink_reset = True
            self._put(item)
            self.unfinished_tasks += 1
            self.not_empty.notify()


def tune_receiver(receiver, adapter, failure, stats):
    """Pinned aiortc 1.14 decoder worker hook, with per-receiver state."""
    from aiortc import rtcrtpreceiver
    from aiortc.jitterbuffer import JitterBuffer
    from backend.media.packet import Packet
    loop = asyncio.get_running_loop()
    if not getattr(rtcrtpreceiver.decoder_worker, "_elink", False):
        original = rtcrtpreceiver.decoder_worker

        def worker(loop, input_q, output_q):
            if not isinstance(input_q, DecodeQueue):
                return original(loop, input_q, output_q)
            while True:
                task = input_q.get()
                if task is None:
                    asyncio.run_coroutine_threadsafe(output_q.put(None), loop)
                    return
                for frame in input_q.decode(task[1]):
                    asyncio.run_coroutine_threadsafe(output_q.put(frame), loop)
        worker._elink = True
        rtcrtpreceiver.decoder_worker = worker

    adapter.native_output = True
    opened = False

    def decode(encoded):
        nonlocal opened
        try:
            if not opened:
                adapter.open()
                opened = True
            packet = Packet(data=encoded.data, pts=encoded.timestamp)
            if getattr(encoded, "_elink_reset", False):
                adapter.reset(packet)
            frames = adapter.decode(packet)
            stats.packets_read += 1
            stats.bytes_read += len(encoded.data)
            stats.frames_decoded += len(frames)
            return [f.to_av() for f in frames]
        except Exception as exc:
            failure(exc)
            return []

    def request():
        async def pli():
            for ssrc in list(receiver._RTCRtpReceiver__active_ssrc):
                await receiver._send_rtcp_pli(ssrc)
        loop.call_soon_threadsafe(lambda: asyncio.create_task(pli()))

    receiver._RTCRtpReceiver__decoder_queue = DecodeQueue(decode, request)
    receiver._RTCRtpReceiver__jitter_buffer = JitterBuffer(capacity=2048, is_video=True)


class LoopbackTrack(MediaStreamTrack):
    kind = "audio"

    def __init__(self, notify):
        super().__init__()
        self.loop = asyncio.get_running_loop()
        self.queue = LatestQueue(maxsize=3)
        self.closed = threading.Event()
        self._worker = threading.Thread(target=self._read, args=(notify,), name="rtc-loopback", daemon=True)
        self._worker.start()

    def _read(self, notify):
        from fractions import Fraction
        from backend.media.audio.capture.windows.wasapi_capture import WasapiCapture
        capture = WasapiCapture()
        try:
            capture.open()
            while not self.closed.is_set():
                frame = capture.read()
                image = av.AudioFrame(format="s16", layout="stereo", samples=frame.samples)
                image.planes[0].update(frame.data)
                image.sample_rate = frame.sample_rate
                image.pts, image.time_base = frame.pts, Fraction(1, frame.sample_rate)
                asyncio.run_coroutine_threadsafe(self.queue.put(image), self.loop).result(timeout=2)
        except Exception as exc:
            if not self.closed.is_set():
                notify(exc)
        finally:
            capture.close()

    async def recv(self):
        frame = await self.queue.get()
        if frame is None:
            raise MediaStreamError
        return frame

    async def close(self):
        self.closed.set()
        await self.queue.put(None)
        self.stop()
        await asyncio.to_thread(self._worker.join, 3)
