"""Linux VA-API AV1 硬件编码（``-c:v av1_vaapi``）。

同 H.264/H.265 的 VA-API 版本：只吃显存帧，CPU 帧需先 ``hwupload``。
"""

from __future__ import annotations

from backend.media.stream import CodecId
from backend.media.video.codec.base import FfmpegEncoderBase


class Av1VaapiEncoder(FfmpegEncoderBase):
    name = "av1_vaapi"
    codec = CodecId.AV1.value
    hardware = True

    ffmpeg_encoder = "av1_vaapi"
    preset_supported = False
    private_params = (("-vf", "format=nv12,hwupload"),)

    @classmethod
    def available(cls, ffmpeg_path: str = "ffmpeg") -> bool:
        from backend.media.video.codec.probe import probe_ffmpeg

        return probe_ffmpeg(ffmpeg_path).has_encoder("av1_vaapi")
