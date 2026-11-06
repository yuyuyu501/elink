"""依赖装配。

把「配置 -> 注册表 -> 能力探测 -> 具体实现 -> 管线」串起来。这里是唯一
知道具体后端名字（``libx264`` / ``mpegts`` / ``gdi``）的装配处，管线本身
只看接口。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO

from backend.config.schema import AppConfig
from backend.errors.base import ElinkError, ErrorCode
from backend.event.bus import EventBus
from backend.logging.logger import get_logger
from backend.media.container.demuxer import Demuxer
from backend.media.container.mpegts import MpegTsDemuxer, MpegTsMuxer
from backend.media.video.codec.probe import ProbeResult, probe_ffmpeg, resolve_encoder
from backend.media.video.render.image_sink import ImageSinkRenderer
from backend.pipeline.receive_pipeline import ReceivePipeline
from backend.pipeline.video_pipeline import VideoPipeline
from backend.registry.codec_registry import CodecRegistry, build_default_registry
from backend.transport.srt import SrtTransport

logger = get_logger(__name__)


class BootstrapError(ElinkError):
    code = ErrorCode.INVALID_STATE


class Runtime:
    """一次运行持有的资源，按构建顺序反向释放。"""

    def __init__(self, **resources: object) -> None:
        self._resources = resources
        self._closed = False

    def get(self, name: str):
        return self._resources.get(name)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for name in reversed(list(self._resources)):
            resource = self._resources[name]
            closer = getattr(resource, "close", None)
            if closer is None:
                continue
            try:
                closer()
            except Exception:  # 释放阶段的异常不应掩盖主流程结果
                logger.exception("释放资源失败: %s", name)


# ---- 环境探测 ----
def prepare_environment(config: AppConfig) -> tuple[ProbeResult, CodecRegistry]:
    """探测 ffmpeg 能力并构建注册表；两者缺一不可。"""
    probe = probe_ffmpeg(config.encode.ffmpeg_path)
    if not probe.available:
        raise BootstrapError(
            "未找到可用的 ffmpeg，请在 PATH 中提供或设置 encode.ffmpeg_path",
            detail=probe.error,
        )

    encoder_name = resolve_encoder(config.encode.encoder, config.encode.ffmpeg_path)
    if not probe.has_encoder(encoder_name):
        raise BootstrapError(
            f"ffmpeg 缺少编码器 {encoder_name}", detail=probe.describe()
        )
    config.encode.encoder = encoder_name

    return probe, build_default_registry(config.encode.ffmpeg_path)


# ---- 采集 ----
def build_capture(config: AppConfig):
    """按平台构建屏幕采集实现。"""
    backend = config.capture.backend
    if backend in {"auto", "gdi", "dxgi"}:
        if os.name != "nt":
            raise BootstrapError(
                "当前里程碑只实现了 Windows 屏幕采集", detail=os.name
            )
        from backend.media.video.capture.windows.dxgi_desktop_duplication import (
            DesktopCapture,
        )

        return DesktopCapture(config.capture)

    raise BootstrapError(f"未知采集后端: {backend}", detail=backend)


# ---- 发送侧 ----
def build_send_resources(
    config: AppConfig,
    registry: CodecRegistry,
    bus: EventBus | None = None,
) -> Runtime:
    """组装发送链路：采集 -> 编码 -> MPEG-TS -> sink（文件或 SRT）。"""
    transport = config.transport
    sink: BinaryIO
    resources: dict[str, object] = {}

    if transport.kind == "file":
        path = Path(transport.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        sink = path.open("wb")
        resources["sink"] = sink
        logger.info("发送目标: 文件 %s", path.resolve())
    elif transport.kind == "srt":
        srt = SrtTransport(
            transport.srt_url,
            mode=transport.srt_mode,
            latency_ms=transport.srt_latency_ms,
            ffmpeg_path=config.encode.ffmpeg_path,
        )
        srt.open()
        sink = srt.writer
        resources["srt"] = srt
        logger.info("发送目标: %s", srt.url)
    else:
        raise BootstrapError(f"未知传输类型: {transport.kind}", detail=transport.kind)

    muxer = MpegTsMuxer(sink, config.mux)
    capture = build_capture(config)

    pipeline = VideoPipeline(
        capture=capture,
        encoder_factory=lambda stream, encode_config: registry.create_encoder(
            stream.codec.value, config.encode.encoder, stream, encode_config
        ),
        muxer=muxer,
        encode_config=config.encode,
        queue_size=config.pipeline.queue_size,
        bus=bus,
    )

    resources.update(
        {"pipeline": pipeline, "muxer": muxer, "capture": capture}
    )
    return Runtime(**resources)


# ---- 接收侧 ----
def build_receive_resources(
    config: AppConfig,
    registry: CodecRegistry,
    bus: EventBus | None = None,
    *,
    source_path: str | Path | None = None,
) -> Runtime:
    """组装接收链路：字节源 -> MPEG-TS 解封装 -> 解码 -> 渲染（写图）。"""
    resources: dict[str, object] = {}

    if source_path is not None:
        path = Path(source_path)
        if not path.is_file():
            raise BootstrapError(f"输入文件不存在: {path}", detail=str(path))
        source: BinaryIO = path.open("rb")
        resources["source"] = source
        logger.info("接收来源: 文件 %s", path.resolve())
    elif config.transport.kind == "file":
        path = Path(config.transport.path)
        if not path.is_file():
            raise BootstrapError(f"输入文件不存在: {path}", detail=str(path))
        source = path.open("rb")
        resources["source"] = source
    elif config.transport.kind == "srt":
        srt = SrtTransport(
            config.transport.srt_url,
            mode="listener",
            latency_ms=config.transport.srt_latency_ms,
            ffmpeg_path=config.encode.ffmpeg_path,
        )
        srt.open()
        source = srt.reader
        resources["srt"] = srt
        logger.info("接收来源: %s", srt.url)
    else:
        raise BootstrapError(
            f"未知传输类型: {config.transport.kind}", detail=config.transport.kind
        )

    demuxer: Demuxer = MpegTsDemuxer(source)
    renderer = ImageSinkRenderer(config.frames_dir)

    pipeline = ReceivePipeline(
        demuxer=demuxer,
        decoder_factory=lambda stream, encode_config: registry.create_decoder(
            stream.codec.value, stream.codec.value, stream, encode_config
        ),
        renderer=renderer,
        encode_config=config.encode,
        bus=bus,
    )

    resources.update({"pipeline": pipeline, "demuxer": demuxer, "renderer": renderer})
    return Runtime(**resources)
