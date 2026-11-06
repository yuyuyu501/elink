"""采样格式转换（s16 / s32 / f32 互转）。

内部统一用 ``array`` 模块做批量转换：逐样本 Python 循环在 48kHz 双声道下
每秒要处理 9.6 万个样本，必须走 C 层。
"""

from __future__ import annotations

import array
import sys

from backend.media.frame import Frame

#: 归一化系数
_INT16_MAX = 32767.0
_INT32_MAX = 2147483647.0


def _s16_to_f32(data: bytes) -> bytes:
    samples = array.array("h")
    samples.frombytes(data)
    if sys.byteorder == "big":
        samples.byteswap()
    out = array.array("f", (value / _INT16_MAX for value in samples))
    return out.tobytes()


def _f32_to_s16(data: bytes) -> bytes:
    samples = array.array("f")
    samples.frombytes(data)
    out = array.array(
        "h", (max(-32768, min(32767, int(value * _INT16_MAX))) for value in samples)
    )
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


def _s16_to_s32(data: bytes) -> bytes:
    samples = array.array("h")
    samples.frombytes(data)
    if sys.byteorder == "big":
        samples.byteswap()
    out = array.array("i", (value << 16 for value in samples))
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


def _s32_to_s16(data: bytes) -> bytes:
    samples = array.array("i")
    samples.frombytes(data)
    if sys.byteorder == "big":
        samples.byteswap()
    out = array.array("h", (value >> 16 for value in samples))
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


def _f32_to_s32(data: bytes) -> bytes:
    samples = array.array("f")
    samples.frombytes(data)
    out = array.array(
        "i",
        (
            max(-2147483648, min(2147483647, int(value * _INT32_MAX)))
            for value in samples
        ),
    )
    return out.tobytes()


def _s32_to_f32(data: bytes) -> bytes:
    samples = array.array("i")
    samples.frombytes(data)
    if sys.byteorder == "big":
        samples.byteswap()
    out = array.array("f", (value / _INT32_MAX for value in samples))
    return out.tobytes()


_CONVERTERS = {
    ("s16", "f32"): _s16_to_f32,
    ("f32", "s16"): _f32_to_s16,
    ("s16", "s32"): _s16_to_s32,
    ("s32", "s16"): _s32_to_s16,
    ("f32", "s32"): _f32_to_s32,
    ("s32", "f32"): _s32_to_f32,
}

#: 每种格式一个样本的字节数
BYTES_PER_SAMPLE = {"s16": 2, "s32": 4, "f32": 4}


class SampleFormatConverter:
    """PCM 采样格式转换滤镜。"""

    name = "sample_format"

    def __init__(self, target: str = "s16") -> None:
        if target not in BYTES_PER_SAMPLE:
            raise ValueError(f"不支持的采样格式: {target}")
        self.target = target

    def process(self, frame: Frame) -> Frame:
        source = frame.sample_format or "s16"
        if source == self.target:
            return frame

        converter = _CONVERTERS.get((source, self.target))
        if converter is None:
            raise ValueError(f"不支持的转换: {source} -> {self.target}")

        data = converter(bytes(frame.data))
        return frame.with_data(data, sample_format=self.target)

    def close(self) -> None:
        pass
