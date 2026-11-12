"""发送管线：采集 -> 编码 -> 封装 -> 字节 sink。

只负责连接与驱动，不实现任何算法：
- 采集线程以固定帧率取 ``Frame``，写入有界队列；
- 队列满时丢最旧的 ``Frame``（视频策略，见 PROJECT_STRUCTURE 的「背压与丢帧」）；
- 编码线程取帧编码成 ``Packet``，交给封装器写成字节流。

退出顺序固定：停止采集 -> 冲刷队列 -> 冲刷编码 -> 关闭封装。
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from backend.config.schema import VideoEncodeConfig
from backend.event.bus import EventBus
from backend.event.events import FrameDropped, PipelineStarted, PipelineStopped
from backend.logging.logger import get_logger
from backend.media.container.muxer import Muxer
from backend.media.frame import Frame, PixelFormat
from backend.media.stream import CodecId, MediaType, StreamInfo
from backend.media.timebase import Timebase
from backend.media.video.codec.base import BaseEncoder
from backend.media.video.filter.pixel_format import BaseVideoFilter

logger = get_logger(__name__)

EncoderFactory = Callable[[StreamInfo, VideoEncodeConfig], BaseEncoder]
_STOP = object()


@dataclass
class VideoPipelineStats:
    frames_captured: int = 0
    frames_dropped: int = 0
    frames_encoded: int = 0
    packets_written: int = 0
    keyframes: int = 0
    bytes_written: int = 0
    encode_seconds: float = 0.0
    wall_seconds: float = 0.0
    extra: dict = field(default_factory=dict)

    def summary(self) -> str:
        fps = self.frames_encoded / self.wall_seconds if self.wall_seconds else 0.0
        kbps = (self.bytes_written * 8 / 1000) / self.wall_seconds if self.wall_seconds else 0.0
        return (
            f"采集={self.frames_captured} 丢帧={self.frames_dropped} "
            f"编码={self.frames_encoded} 写出包={self.packets_written} "
            f"字节={self.bytes_written} 帧率={fps:.2f}fps 码率={kbps:.0f}kbps"
        )


class VideoPipeline:
    """发送侧管线。"""

    def __init__(
        self,
        capture,
        encoder_factory: EncoderFactory,
        muxer: Muxer,
        encode_config: VideoEncodeConfig | None = None,
        *,
        codec: CodecId = CodecId.H264,
        queue_size: int = 4,
        filters: list[BaseVideoFilter] | None = None,
        bus: EventBus | None = None,
    ) -> None:
        self.capture = capture
        self.encoder_factory = encoder_factory
        self.muxer = muxer
        self.encode_config = encode_config or VideoEncodeConfig()
        self.codec = codec
        self.queue_size = 1 if self.encode_config.realtime else max(1, queue_size)
        self.filters = filters or []
        self.bus = bus

        self.stats = VideoPipelineStats()
        self.stream: StreamInfo | None = None
        self._queue: "queue.Queue[object]" = queue.Queue(maxsize=self.queue_size)
        self._stop_event = threading.Event()
        self._capture_thread: threading.Thread | None = None
        self._timebase = Timebase.mpegts()
        self._capture_error: Exception | None = None
        self.process_diagnostics: dict = {}

    # ---- 主入口 ----
    def run(self, duration_s: float | None = None, max_frames: int = 0) -> VideoPipelineStats:
        started_at = time.perf_counter()
        self._publish(PipelineStarted(stage="video"))

        self.capture.open()
        self._capture_error = None
        self._stop_event.clear()
        self._capture_thread = threading.Thread(
            target=self._capture_loop,
            args=(duration_s, max_frames),
            name="video-capture",
            daemon=True,
        )
        self._capture_thread.start()

        encoder: BaseEncoder | None = None
        self.process_diagnostics = {}
        try:
            first = self._queue.get()
            if first is _STOP:
                if self._capture_error is not None:
                    raise self._capture_error
                logger.warning("采集未产生任何帧")
                return self.stats

            frame = self._apply_filters(first)
            self.stream = self._build_stream_info(frame)
            encoder = self.encoder_factory(self.stream, self.encode_config)
            encoder.open()
            self.stats.extra["encoder"] = encoder.name
            self.muxer.open([self.stream])

            self._encode(encoder, frame)
            while True:
                item = self._queue.get()
                if item is _STOP:
                    break
                self._encode(encoder, self._apply_filters(item))

            if self._capture_error is not None:
                raise self._capture_error
            for packet in encoder.flush():
                self.stats.bytes_written += self.muxer.write(packet)
                self.stats.packets_written += 1
                self.stats.keyframes += int(packet.keyframe)
        except Exception as exc:
            if encoder is not None and hasattr(encoder, "diagnostics"):
                exc.process_diagnostics = encoder.diagnostics()
            raise
        finally:
            if encoder is not None and hasattr(encoder, "diagnostics"):
                self.process_diagnostics = encoder.diagnostics()
            self._stop_event.set()
            if self._capture_thread is not None and self._capture_thread.is_alive():
                self._capture_thread.join(timeout=5)
            if encoder is not None:
                encoder.close()
            self.capture.close()
            self.muxer.close()
            for video_filter in self.filters:
                video_filter.close()

            self.stats.wall_seconds = time.perf_counter() - started_at
            logger.info("发送管线结束: %s", self.stats.summary())
            self._publish(PipelineStopped(stage="video", reason="completed"))
        return self.stats

    def stop(self) -> None:
        self._stop_event.set()

    # ---- 内部 ----
    def _capture_loop(self, duration_s: float | None, max_frames: int) -> None:
        fps = max(1e-6, float(getattr(self.capture.config, "fps", 30.0)))
        interval = 1.0 / fps
        deadline = time.perf_counter() + duration_s if duration_s else None
        next_deadline = time.perf_counter()
        frame_index = 0

        try:
            while not self._stop_event.is_set():
                now = time.perf_counter()
                if deadline is not None and now >= deadline:
                    break
                if max_frames and self.stats.frames_captured >= max_frames:
                    break

                capture_begin = time.perf_counter()
                frame = self.capture.read()
                samples = self.stats.extra.setdefault("capture_ms", [])
                samples.append((time.perf_counter() - capture_begin) * 1000)
                if len(samples) > 4096:
                    del samples[0]
                # 采集节拍由本管线控制，因此按帧率栅格重新打时间戳：
                # 把线程调度抖动写进 PTS 会让下游（封装/播放器）看到长短不一的
                # 帧间隔，进而触发重复 DTS 之类的重排问题。
                grid_pts = self._timebase.ticks(frame_index / fps)
                frame = frame.with_data(bytes(frame.data), pts=grid_pts)
                if hasattr(self.capture, "cursor_applied_mode"):
                    self.capture.cursor_state = {
                        "mode": self.capture.cursor_applied_mode,
                        "visible": self.capture.cursor_visible, "pts": grid_pts,
                    }
                frame_index += 1

                self.stats.frames_captured += 1
                self._offer((frame, time.perf_counter()))

                next_deadline += interval
                delay = next_deadline - time.perf_counter()
                if delay > 0:
                    self._stop_event.wait(delay)
                else:
                    next_deadline = time.perf_counter()
        except Exception as exc:
            self._capture_error = exc
            logger.exception("采集失败，停止采集线程")
        finally:
            self._offer(_STOP)

    def _offer(self, item: object) -> None:
        """入队；满队列时丢最旧的帧（保持时间戳单调递增）。"""
        if not self.encode_config.realtime and item is _STOP:
            while not self._stop_event.is_set():
                try:
                    self._queue.put(item, timeout=0.1)
                    return
                except queue.Full:
                    pass
        while True:
            try:
                self._queue.put_nowait(item)
                return
            except queue.Full:
                try:
                    dropped = self._queue.get_nowait()
                except queue.Empty:
                    continue
                if dropped is not _STOP:
                    self.stats.frames_dropped += 1
                    self._publish(FrameDropped(reason="queue_full"))

    def _encode(self, encoder: BaseEncoder, frame: Frame) -> None:
        begin = time.perf_counter()
        packets = encoder.encode(frame)
        elapsed_ms = (time.perf_counter() - begin) * 1000.0
        self.stats.encode_seconds += time.perf_counter() - begin
        samples = self.stats.extra.setdefault("encode_ms", [])
        if len(samples) < 4096:
            samples.append(round(elapsed_ms, 3))
        self.stats.frames_encoded += 1
        for packet in packets:
            self.stats.bytes_written += self.muxer.write(packet)
            self.stats.packets_written += 1
            self.stats.keyframes += int(packet.keyframe)

    def _apply_filters(self, frame: Frame) -> Frame:
        if isinstance(frame, tuple):
            frame, entered = frame
            samples = self.stats.extra.setdefault("queue_ms", [])
            samples.append((time.perf_counter() - entered) * 1000)
            if len(samples) > 4096:
                del samples[0]
        for video_filter in self.filters:
            frame = video_filter.process(frame)
        return frame

    def _build_stream_info(self, frame: Frame) -> StreamInfo:
        fps = float(getattr(self.capture.config, "fps", 30.0))
        timebase = Timebase.mpegts()
        return StreamInfo(
            stream_id=0,
            media_type=MediaType.VIDEO,
            codec=self.codec,
            timebase=timebase,
            width=frame.width,
            height=frame.height,
            framerate=fps,
            metadata={
                "pixel_format": (frame.pixel_format or PixelFormat.BGRA).value,
                "capture_backend": getattr(self.capture, "backend", "unknown"),
            },
        )

    def _publish(self, event) -> None:
        if self.bus is not None:
            self.bus.publish(event)
