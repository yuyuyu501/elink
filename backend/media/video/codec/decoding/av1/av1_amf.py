"""AMD AMF 硬件解码（AV1，``-c:v av1_amf``）。

AMF 的 AV1 解码需要 RDNA 2 及以上的 VCN 单元。
"""

from __future__ import annotations

from backend.media.stream import CodecId
from backend.media.video.codec.base import FfmpegDecoderBase


class Av1AmfDecoder(FfmpegDecoderBase):
    name = "av1_amf"
    codec = CodecId.AV1.value
    hardware = True

    ffmpeg_decoder = "av1_amf"
    input_format = "obu"

    @classmethod
    def available(cls, ffmpeg_path: str = "ffmpeg") -> bool:
        from backend.media.video.codec.probe import probe_ffmpeg

        return probe_ffmpeg(ffmpeg_path).has_decoder("av1_amf")
