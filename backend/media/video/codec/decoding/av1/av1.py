"""AV1 软件解码（``-f obu`` -> rawvideo）。

与 H.264/H.265 不同，这里**没有**从码流解析宽高的辅助函数：AV1 的
``sequence_header_obu`` 尺寸字段依赖 ``reduced_still_picture_header`` /
``frame_size_override`` 等一连串语法，实现成本高且易错。当前约定是接收侧的
宽高由能力协商（SDP/信令）或外部探测给出，写进 ``StreamInfo`` 再打开解码器；
拿不到时 ``pipeline/receive_pipeline.py`` 会给出明确错误而不是猜。
"""

from __future__ import annotations

from backend.media.stream import CodecId
from backend.media.video.codec.base import FfmpegDecoderBase


class Av1Decoder(FfmpegDecoderBase):
    """AV1 解码：Packet(OBU) -> Frame(BGRA)。"""

    name = "av1"
    codec = CodecId.AV1.value
    hardware = False

    ffmpeg_decoder = None
    input_format = "obu"
