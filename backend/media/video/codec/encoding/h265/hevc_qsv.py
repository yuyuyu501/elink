"""Intel Quick Sync 硬件编码（HEVC，``-c:v hevc_qsv``）。"""

from __future__ import annotations

from backend.media.stream import CodecId
from backend.media.video.codec.base import FfmpegEncoderBase


class HevcQsvEncoder(FfmpegEncoderBase):
    name = "hevc_qsv"
    codec = CodecId.H265.value
    hardware = True

    ffmpeg_encoder = "hevc_qsv"
    preset_supported = True
    private_params = (("-look_ahead", "0"),)

    @classmethod
    def available(cls, ffmpeg_path: str = "ffmpeg") -> bool:
        from backend.media.video.codec.probe import probe_ffmpeg

        return probe_ffmpeg(ffmpeg_path).has_encoder("hevc_qsv")
