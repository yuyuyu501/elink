"""Linux VA-API 硬件解码（原生解码器配合 ``-hwaccel vaapi``）。

VA-API 解出的帧在显存里，必须显式 ``hwdownload`` 到 CPU 才能装进 ``Frame``；
这一步不能隐式发生（见 PROJECT_STRUCTURE 的「硬件帧内存域」约定）。
"""

from __future__ import annotations

from backend.media.stream import CodecId
from backend.media.video.codec.base import FfmpegDecoderBase


class H264VaapiDecoder(FfmpegDecoderBase):
    name = "h264_vaapi"
    codec = CodecId.H264.value
    hardware = True

    ffmpeg_decoder = "h264"
    input_format = "h264"
    input_args = ("-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi")
    video_filters = ("hwdownload", "format=bgra")

    @classmethod
    def available(cls, ffmpeg_path: str = "ffmpeg") -> bool:
        from backend.media.video.codec.probe import probe_ffmpeg

        return probe_ffmpeg(ffmpeg_path).has_decoder("h264_vaapi")
