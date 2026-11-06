"""Intel Quick Sync 硬件解码（``-c:v h264_qsv``）。"""

from __future__ import annotations

from backend.media.stream import CodecId
from backend.media.video.codec.base import FfmpegDecoderBase


class H264QsvDecoder(FfmpegDecoderBase):
    name = "h264_qsv"
    codec = CodecId.H264.value
    hardware = True

    ffmpeg_decoder = "h264_qsv"
    input_format = "h264"

    @classmethod
    def available(cls, ffmpeg_path: str = "ffmpeg") -> bool:
        from backend.media.video.codec.probe import probe_ffmpeg

        return probe_ffmpeg(ffmpeg_path).has_decoder("h264_qsv")
