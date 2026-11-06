"""音频管线：采集 -> 滤镜/增强 -> 编码 -> 输出。

与视频管线的关键差异：

- **不丢帧**：队列满时阻塞（背压），因为音频丢一帧就是可听见的爆音；
  带宽不足应该由 ``qos/bitrate.py`` 降码率，而不是在管线里丢样本；
- **更短的链路延迟**：默认 20ms 一帧，处理链路（NS/AGC）都是逐帧无缓冲的；
- **时钟即音频时钟**：音频帧的时间戳最稳定，通常反过来被用作 A/V 同步的主时钟。

输出侧交给 ``sink``（可以是封装器、SRT 通道或播放器），本类不关心具体承载。
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from backend.event.bus import EventBus
from backend.event.events import PipelineError, PipelineStarted, PipelineStopped
from backend.logging.logger import get_logger
from backend.media.audio.codec.base import AudioEncoder
from backend.media.frame import Frame
from backend.media.packet import Packet
from backend.media.stream import CodecId, MediaType, StreamInfo
from backend.media.timebase import Timebase

logger = get_logger(__name__)

_STOP = object()


@dataclass
class AudioPipelineStats:
    frames_captured: int = 0
    frames_encoded: int = 0
    packets_out: int = 0
    bytes_out: int = 0
    queue_waits: int = 0
    wall_seconds: float = 0.0
    extra: dict = field(default_factory=dict)

    @property
    def media_seconds(self) -> float:
        return float(self.extra.get("media_seconds", 0.0))

    def summary(self) -> str:
        return (
            f"采集={self.frames_captured} 编码={self.frames_encoded} "
            f"输出包={self.packets_out} 字节={self.bytes_out} "
            f"媒体时长={self.media_seconds:.2f}s 背压等待={self.queue_waits}"
        )


class AudioPipeline:
    """采集线程 + 编码线程，队列满时阻塞（音频不丢帧）。"""

    def __init__(
        self,
        capture,
        encoder: AudioEncoder,
        sink: Callable[[Packet], int] | None = None,
        *,
        filters: list | None = None,
        queue_size: int = 8,
        codec: CodecId = CodecId.AAC,
        bus: EventBus | None = None,
    ) -> None:
        self.capture = capture
        self.encoder = encoder
        self.sink = sink
        self.filters = filters or []
        self.queue_size = max(1, queue_size)
        self.codec = codec
        self.bus = bus

        self.stats = AudioPipelineStats()
        self.stream: StreamInfo | None = None
        self._queue: "queue.Queue[object]" = queue.Queue(maxsize=self.queue_size)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- 主入口 ----
    def run(self, duration_s: float | None = None, max_frames: int = 0) -> AudioPipelineStats:
        started = time.perf_counter()
        self._publish(PipelineStarted(stage="audio"))

        self.capture.open()
        self.encoder.open()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._capture_loop,
            args=(duration_s, max_frames),
            name="audio-capture",
            daemon=True,
        )
        self._thread.start()

        try:
            while True:
                item = self._queue.get()
                if item is _STOP:
                    break
                self._encode(item)
            for packet in self.encoder.flush():
                self._emit(packet)
        finally:
            self._stop.set()
            if self._thread is not None and self._thread.is_alive():
                self._thread.join(timeout=3)
            self.encoder.close()
            self.capture.close()
            for audio_filter in self.filters:
                closer = getattr(audio_filter, "close", None)
                if closer:
                    closer()
            self.stats.wall_seconds = time.perf_counter() - started
            logger.info("音频管线结束: %s", self.stats.summary())
            self._publish(PipelineStopped(stage="audio", reason="completed"))
        return self.stats

    def stop(self) -> None:
        self._stop.set()

    # ---- 内部 ----
    def _capture_loop(self, duration_s: float | None, max_frames: int) -> None:
        deadline = time.perf_counter() + duration_s if duration_s else None
        try:
            while not self._stop.is_set():
                if deadline is not None and time.perf_counter() >= deadline:
                    break
                if max_frames and self.stats.frames_captured >= max_frames:
                    break

                try:
                    frame = self.capture.read()
                except StopIteration:
                    logger.info("音频采集到达流末尾")
                    break
                except Exception as exc:
                    logger.exception("音频采集失败")
                    self._publish(PipelineError(stage="audio", message=str(exc)))
                    break

                if frame is None:  # 采集端以 None 表示「没有更多数据」
                    logger.info("音频采集返回空帧，结束")
                    break

                frame = self._apply_filters(frame)
                self.stats.frames_captured += 1
                # 音频不丢帧：满队列时阻塞等待，并把等待计入统计
                while True:
                    try:
                        self._queue.put(frame, timeout=0.1)
                        break
                    except queue.Full:
                        self.stats.queue_waits += 1
                        if self._stop.is_set():
                            break
        finally:
            self._queue.put(_STOP)

    def _encode(self, frame: Frame) -> None:
        packets = self.encoder.encode(frame)
        self.stats.frames_encoded += 1
        for packet in packets:
            self._emit(packet)

    def _emit(self, packet: Packet) -> None:
        size = len(packet.data)
        if self.sink is not None:
            written = self.sink(packet)
            size = written or size
        self.stats.packets_out += 1
        self.stats.bytes_out += size
        samples = packet.duration or 0
        self.stats.extra["media_seconds"] = (
            self.stats.extra.get("media_seconds", 0.0)
            + (samples / 1_000_000.0 if packet.timebase == Timebase.microseconds() else 0.0)
        )

    def _apply_filters(self, frame: Frame) -> Frame:
        for audio_filter in self.filters:
            frame = audio_filter.process(frame)
        return frame

    def build_stream(self, frame: Frame) -> StreamInfo:
        stream = StreamInfo(
            stream_id=0,
            media_type=MediaType.AUDIO,
            codec=self.codec,
            timebase=Timebase.microseconds(),
            sample_rate=frame.sample_rate,
            channels=frame.channels,
        )
        self.stream = stream
        return stream

    def _publish(self, event) -> None:
        if self.bus is not None:
            self.bus.publish(event)
