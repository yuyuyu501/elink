"""SVT-AV1 软件编码（``-c:v libsvtav1``）。

SVT-AV1 的 preset 是 0~13 的整数（数字越大越快），与 x264 的字符串 preset
不通用，所以这里关掉通用 preset，改用它自己的参数。
输出为 IVF，由公共分帧器还原 AV1 OBU 访问单元。
"""

from __future__ import annotations

from backend.media.stream import CodecId
from backend.media.video.codec.base import FfmpegEncoderBase


class LibSvtAv1Encoder(FfmpegEncoderBase):
    name = "libsvtav1"
    codec = CodecId.AV1.value
    hardware = False

    ffmpeg_encoder = "libsvtav1"
    preset_supported = False
    private_params = (
        ("-preset", "8"),
        ("-svtav1-params", "tune=0:scd=0"),
    )
