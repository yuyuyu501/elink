"""Android MediaCodec 硬件解码（``-c:v h264_mediacodec``）。

MediaCodec 通常输出 Surface 纹理，这里走 ffmpeg 的默认下载路径拿 CPU 帧；
需要零拷贝上屏时应改用 ``render/android/surface_renderer.py``。
"""

from __future__ import annotations

from backend.media.stream import CodecId
from backend.media.video.codec.base import FfmpegDecoderBase


class H264MediaCodecDecoder(FfmpegDecoderBase):
    name = "h264_mediacodec"
    codec = CodecId.H264.value
    hardware = True

    ffmpeg_decoder = "h264_mediacodec"
    input_format = "h264"

    @classmethod
    def available(cls, ffmpeg_path: str = "ffmpeg") -> bool:
        from backend.media.video.codec.probe import probe_ffmpeg

        return probe_ffmpeg(ffmpeg_path).has_decoder("h264_mediacodec")
