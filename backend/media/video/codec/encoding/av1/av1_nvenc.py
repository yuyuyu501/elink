"""NVIDIA AV1 硬件编码（``-c:v av1_nvenc``）。

需要 RTX 40 系及以上（Ada 架构）才具备 AV1 编码单元；更早的卡只有 AV1 解码，
由 ``codec/probe.py`` 的探测结果决定是否回退到 H.264。
"""

from __future__ import annotations

from backend.media.stream import CodecId
from backend.media.video.codec.base import FfmpegEncoderBase


class Av1NvencEncoder(FfmpegEncoderBase):
    name = "av1_nvenc"
    codec = CodecId.AV1.value
    hardware = True

    ffmpeg_encoder = "av1_nvenc"
    preset_supported = False
    private_params = (("-tune", "ll"), ("-rc", "cbr"))

    @classmethod
    def available(cls, ffmpeg_path: str = "ffmpeg") -> bool:
        from backend.media.video.codec.probe import probe_ffmpeg

        return probe_ffmpeg(ffmpeg_path).has_encoder("av1_nvenc")
