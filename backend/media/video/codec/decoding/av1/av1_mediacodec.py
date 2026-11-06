"""Android MediaCodec 硬件解码（AV1，``-c:v av1_mediacodec``）。

老设备常见「能解不能编」：AV1 解码在骁龙 8 Gen 1 起可用，编码要到 Gen 2+。
"""

from __future__ import annotations

from backend.media.stream import CodecId
from backend.media.video.codec.base import FfmpegDecoderBase


class Av1MediaCodecDecoder(FfmpegDecoderBase):
    name = "av1_mediacodec"
    codec = CodecId.AV1.value
    hardware = True

    ffmpeg_decoder = "av1_mediacodec"
    input_format = "obu"

    @classmethod
    def available(cls, ffmpeg_path: str = "ffmpeg") -> bool:
        from backend.media.video.codec.probe import probe_ffmpeg

        return probe_ffmpeg(ffmpeg_path).has_decoder("av1_mediacodec")
