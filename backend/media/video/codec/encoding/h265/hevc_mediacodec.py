"""Android MediaCodec 硬件编码（HEVC，``-c:v hevc_mediacodec``）。

也是 ``h264_mediacodec`` 配置失败后的回退目标：老设备常见「能解不能编」，
因此可用性只看 ffmpeg 是否编进了该编码器，实际失败由打开阶段回退处理。
"""

from __future__ import annotations

from backend.media.stream import CodecId
from backend.media.video.codec.base import FfmpegEncoderBase


class HevcMediaCodecEncoder(FfmpegEncoderBase):
    name = "hevc_mediacodec"
    codec = CodecId.H265.value
    hardware = True

    ffmpeg_encoder = "hevc_mediacodec"
    preset_supported = False
    private_params = (("-bitrate_mode", "cbr"),)

    @classmethod
    def available(cls, ffmpeg_path: str = "ffmpeg") -> bool:
        from backend.media.video.codec.probe import probe_ffmpeg

        return probe_ffmpeg(ffmpeg_path).has_encoder("hevc_mediacodec")
