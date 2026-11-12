"""接收管线：字节流 -> 解封装 -> 解码 -> 渲染。

同样只做连接与驱动：字节源（文件 or SRT）由上层注入，解封装负责
``bytes -> Packet``，解码负责 ``Packet -> Frame``，渲染端负责把 ``Frame``
写图或上屏。

分辨率问题：TS 的 PMT 只给 stream_type，不给宽高；宽高在 SPS 里，因此要先
缓存若干个 ``Packet``，等拿到 SPS 再确定解码器输出尺寸，然后按顺序回灌。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from backend.config.schema import VideoEncodeConfig
from backend.errors.codec import DecodeError
from backend.event.bus import EventBus
from backend.event.events import PipelineStarted, PipelineStopped
from backend.logging.logger import get_logger
from backend.media.container.demuxer import Demuxer
from backend.media.frame import Frame
from backend.media.packet import Packet
from backend.media.stream import StreamInfo
from backend.media.video.codec.base import BaseDecoder
from backend.media.video.codec.probe import detect_video_dimensions

logger = get_logger(__name__)

DecoderFactory = Callable[[StreamInfo, VideoEncodeConfig], BaseDecoder]


@dataclass
class ReceivePipelineStats:
    packets_read: int = 0
    bytes_read: int = 0
    frames_decoded: int = 0
    frames_rendered: int = 0
    keyframes: int = 0
    first_pts: int | None = None
    last_pts: int | None = None
    wall_seconds: float = 0.0
    extra: dict = field(default_factory=dict)

    @property
    def media_seconds(self) -> float:
        if self.first_pts is None or self.last_pts is None:
            return 0.0
        return (self.last_pts - self.first_pts) / 90_000

    def summary(self) -> str:
        fps = self.frames_decoded / self.wall_seconds if self.wall_seconds else 0.0
        return (
            f"读包={self.packets_read} 解码帧={self.frames_decoded} "
            f"渲染帧={self.frames_rendered} 关键帧={self.keyframes} "
            f"媒体时长={self.media_seconds:.2f}s 解码帧率={fps:.2f}fps"
        )


class ReceivePipeline:
    """接收侧管线。"""

    def __init__(
        self,
        demuxer: Demuxer,
        decoder_factory: DecoderFactory,
        renderer,
        encode_config: VideoEncodeConfig | None = None,
        bus: EventBus | None = None,
    ) -> None:
        self.demuxer = demuxer
        self.decoder_factory = decoder_factory
        self.renderer = renderer
        self.encode_config = encode_config or VideoEncodeConfig()
        self.bus = bus
        self.stats = ReceivePipelineStats()
        self.stream: StreamInfo | None = None
        self.process_diagnostics: dict = {}

    def run(self) -> ReceivePipelineStats:
        started_at = time.perf_counter()
        self._publish(PipelineStarted(stage="receive"))

        decoder: BaseDecoder | None = None
        self.process_diagnostics = {}
        pending: list[Packet] = []

        try:
            self.demuxer.open()
            self.renderer.open()

            while True:
                packet = self.demuxer.read()
                if packet is None:
                    break

                self.stats.packets_read += 1
                self.stats.bytes_read += len(packet.data)
                if packet.keyframe:
                    self.stats.keyframes += 1
                if self.stats.first_pts is None:
                    self.stats.first_pts = packet.pts
                if packet.pts is not None:
                    self.stats.last_pts = packet.pts

                if decoder is None:
                    pending.append(packet)
                    if (len(pending) > 240 or sum(len(p.data) for p in pending) > 32 * 1024 * 1024
                            or time.perf_counter() - started_at > 10):
                        raise DecodeError("等待视频初始化参数超时")
                    decoder = self._open_decoder(pending)
                    if decoder is None:
                        continue
                    for buffered in pending:
                        self._decode(decoder, buffered)
                    pending.clear()
                    continue

                self._decode(decoder, packet)

            if decoder is None:
                if pending:
                    codec = self.demuxer.streams[0].codec.value if self.demuxer.streams else "?"
                    raise DecodeError(
                        f"未能从 {codec} 码流中解析出参数集（SPS），无法确定解码输出尺寸"
                    )
                logger.warning("流中没有任何可解码的包")
                return self.stats

            for frame in decoder.flush():
                self._render(frame)
        except Exception as exc:
            if decoder is not None and hasattr(decoder, "diagnostics"):
                exc.process_diagnostics = decoder.diagnostics()
            raise
        finally:
            if decoder is not None and hasattr(decoder, "diagnostics"):
                self.process_diagnostics = decoder.diagnostics()
            if decoder is not None:
                decoder.close()
            self.renderer.close()
            self.demuxer.close()
            self.stats.wall_seconds = time.perf_counter() - started_at
            logger.info("接收管线结束: %s", self.stats.summary())
            self._publish(PipelineStopped(stage="receive", reason="completed"))

        return self.stats

    # ---- 内部 ----
    def _open_decoder(self, pending: list[Packet]) -> BaseDecoder | None:
        if not self.demuxer.streams:
            return None

        base = self.demuxer.streams[0]
        dimensions = (base.width, base.height) if base.width and base.height else None
        for packet in pending:
            parsed = detect_video_dimensions(packet.data, base.codec)
            if parsed:
                dimensions = parsed
                break
        if dimensions is None:
            if self.encode_config.width and self.encode_config.height:
                dimensions = self.encode_config.width, self.encode_config.height
            else:
                return None

        width, height = dimensions
        if not (0 < width <= 7680 and 0 < height <= 4320):
            raise DecodeError("无效视频尺寸")
        if self.encode_config.realtime and base.codec.value != self.encode_config.codec:
            raise DecodeError("视频格式与协商结果不一致")
        stream = base.copy_with(width=width, height=height)
        self.stream = stream
        logger.info(
            "解码参数就绪: codec=%s 分辨率=%dx%d", stream.codec.value, width, height
        )

        decoder = self.decoder_factory(stream, self.encode_config)
        self.stats.extra["decoder"] = decoder.name
        if self.encode_config.realtime and hasattr(decoder, "output_callback"):
            decoder.output_callback = self._render
            self.stats.extra["decode_output_ms"] = decoder.output_wait_ms
        try:
            decoder.open()
        except Exception as exc:
            if hasattr(decoder, "diagnostics"):
                exc.process_diagnostics = decoder.diagnostics()
            decoder.close()
            raise
        return decoder

    def _decode(self, decoder: BaseDecoder, packet: Packet) -> None:
        begin = time.perf_counter()
        frames = decoder.decode(packet)
        elapsed_ms = (time.perf_counter() - begin) * 1000.0
        samples = self.stats.extra.setdefault("decode_feed_ms", [])
        if len(samples) < 4096:
            samples.append(round(elapsed_ms, 3))
        for frame in frames:
            self._render(frame)

    def _render(self, frame: Frame) -> None:
        begin = time.perf_counter()
        self.stats.frames_decoded += 1
        if self.renderer.render(frame) is not None:
            self.stats.frames_rendered += 1
        elapsed_ms = (time.perf_counter() - begin) * 1000.0
        samples = self.stats.extra.setdefault("render_ms", [])
        if len(samples) < 4096:
            samples.append(round(elapsed_ms, 3))

    def _publish(self, event) -> None:
        if self.bus is not None:
            self.bus.publish(event)
