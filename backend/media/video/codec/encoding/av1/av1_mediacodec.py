"""Android MediaCodec AV1 硬件编码（``-c:v av1_mediacodec``）。

需要骁龙 8 Gen 2 及以上等具备 AV1 编码能力的 SoC；旧设备通常「能解不能编」，
探测不通过时由注册表回退。
"""

from __future__ import annotations

from backend.media.stream import CodecId
from backend.media.video.codec.base import FfmpegEncoderBase


class Av1MediaCodecEncoder(FfmpegEncoderBase):
    name = "av1_mediacodec"
    codec = CodecId.AV1.value
    hardware = True

    ffmpeg_encoder = "av1_mediacodec"
    preset_supported = False
    private_params = (("-bitrate_mode", "cbr"),)

    @classmethod
    def available(cls, ffmpeg_path: str = "ffmpeg") -> bool:
        from backend.media.video.codec.probe import probe_ffmpeg

        return probe_ffmpeg(ffmpeg_path).has_encoder("av1_mediacodec")
