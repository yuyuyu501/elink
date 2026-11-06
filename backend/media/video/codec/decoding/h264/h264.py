"""H.264 软件解码器（h264 原生解码器）+ 码流参数解析。

类本身只是 ``FfmpegDecoderBase`` 的薄壳；本文件更有价值的是
:func:`parse_sps_dimensions`：解封装只能从 PMT 拿到 stream_type，拿不到分辨率，
分辨率藏在 SPS 里，接收侧必须先解 SPS 才能确定 rawvideo 每帧的字节数。
"""

from __future__ import annotations

from backend.logging.logger import get_logger
from backend.media.packet import split_annexb
from backend.media.stream import CodecId
from backend.media.video.codec.base import FfmpegDecoderBase, unescape_nal

logger = get_logger(__name__)

#: 这些 profile 的 SPS 里多出 chroma_format_idc / scaling_list 等字段
_HIGH_PROFILE_IDS = frozenset(
    {100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135}
)
_CHROMA_SUB = {0: (1, 1), 1: (2, 2), 2: (2, 1), 3: (1, 1)}


class H264Decoder(FfmpegDecoderBase):
    """H.264 解码：Packet(Annex-B) -> Frame(BGRA)。"""

    name = "h264"
    codec = CodecId.H264.value
    hardware = False

    ffmpeg_decoder = None  # 交给 ffmpeg 自动选择原生解码器
    input_format = "h264"


class _BitReader:
    """按位读取（SPS 是比特对齐的变长语法，不能用字节索引）。"""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._bit = 0

    def u(self, count: int) -> int:
        value = 0
        for _ in range(count):
            byte_index = self._bit >> 3
            if byte_index >= len(self._data):
                raise ValueError("码流提前结束")
            bit = (self._data[byte_index] >> (7 - (self._bit & 7))) & 1
            value = (value << 1) | bit
            self._bit += 1
        return value

    def skip(self, count: int) -> None:
        self._bit += count

    def ue(self) -> int:
        leading = 0
        while self.u(1) == 0:
            leading += 1
            if leading > 32:
                raise ValueError("ue(v) 非法")
        if leading == 0:
            return 0
        return (1 << leading) - 1 + self.u(leading)

    def se(self) -> int:
        value = self.ue()
        magnitude = (value + 1) // 2
        return magnitude if value % 2 else -magnitude


def _parse_sps_dimensions(nal: bytes) -> tuple[int, int]:
    reader = _BitReader(unescape_nal(nal[1:]))

    profile_idc = reader.u(8)
    reader.skip(8)  # constraint_set_flags + reserved
    reader.u(8)  # level_idc
    reader.ue()  # seq_parameter_set_id

    chroma_format_idc = 1
    if profile_idc in _HIGH_PROFILE_IDS:
        chroma_format_idc = reader.ue()
        if chroma_format_idc == 3:
            reader.u(1)  # separate_colour_plane_flag
        reader.ue()  # bit_depth_luma_minus8
        reader.ue()  # bit_depth_chroma_minus8
        reader.u(1)  # qpprime_y_zero_transform_bypass_flag
        if reader.u(1):  # seq_scaling_matrix_present_flag
            for index in range(8 if chroma_format_idc != 3 else 12):
                if not reader.u(1):
                    continue
                size = 16 if index < 6 else 64
                last_scale = 8
                next_scale = 8
                for _ in range(size):
                    if next_scale != 0:
                        next_scale = (last_scale + reader.se() + 256) % 256
                    if next_scale != 0:
                        last_scale = next_scale

    reader.ue()  # log2_max_frame_num_minus4
    poc_type = reader.ue()
    if poc_type == 0:
        reader.ue()  # log2_max_pic_order_cnt_lsb_minus4
    elif poc_type == 1:
        reader.u(1)  # delta_pic_order_always_zero_flag
        reader.se()  # offset_for_non_ref_pic
        reader.se()  # offset_for_top_to_bottom_field
        for _ in range(reader.ue()):
            reader.se()

    reader.ue()  # max_num_ref_frames
    reader.u(1)  # gaps_in_frame_num_value_allowed_flag
    width_in_mbs = reader.ue() + 1
    height_in_map_units = reader.ue() + 1
    frame_mbs_only = reader.u(1)
    if not frame_mbs_only:
        reader.u(1)  # mb_adaptive_frame_field_flag
    reader.u(1)  # direct_8x8_inference_flag

    crop_left = crop_right = crop_top = crop_bottom = 0
    if reader.u(1):  # frame_cropping_flag
        crop_left = reader.ue()
        crop_right = reader.ue()
        crop_top = reader.ue()
        crop_bottom = reader.ue()

    sub_width, sub_height = _CHROMA_SUB.get(chroma_format_idc, (1, 1))
    height_multiplier = 2 - frame_mbs_only
    width = width_in_mbs * 16
    height = height_in_map_units * 16 * height_multiplier
    if chroma_format_idc != 0:
        width -= (crop_left + crop_right) * sub_width
        height -= (crop_top + crop_bottom) * sub_height * height_multiplier
    return width, height


def parse_sps_dimensions(annexb: bytes) -> tuple[int, int] | None:
    """从 Annex-B 数据中解析 SPS 宽高；没有 SPS 或解析失败返回 None。"""
    for nal in split_annexb(annexb):
        if not nal or (nal[0] & 0x1F) != 7:
            continue
        try:
            return _parse_sps_dimensions(nal)
        except (ValueError, IndexError):
            logger.debug("SPS 解析失败", exc_info=True)
            return None
    return None
