"""包抽象：已编码的数据单元。

编码器输出、封装输入、解封装输出、解码输入都是 :class:`Packet`。
H.264/H.265 的 ``data`` 为 Annex-B 字节流（含起始码），一个 ``Packet``
对应一个访问单元（Access Unit），即一帧。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.media.stream import CodecId, MediaType
from backend.media.timebase import Timebase

_START_CODE = b"\x00\x00\x01"


def split_annexb(data: bytes) -> list[bytes]:
    """按起始码切分 Annex-B 数据，返回裸 NAL（去掉起始码与尾随零）。"""
    nals: list[bytes] = []
    positions: list[int] = []
    cursor = 0
    while True:
        idx = data.find(_START_CODE, cursor)
        if idx < 0:
            break
        positions.append(idx)
        cursor = idx + 3
    for i, start in enumerate(positions):
        begin = start + 3
        end = positions[i + 1] if i + 1 < len(positions) else len(data)
        while end > begin and data[end - 1] == 0:
            end -= 1
        if end > begin:
            nals.append(data[begin:end])
    return nals


def is_keyframe(data: bytes, *, codec: CodecId = CodecId.H264) -> bool:
    """以是否含 IDR 判断关键帧（H.264 NAL 类型 5）。"""
    if codec is not CodecId.H264:
        return False
    return any(nal and (nal[0] & 0x1F) == 5 for nal in split_annexb(data))


@dataclass
class Packet:
    """一个已编码的访问单元。"""

    data: bytes
    codec: CodecId = CodecId.H264
    timebase: Timebase = field(default_factory=Timebase.mpegts)
    pts: int | None = None
    dts: int | None = None
    duration: int | None = None
    stream_id: int = 0
    keyframe: bool = False
    eos: bool = False

    def __post_init__(self) -> None:
        if self.data is None:
            raise ValueError("Packet.data 不能为空")
        if self.dts is None:
            self.dts = self.pts
        if not self.keyframe:
            self.keyframe = is_keyframe(self.data, codec=self.codec)

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def media_type(self) -> MediaType:
        return self.codec.media_type

    @property
    def seconds(self) -> float:
        return self.timebase.seconds(self.pts or 0)

    def rescaled_pts(self, target: Timebase) -> int | None:
        return None if self.pts is None else self.timebase.rescale(self.pts, target)

    def rescaled_dts(self, target: Timebase) -> int | None:
        return None if self.dts is None else self.timebase.rescale(self.dts, target)
