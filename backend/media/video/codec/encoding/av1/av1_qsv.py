"""Intel AV1 硬件编码（``-c:v av1_qsv``）。

需要 Arc 独显或 Meteor Lake 及以上的核显。
"""

from __future__ import annotations

from backend.media.stream import CodecId
from backend.media.video.codec.base import FfmpegEncoderBase


class Av1QsvEncoder(FfmpegEncoderBase):
    name = "av1_qsv"
    codec = CodecId.AV1.value
    hardware = True

    ffmpeg_encoder = "av1_qsv"
    preset_supported = True
    private_params = (("-look_ahead", "0"),)

    @classmethod
    def available(cls, ffmpeg_path: str = "ffmpeg") -> bool:
        from backend.media.video.codec.probe import probe_ffmpeg

        return probe_ffmpeg(ffmpeg_path).has_encoder("av1_qsv")
