"""NVIDIA NVENC 硬件编码（HEVC，``-c:v hevc_nvenc``）。"""

from __future__ import annotations

from backend.media.stream import CodecId
from backend.media.video.codec.base import FfmpegEncoderBase


class HevcNvencEncoder(FfmpegEncoderBase):
    name = "hevc_nvenc"
    codec = CodecId.H265.value
    hardware = True

    ffmpeg_encoder = "hevc_nvenc"
    preset_supported = False
    private_params = (("-tune", "ll"), ("-rc", "cbr"))

    @classmethod
    def available(cls, ffmpeg_path: str = "ffmpeg") -> bool:
        from backend.media.video.codec.probe import probe_ffmpeg

        return probe_ffmpeg(ffmpeg_path).has_encoder("hevc_nvenc")
