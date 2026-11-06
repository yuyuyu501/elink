"""AMD AMF 硬件编码（``-c:v h264_amf``）。

AMF 的编解码同名接口，参数名与 x264 不同：质量用 ``-quality``、
低延迟用 ``-usage lowlatency``。
"""

from __future__ import annotations

from backend.media.stream import CodecId
from backend.media.video.codec.base import FfmpegEncoderBase


class H264AmfEncoder(FfmpegEncoderBase):
    name = "h264_amf"
    codec = CodecId.H264.value
    hardware = True

    ffmpeg_encoder = "h264_amf"
    preset_supported = False
    private_params = (
        ("-usage", "lowlatency"),
        ("-quality", "speed"),
        ("-rc", "cbr"),
    )

    @classmethod
    def available(cls, ffmpeg_path: str = "ffmpeg") -> bool:
        from backend.media.video.codec.probe import probe_ffmpeg

        return probe_ffmpeg(ffmpeg_path).has_encoder("h264_amf")
