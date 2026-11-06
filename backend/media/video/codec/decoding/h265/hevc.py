"""H.265/HEVC 软件解码 + SPS 尺寸解析。

HEVC 的 SPS 结构与 H.264 完全不同（多一层 profile_tier_level），因此单独实现
尺寸解析：接收侧同样需要先知道宽高，才能按固定字节数切分 rawvideo。
"""

from __future__ import annotations

from backend.logging.logger import get_logger
from backend.media.packet import split_annexb
from backend.media.stream import CodecId
from backend.media.video.codec.base import FfmpegDecoderBase
from backend.media.video.codec.decoding.h264.h264 import _BitReader

logger = get_logger(__name__)

_CHROMA_SUB = {0: (1, 1), 1: (2, 2), 2: (2, 1), 3: (1, 1)}


class HevcDecoder(FfmpegDecoderBase):
    """HEVC 解码：Packet(Annex-B) -> Frame(BGRA)。"""

    name = "hevc"
    codec = CodecId.H265.value
    hardware = False

    ffmpeg_decoder = None
    input_format = "hevc"


def _skip_profile_tier_level(reader: _BitReader, max_sub_layers_minus1: int) -> None:
    """跳过 profile_tier_level 语法（H.265 7.3.3）。"""
    reader.skip(2 + 1 + 5)  # profile_space / tier_flag / profile_idc
    reader.skip(32)  # profile_compatibility_flags[32]
    reader.skip(4)  # progressive / interlaced / non_packed / frame_only
    reader.skip(44)  # 各 profile 的 constraint flags 区
    reader.skip(8)  # general_level_idc

    profile_present: list[int] = []
    level_present: list[int] = []
    for _ in range(max_sub_layers_minus1):
        profile_present.append(reader.u(1))
        level_present.append(reader.u(1))

    if max_sub_layers_minus1 > 0:
        for _ in range(max_sub_layers_minus1, 8):
            reader.u(2)  # reserved_zero_2bits

    for index in range(max_sub_layers_minus1):
        if profile_present[index]:
            reader.skip(88)
        if level_present[index]:
            reader.skip(8)


def _parse_sps_dimensions(nal: bytes) -> tuple[int, int]:
    from backend.media.video.codec.base import unescape_nal

    reader = _BitReader(unescape_nal(nal[2:]))  # 2 字节 NAL header

    reader.u(4)  # sps_video_parameter_set_id
    max_sub_layers_minus1 = reader.u(3)
    reader.u(1)  # sps_temporal_id_nesting_flag
    _skip_profile_tier_level(reader, max_sub_layers_minus1)

    reader.ue()  # sps_seq_parameter_set_id
    chroma_format_idc = reader.ue()
    if chroma_format_idc == 3:
        reader.u(1)  # separate_colour_plane_flag

    width = reader.ue()
    height = reader.ue()

    if reader.u(1):  # conformance_window_flag
        left = reader.ue()
        right = reader.ue()
        top = reader.ue()
        bottom = reader.ue()
        sub_width, sub_height = _CHROMA_SUB.get(chroma_format_idc, (1, 1))
        width -= (left + right) * sub_width
        height -= (top + bottom) * sub_height

    return width, height


def parse_sps_dimensions(annexb: bytes) -> tuple[int, int] | None:
    """从 Annex-B 数据中解析 HEVC SPS 宽高。"""
    for nal in split_annexb(annexb):
        # HEVC NAL header 是 2 字节：type 在第一个字节的 1~6 位
        if not nal or ((nal[0] >> 1) & 0x3F) != 33:  # 33 = SPS_NUT
            continue
        try:
            return _parse_sps_dimensions(nal)
        except (ValueError, IndexError):
            logger.debug("HEVC SPS 解析失败", exc_info=True)
            return None
    return None
