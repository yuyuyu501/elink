"""Linux VA-API 硬件解码（AV1 原生解码器配合 ``-hwaccel vaapi``）。"""

from __future__ import annotations

from backend.media.stream import CodecId
from backend.media.video.codec.base import FfmpegDecoderBase


class Av1VaapiDecoder(FfmpegDecoderBase):
    name = "av1_vaapi"
    codec = CodecId.AV1.value
    hardware = True

    ffmpeg_decoder = "av1"
    input_format = "obu"
    input_args = ("-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi")
    video_filters = ("hwdownload", "format=bgra")

    @classmethod
    def available(cls, ffmpeg_path: str = "ffmpeg") -> bool:
        from backend.media.video.codec.probe import probe_ffmpeg

        return probe_ffmpeg(ffmpeg_path).has_decoder("av1_vaapi")
