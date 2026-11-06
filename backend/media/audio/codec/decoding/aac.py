"""AAC 解码（ADTS -> PCM s16le）。

AAC 每帧固定 1024 采样，因此解码输出按 1024 采样切帧即可，不需要再解析码流。
"""

from __future__ import annotations

from backend.media.audio.codec.base import FfmpegAudioDecoderBase
from backend.media.stream import CodecId


class AacDecoder(FfmpegAudioDecoderBase):
    name = "aac"
    codec = CodecId.AAC.value

    ffmpeg_decoder = None
    # 注意：ffmpeg 里 ADTS 的「解封装器」叫 aac，「封装器」才叫 adts（见 -formats）
    input_format = "aac"
    samples_per_frame = 1024
    output_format = "s16le"
