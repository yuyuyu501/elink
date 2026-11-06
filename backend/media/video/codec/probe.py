"""编解码能力探测。

运行时探测而不是硬编码：同一份代码在不同机器上可用后端不同。
这里只回答「本机 ffmpeg 提供了哪些编解码器」，选型逻辑在
``registry/codec_registry.py``，pipeline 不感知。
"""

from __future__ import annotations

import functools
import subprocess
from dataclasses import dataclass, field

from backend.logging.logger import get_logger

logger = get_logger(__name__)

#: 项目内支持的软件编码后端 -> ffmpeg 编码器名
KNOWN_ENCODERS: dict[str, str] = {
    "libx264": "h264",
    "libx265": "hevc",
    "libsvtav1": "av1",
    "libopus": "opus",
    "aac": "aac",
    "flac": "flac",
}

#: 项目内支持的解码后端 -> ffmpeg 解码器名
KNOWN_DECODERS: dict[str, str] = {
    "h264": "h264",
    "hevc": "hevc",
    "av1": "av1",
    "aac": "aac",
    "opus": "opus",
    "flac": "flac",
}


@dataclass
class ProbeResult:
    """一次探测的结果快照。"""

    ffmpeg_path: str = "ffmpeg"
    version: str = ""
    encoders: set[str] = field(default_factory=set)
    decoders: set[str] = field(default_factory=set)
    error: str | None = None

    @property
    def available(self) -> bool:
        return self.error is None

    def has_encoder(self, name: str) -> bool:
        return name in self.encoders

    def has_decoder(self, name: str) -> bool:
        return name in self.decoders

    def describe(self) -> str:
        if not self.available:
            return f"ffmpeg 不可用: {self.error}"
        video = sorted(
            name for name, codec in KNOWN_ENCODERS.items()
            if codec in {"h264", "hevc", "av1"} and name in self.encoders
        )
        return f"ffmpeg {self.version}; 可用视频编码器: {', '.join(video) or '无'}"


def _list_codecs(ffmpeg: str, flag: str) -> tuple[set[str], str]:
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", flag],
        capture_output=True,
        text=True,
        timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"{flag} 返回 {proc.returncode}")

    names: set[str] = set()
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[0].startswith("-"):
            continue
        flags, name = parts[0], parts[1]
        if len(flags) != 6:
            continue
        names.add(name)
    return names, proc.stdout


def _read_version(ffmpeg: str) -> str:
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-version"],
        capture_output=True,
        text=True,
        timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    first = proc.stdout.splitlines()[0] if proc.stdout else ""
    return first.replace("ffmpeg version ", "").split(" Copyright")[0].strip()


@functools.lru_cache(maxsize=4)
def probe_ffmpeg(ffmpeg_path: str = "ffmpeg") -> ProbeResult:
    """探测 ffmpeg 可用编码器/解码器（同一路径只探测一次）。"""
    try:
        version = _read_version(ffmpeg_path)
        encoders, _ = _list_codecs(ffmpeg_path, "-encoders")
        decoders, _ = _list_codecs(ffmpeg_path, "-decoders")
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        logger.error("ffmpeg 探测失败: %s", exc)
        return ProbeResult(ffmpeg_path=ffmpeg_path, error=str(exc))

    result = ProbeResult(
        ffmpeg_path=ffmpeg_path,
        version=version,
        encoders=encoders,
        decoders=decoders,
    )
    logger.info("%s", result.describe())
    return result


#: 全部视频编码后端（含硬件），用于能力上报与前端下拉框
VIDEO_ENCODERS: dict[str, str] = {
    "libx264": "h264",
    "h264_nvenc": "h264",
    "h264_amf": "h264",
    "h264_qsv": "h264",
    "h264_vaapi": "h264",
    "h264_mediacodec": "h264",
    "libx265": "hevc",
    "hevc_nvenc": "hevc",
    "hevc_amf": "hevc",
    "hevc_qsv": "hevc",
    "hevc_vaapi": "hevc",
    "hevc_mediacodec": "hevc",
    "libsvtav1": "av1",
    "av1_nvenc": "av1",
    "av1_amf": "av1",
    "av1_qsv": "av1",
    "av1_vaapi": "av1",
    "av1_mediacodec": "av1",
}

VIDEO_DECODERS: dict[str, str] = {
    "h264": "h264",
    "h264_cuvid": "h264",
    "h264_amf": "h264",
    "h264_qsv": "h264",
    "h264_vaapi": "h264",
    "h264_mediacodec": "h264",
    "hevc": "hevc",
    "hevc_cuvid": "hevc",
    "hevc_amf": "hevc",
    "hevc_qsv": "hevc",
    "hevc_vaapi": "hevc",
    "hevc_mediacodec": "hevc",
    "av1": "av1",
    "av1_cuvid": "av1",
    "av1_amf": "av1",
    "av1_qsv": "av1",
    "av1_vaapi": "av1",
    "av1_mediacodec": "av1",
}


def available_encoders(
    codec: str | None = None, ffmpeg_path: str = "ffmpeg"
) -> list[str]:
    """本机可用的视频编码后端；``codec`` 为空则返回全部。"""
    probe = probe_ffmpeg(ffmpeg_path)
    return [
        name
        for name, name_codec in VIDEO_ENCODERS.items()
        if (codec is None or name_codec == codec) and probe.has_encoder(name)
    ]


def available_decoders(
    codec: str | None = None, ffmpeg_path: str = "ffmpeg"
) -> list[str]:
    probe = probe_ffmpeg(ffmpeg_path)
    return [
        name
        for name, name_codec in VIDEO_DECODERS.items()
        if (codec is None or name_codec == codec) and probe.has_decoder(name)
    ]


def detect_video_dimensions(annexb: bytes, codec) -> tuple[int, int] | None:
    """按编码格式分派到对应的参数集解析，返回画面宽高。

    TS 的 PMT 只给 stream_type，分辨率必须从码流的参数集里解出来；
    AV1 的 sequence header 解析成本高，当前返回 None（由能力协商提供宽高）。
    """
    from backend.media.stream import CodecId

    if codec is CodecId.H264:
        from backend.media.video.codec.decoding.h264.h264 import parse_sps_dimensions

        return parse_sps_dimensions(annexb)
    if codec is CodecId.H265:
        from backend.media.video.codec.decoding.h265.hevc import parse_sps_dimensions

        return parse_sps_dimensions(annexb)
    return None


def resolve_encoder(preferred: str, ffmpeg_path: str = "ffmpeg") -> str:
    """在首选后端不可用时回退到同编码的软件后端。"""
    probe = probe_ffmpeg(ffmpeg_path)
    if probe.has_encoder(preferred):
        return preferred

    codec = KNOWN_ENCODERS.get(preferred)
    for name, candidate_codec in KNOWN_ENCODERS.items():
        if candidate_codec == codec and probe.has_encoder(name):
            logger.warning("编码器 %s 不可用，回退到 %s", preferred, name)
            return name
    return preferred
