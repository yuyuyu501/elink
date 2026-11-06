"""libx264 软件编码器。

通用搬运逻辑在 ``codec/base.py`` 的 ``FfmpegEncoderBase``，这里只声明后端语义：

- ``-c:v libx264``、支持 ``-preset`` / ``-tune zerolatency``
- ``aud=1``：强制插 AUD，让访问单元边界显式可见
- ``repeat-headers=1``：SPS/PPS 随每个 IDR 重发，接收端可以随时入流
"""

from __future__ import annotations

from backend.media.stream import CodecId
from backend.media.video.codec.base import FfmpegEncoderBase


class Libx264Encoder(FfmpegEncoderBase):
    name = "libx264"
    codec = CodecId.H264.value
    hardware = False

    ffmpeg_encoder = "libx264"
    preset_supported = True
    tune = "zerolatency"
    private_params = (("-x264-params", "aud=1:repeat-headers=1"),)
