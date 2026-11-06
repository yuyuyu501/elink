"""Linux VA-API 硬件解码（HEVC，``-c:v hevc_vaapi``）。

显存帧需显式 ``hwdownload`` 才能进入 ``Frame``（内存域转换不隐式发生）。
"""

from __future__ import annotations

from backend.media.stream import CodecId
from backend.media.video.codec.base import FfmpegDecoderBase


class HevcVaapiDecoder(FfmpegDecoderBase):
    name = "hevc_vaapi"
    codec = CodecId.H265.value
    hardware = True

    ffmpeg_decoder = "hevc_vaapi"
    input_format = "hevc"
    input_args = ("-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi")
    video_filters = ("hwdownload", "format=bgra")

    @classmethod
    def available(cls, ffmpeg_path: str = "ffmpeg") -> bool:
        from backend.media.video.codec.probe import probe_ffmpeg

        return probe_ffmpeg(ffmpeg_path).has_decoder("hevc_vaapi")
