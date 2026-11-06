"""NVIDIA NVDEC 硬件解码（AV1，``-c:v av1_cuvid``）。

RTX 30 系及以上支持 AV1 解码（编码要 40 系）。
"""

from __future__ import annotations

from backend.media.stream import CodecId
from backend.media.video.codec.base import FfmpegDecoderBase


class Av1CuvidDecoder(FfmpegDecoderBase):
    name = "av1_cuvid"
    codec = CodecId.AV1.value
    hardware = True

    ffmpeg_decoder = "av1_cuvid"
    input_format = "obu"

    @classmethod
    def available(cls, ffmpeg_path: str = "ffmpeg") -> bool:
        from backend.media.video.codec.probe import probe_ffmpeg

        return probe_ffmpeg(ffmpeg_path).has_decoder("av1_cuvid")
