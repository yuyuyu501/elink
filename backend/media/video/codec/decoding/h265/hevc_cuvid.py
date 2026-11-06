"""NVIDIA NVDEC 硬件解码（HEVC，``-c:v hevc_cuvid``）。"""

from __future__ import annotations

from backend.media.stream import CodecId
from backend.media.video.codec.base import FfmpegDecoderBase


class HevcCuvidDecoder(FfmpegDecoderBase):
    name = "hevc_cuvid"
    codec = CodecId.H265.value
    hardware = True

    ffmpeg_decoder = "hevc_cuvid"
    input_format = "hevc"

    @classmethod
    def available(cls, ffmpeg_path: str = "ffmpeg") -> bool:
        from backend.media.video.codec.probe import probe_ffmpeg

        return probe_ffmpeg(ffmpeg_path).has_decoder("hevc_cuvid")
