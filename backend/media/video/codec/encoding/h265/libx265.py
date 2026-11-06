"""libx265 软件编码（HEVC）。

输出为 HEVC 基本流（Annex-B），封装侧按 stream_type 0x24 写入 TS。
``aud=1`` 与 ``repeat-headers=1`` 的语义同 libx264。
"""

from __future__ import annotations

from backend.media.stream import CodecId
from backend.media.video.codec.base import FfmpegEncoderBase


class Libx265Encoder(FfmpegEncoderBase):
    name = "libx265"
    codec = CodecId.H265.value
    hardware = False

    ffmpeg_encoder = "libx265"
    preset_supported = True
    tune = "zerolatency"
    private_params = (("-x265-params", "aud=1:repeat-headers=1"),)
