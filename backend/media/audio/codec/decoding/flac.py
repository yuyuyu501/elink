"""FLAC 解码（FLAC 基本流 -> PCM s16le）。

ffmpeg 的 flac 解码器按帧输出，常见块大小 4096；块大小不固定时按 4096 切帧
会让时间戳有毫秒级偏差（不影响播放，影响严格对齐），需要严格对齐时应改为
按帧头解析块大小。
"""

from __future__ import annotations

from backend.media.audio.codec.base import FfmpegAudioDecoderBase
from backend.media.stream import CodecId


class FlacDecoder(FfmpegAudioDecoderBase):
    name = "flac"
    codec = CodecId.FLAC.value

    ffmpeg_decoder = None
    input_format = "flac"
    samples_per_frame = 4096
    output_format = "s16le"
