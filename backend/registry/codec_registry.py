"""编解码实现注册表。

选型的唯一入口：pipeline 只说「我要 h264 编码器」，由注册表决定具体后端
（``libx264`` / ``h264_nvenc`` / ``h264_qsv`` ...）。

三条规则：

1. **必须可探测**：每条登记带 ``available`` 回调，不可用的后端不参与选型；
2. **明确选择**：指定后端不可用时报告错误，自动选择由能力协商统一处理；
3. **只登记能导入的**：缺可选依赖的后端在构建注册表时跳过，而不是启动即崩。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from backend.config.schema import VideoEncodeConfig
from backend.errors.codec import CodecNotFoundError
from backend.logging.logger import get_logger
from backend.media.stream import CodecId, StreamInfo
from backend.media.video.codec.base import BaseDecoder, BaseEncoder

logger = get_logger(__name__)

EncoderFactory = Callable[[StreamInfo, VideoEncodeConfig], BaseEncoder]
DecoderFactory = Callable[[StreamInfo, VideoEncodeConfig], BaseDecoder]
Availability = Callable[[], bool]

#: 后端的硬件优先级：数字越大越优先（``auto`` 时先挑硬件）
_PRIORITY_HARDWARE = 10
_PRIORITY_SOFTWARE = 0


@dataclass
class _Entry:
    name: str
    factory: Any
    available: Availability | None = None
    priority: int = 0

    def is_available(self) -> bool:
        if self.available is None:
            return True
        try:
            return bool(self.available())
        except Exception:  # 探测失败视为不可用，交给回退逻辑
            logger.debug("后端可用性探测失败: %s", self.name, exc_info=True)
            return False


class CodecRegistry:
    """编码器 / 解码器工厂注册表。"""

    def __init__(self) -> None:
        self._encoders: dict[str, list[_Entry]] = {}
        self._decoders: dict[str, list[_Entry]] = {}

    # ---- 注册 ----
    def register_encoder(
        self,
        codec: str,
        name: str,
        factory: EncoderFactory,
        *,
        available: Availability | None = None,
        priority: int = _PRIORITY_SOFTWARE,
    ) -> None:
        self._encoders.setdefault(codec, []).append(
            _Entry(name, factory, available, priority)
        )

    def register_decoder(
        self,
        codec: str,
        name: str,
        factory: DecoderFactory,
        *,
        available: Availability | None = None,
        priority: int = _PRIORITY_SOFTWARE,
    ) -> None:
        self._decoders.setdefault(codec, []).append(
            _Entry(name, factory, available, priority)
        )

    # ---- 查询 ----
    def encoder_names(self, codec: str) -> list[str]:
        return [entry.name for entry in self._usable(self._encoders, codec)]

    def decoder_names(self, codec: str) -> list[str]:
        return [entry.name for entry in self._usable(self._decoders, codec)]

    def candidates(self, kind: str):
        table = self._encoders if kind == "encoders" else self._decoders
        for codec, entries in table.items():
            for entry in sorted(entries, key=lambda item: -item.priority):
                yield codec, entry

    # ---- 创建 ----
    def create_encoder(
        self,
        codec: str,
        name: str,
        stream: StreamInfo,
        config: VideoEncodeConfig,
    ) -> BaseEncoder:
        entry = self._pick(self._encoders, codec, name, kind="编码器")
        logger.debug("创建编码器: %s/%s", codec, entry.name)
        return entry.factory(stream, config)

    def create_decoder(
        self,
        codec: str,
        name: str,
        stream: StreamInfo,
        config: VideoEncodeConfig,
    ) -> BaseDecoder:
        entry = self._pick(self._decoders, codec, name, kind="解码器")
        logger.debug("创建解码器: %s/%s", codec, entry.name)
        return entry.factory(stream, config)

    # ---- 内部 ----
    def _usable(self, table: dict[str, list[_Entry]], codec: str) -> list[_Entry]:
        entries = sorted(table.get(codec, []), key=lambda item: -item.priority)
        return [entry for entry in entries if entry.is_available()]

    def _pick(
        self,
        table: dict[str, list[_Entry]],
        codec: str,
        name: str,
        *,
        kind: str,
    ) -> _Entry:
        usable = self._usable(table, codec)
        if not usable:
            registered = [entry.name for entry in table.get(codec, [])]
            raise CodecNotFoundError(
                f"{codec} 没有可用的{kind}",
                detail=f"已登记: {', '.join(registered) or '<无>'}",
            )

        if not name or name == "auto":
            return usable[0]

        for entry in usable:
            if entry.name == name:
                return entry

        raise CodecNotFoundError(f"{codec} {kind}不可用: {name}")


def build_default_registry(ffmpeg_path: str = "ffmpeg") -> CodecRegistry:
    """登记全部已实现的后端。

    导入放在函数内部：某个后端模块引入的可选依赖缺失时，只跳过它自己。
    """
    from backend.media.video.codec.probe import probe_ffmpeg, resolve_ffmpeg_path

    ffmpeg_path = resolve_ffmpeg_path(ffmpeg_path)
    probe = probe_ffmpeg(ffmpeg_path)
    registry = CodecRegistry()

    def availability(name: str) -> Availability:
        return lambda: probe.has_encoder(name)

    def decoder_availability(name: str) -> Availability:
        if name.endswith("_vaapi"):
            codec = name.removesuffix("_vaapi")
            return lambda: probe.has_decoder(codec) and "vaapi" in probe.hwaccels
        if name == "av1":
            return lambda: any(probe.has_decoder(n) for n in ("libdav1d", "libaom-av1", "av1"))
        return lambda: probe.has_decoder(name)

    encoder_specs = [
        # (codec, 后端名, 模块, 类名, 是否硬件)
        (CodecId.H264.value, "libx264", "backend.media.video.codec.encoding.h264.libx264", "Libx264Encoder", False),
        (CodecId.H264.value, "h264_nvenc", "backend.media.video.codec.encoding.h264.h264_nvenc", "H264NvencEncoder", True),
        (CodecId.H264.value, "h264_amf", "backend.media.video.codec.encoding.h264.h264_amf", "H264AmfEncoder", True),
        (CodecId.H264.value, "h264_qsv", "backend.media.video.codec.encoding.h264.h264_qsv", "H264QsvEncoder", True),
        (CodecId.H264.value, "h264_vaapi", "backend.media.video.codec.encoding.h264.h264_vaapi", "H264VaapiEncoder", True),
        (CodecId.H264.value, "h264_mediacodec", "backend.media.video.codec.encoding.h264.h264_mediacodec", "H264MediaCodecEncoder", True),
        (CodecId.H265.value, "libx265", "backend.media.video.codec.encoding.h265.libx265", "Libx265Encoder", False),
        (CodecId.H265.value, "hevc_nvenc", "backend.media.video.codec.encoding.h265.hevc_nvenc", "HevcNvencEncoder", True),
        (CodecId.H265.value, "hevc_amf", "backend.media.video.codec.encoding.h265.hevc_amf", "HevcAmfEncoder", True),
        (CodecId.H265.value, "hevc_qsv", "backend.media.video.codec.encoding.h265.hevc_qsv", "HevcQsvEncoder", True),
        (CodecId.H265.value, "hevc_vaapi", "backend.media.video.codec.encoding.h265.hevc_vaapi", "HevcVaapiEncoder", True),
        (CodecId.H265.value, "hevc_mediacodec", "backend.media.video.codec.encoding.h265.hevc_mediacodec", "HevcMediaCodecEncoder", True),
        (CodecId.AV1.value, "libsvtav1", "backend.media.video.codec.encoding.av1.libsvtav1", "LibSvtAv1Encoder", False),
        (CodecId.AV1.value, "av1_nvenc", "backend.media.video.codec.encoding.av1.av1_nvenc", "Av1NvencEncoder", True),
        (CodecId.AV1.value, "av1_amf", "backend.media.video.codec.encoding.av1.av1_amf", "Av1AmfEncoder", True),
        (CodecId.AV1.value, "av1_qsv", "backend.media.video.codec.encoding.av1.av1_qsv", "Av1QsvEncoder", True),
        (CodecId.AV1.value, "av1_vaapi", "backend.media.video.codec.encoding.av1.av1_vaapi", "Av1VaapiEncoder", True),
        (CodecId.AV1.value, "av1_mediacodec", "backend.media.video.codec.encoding.av1.av1_mediacodec", "Av1MediaCodecEncoder", True),
    ]

    decoder_specs = [
        (CodecId.H264.value, "h264", "backend.media.video.codec.decoding.h264.h264", "H264Decoder", False),
        (CodecId.H264.value, "h264_cuvid", "backend.media.video.codec.decoding.h264.h264_cuvid", "H264CuvidDecoder", True),
        (CodecId.H264.value, "h264_amf", "backend.media.video.codec.decoding.h264.h264_amf", "H264AmfDecoder", True),
        (CodecId.H264.value, "h264_qsv", "backend.media.video.codec.decoding.h264.h264_qsv", "H264QsvDecoder", True),
        (CodecId.H264.value, "h264_vaapi", "backend.media.video.codec.decoding.h264.h264_vaapi", "H264VaapiDecoder", True),
        (CodecId.H264.value, "h264_mediacodec", "backend.media.video.codec.decoding.h264.h264_mediacodec", "H264MediaCodecDecoder", True),
        (CodecId.H265.value, "hevc", "backend.media.video.codec.decoding.h265.hevc", "HevcDecoder", False),
        (CodecId.H265.value, "hevc_cuvid", "backend.media.video.codec.decoding.h265.hevc_cuvid", "HevcCuvidDecoder", True),
        (CodecId.H265.value, "hevc_amf", "backend.media.video.codec.decoding.h265.hevc_amf", "HevcAmfDecoder", True),
        (CodecId.H265.value, "hevc_qsv", "backend.media.video.codec.decoding.h265.hevc_qsv", "HevcQsvDecoder", True),
        (CodecId.H265.value, "hevc_vaapi", "backend.media.video.codec.decoding.h265.hevc_vaapi", "HevcVaapiDecoder", True),
        (CodecId.H265.value, "hevc_mediacodec", "backend.media.video.codec.decoding.h265.hevc_mediacodec", "HevcMediaCodecDecoder", True),
        (CodecId.AV1.value, "av1", "backend.media.video.codec.decoding.av1.av1", "Av1Decoder", False),
        (CodecId.AV1.value, "av1_cuvid", "backend.media.video.codec.decoding.av1.av1_cuvid", "Av1CuvidDecoder", True),
        (CodecId.AV1.value, "av1_amf", "backend.media.video.codec.decoding.av1.av1_amf", "Av1AmfDecoder", True),
        (CodecId.AV1.value, "av1_qsv", "backend.media.video.codec.decoding.av1.av1_qsv", "Av1QsvDecoder", True),
        (CodecId.AV1.value, "av1_vaapi", "backend.media.video.codec.decoding.av1.av1_vaapi", "Av1VaapiDecoder", True),
        (CodecId.AV1.value, "av1_mediacodec", "backend.media.video.codec.decoding.av1.av1_mediacodec", "Av1MediaCodecDecoder", True),
    ]

    for codec, name, module_path, class_name, is_hardware in encoder_specs:
        try:
            module = __import__(module_path, fromlist=[class_name])
            cls = getattr(module, class_name)
        except ImportError as exc:
            logger.debug("跳过编码后端 %s: %s", name, exc)
            continue
        registry.register_encoder(
            codec,
            name,
            lambda stream, config, _cls=cls: _cls(stream, replace(config, ffmpeg_path=ffmpeg_path)),
            available=availability(name),
            priority=_PRIORITY_HARDWARE if is_hardware else _PRIORITY_SOFTWARE,
        )

    for codec, name, module_path, class_name, is_hardware in decoder_specs:
        try:
            module = __import__(module_path, fromlist=[class_name])
            cls = getattr(module, class_name)
        except ImportError as exc:
            logger.debug("跳过后端解码器 %s: %s", name, exc)
            continue
        registry.register_decoder(
            codec,
            name,
            lambda stream, config, _cls=cls: _cls(stream, replace(config, ffmpeg_path=ffmpeg_path)),
            available=decoder_availability(name),
            priority=_PRIORITY_HARDWARE if is_hardware else _PRIORITY_SOFTWARE,
        )

    return registry
