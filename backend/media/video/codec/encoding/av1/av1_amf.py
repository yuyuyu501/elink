"""AMD AV1 硬件编码（``-c:v av1_amf``）。

需要 RDNA 3 及以上（RX 7000 系 / 7040 系核显）。
"""

from __future__ import annotations

from backend.media.stream import CodecId
from backend.media.video.codec.base import FfmpegEncoderBase


class Av1AmfEncoder(FfmpegEncoderBase):
    name = "av1_amf"
    codec = CodecId.AV1.value
    hardware = True

    ffmpeg_encoder = "av1_amf"
    preset_supported = False
    private_params = (
        ("-usage", "lowlatency"),
        ("-quality", "speed"),
        ("-rc", "cbr"),
    )

    @classmethod
    def available(cls, ffmpeg_path: str = "ffmpeg") -> bool:
        from backend.media.video.codec.probe import probe_ffmpeg

        return probe_ffmpeg(ffmpeg_path).has_encoder("av1_amf")
