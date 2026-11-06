"""NVIDIA NVDEC 硬件解码（``-c:v h264_cuvid``）。

编码器叫 ``_nvenc``、解码器叫 ``_cuvid``，命名不同源是 FFmpeg 的历史约定。
cuvid 输出到系统内存，因此不需要额外 hwdownload。
"""

from __future__ import annotations

from backend.media.stream import CodecId
from backend.media.video.codec.base import FfmpegDecoderBase


class H264CuvidDecoder(FfmpegDecoderBase):
    name = "h264_cuvid"
    codec = CodecId.H264.value
    hardware = True

    ffmpeg_decoder = "h264_cuvid"
    input_format = "h264"

    @classmethod
    def available(cls, ffmpeg_path: str = "ffmpeg") -> bool:
        from backend.media.video.codec.probe import probe_ffmpeg

        return probe_ffmpeg(ffmpeg_path).has_decoder("h264_cuvid")
