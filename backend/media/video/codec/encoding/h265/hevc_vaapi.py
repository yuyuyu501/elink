"""Linux VA-API 硬件编码（HEVC，``-c:v hevc_vaapi``）。

同 H.264 版本：只接受显存帧，CPU 帧需先 ``hwupload``。
"""

from __future__ import annotations

from backend.media.stream import CodecId
from backend.media.video.codec.base import FfmpegEncoderBase


class HevcVaapiEncoder(FfmpegEncoderBase):
    name = "hevc_vaapi"
    codec = CodecId.H265.value
    hardware = True

    ffmpeg_encoder = "hevc_vaapi"
    preset_supported = False
    private_params = (("-vf", "format=nv12,hwupload"),)

    @classmethod
    def available(cls, ffmpeg_path: str = "ffmpeg") -> bool:
        from backend.media.video.codec.probe import probe_ffmpeg

        return probe_ffmpeg(ffmpeg_path).has_encoder("hevc_vaapi")
