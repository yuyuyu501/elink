"""Android MediaCodec 硬件编码（``-c:v h264_mediacodec``）。

已知问题：部分 Adreno 设备上 H.264 的 ``color-format`` 会配置失败，回退策略是
``h264_mediacodec`` -> ``hevc_mediacodec``（见 PROJECT_STRUCTURE 的编解码命名体系）。
"""

from __future__ import annotations

from backend.media.stream import CodecId
from backend.media.video.codec.base import FfmpegEncoderBase


class H264MediaCodecEncoder(FfmpegEncoderBase):
    name = "h264_mediacodec"
    codec = CodecId.H264.value
    hardware = True

    ffmpeg_encoder = "h264_mediacodec"
    preset_supported = False
    private_params = (("-bitrate_mode", "cbr"),)

    @classmethod
    def available(cls, ffmpeg_path: str = "ffmpeg") -> bool:
        from backend.media.video.codec.probe import probe_ffmpeg

        return probe_ffmpeg(ffmpeg_path).has_encoder("h264_mediacodec")
