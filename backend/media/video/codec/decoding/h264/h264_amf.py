"""AMD AMF 硬件解码（``-c:v h264_amf``）。

AMF 编码解码同接口名；解码侧依赖 Windows 的 AMF 运行时。
"""

from __future__ import annotations

from backend.media.stream import CodecId
from backend.media.video.codec.base import FfmpegDecoderBase


class H264AmfDecoder(FfmpegDecoderBase):
    name = "h264_amf"
    codec = CodecId.H264.value
    hardware = True

    ffmpeg_decoder = "h264_amf"
    input_format = "h264"

    @classmethod
    def available(cls, ffmpeg_path: str = "ffmpeg") -> bool:
        from backend.media.video.codec.probe import probe_ffmpeg

        return probe_ffmpeg(ffmpeg_path).has_decoder("h264_amf")
