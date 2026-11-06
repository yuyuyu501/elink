"""Opus 解码（Ogg/Opus -> PCM s16le）。

Opus 每包固定 960 采样（20ms@48kHz），因此按 960 采样切帧即可。
注意输入必须是 Ogg 封装；裸 Opus 流（``-f opus`` 之外）缺少 TOC 之外的
封装信息，不建议直接喂。
"""

from __future__ import annotations

from backend.media.audio.codec.base import FfmpegAudioDecoderBase
from backend.media.stream import CodecId


class OpusDecoder(FfmpegAudioDecoderBase):
    name = "opus"
    codec = CodecId.OPUS.value

    ffmpeg_decoder = None
    input_format = "ogg"
    samples_per_frame = 960
    output_format = "s16le"
