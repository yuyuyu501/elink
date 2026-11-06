"""Intel Quick Sync 硬件解码（AV1，``-c:v av1_qsv``）。"""

from __future__ import annotations

from backend.media.stream import CodecId
from backend.media.video.codec.base import FfmpegDecoderBase


class Av1QsvDecoder(FfmpegDecoderBase):
    name = "av1_qsv"
    codec = CodecId.AV1.value
    hardware = True

    ffmpeg_decoder = "av1_qsv"
    input_format = "obu"

    @classmethod
    def available(cls, ffmpeg_path: str = "ffmpeg") -> bool:
        from backend.media.video.codec.probe import probe_ffmpeg

        return probe_ffmpeg(ffmpeg_path).has_decoder("av1_qsv")
