"""Android MediaCodec 硬件解码（HEVC，``-c:v hevc_mediacodec``）。

也是 ``h264_mediacodec`` 在 Adreno 设备上配置失败后的回退目标。
"""

from __future__ import annotations

from backend.media.stream import CodecId
from backend.media.video.codec.base import FfmpegDecoderBase


class HevcMediaCodecDecoder(FfmpegDecoderBase):
    name = "hevc_mediacodec"
    codec = CodecId.H265.value
    hardware = True

    ffmpeg_decoder = "hevc_mediacodec"
    input_format = "hevc"

    @classmethod
    def available(cls, ffmpeg_path: str = "ffmpeg") -> bool:
        from backend.media.video.codec.probe import probe_ffmpeg

        return probe_ffmpeg(ffmpeg_path).has_decoder("hevc_mediacodec")
