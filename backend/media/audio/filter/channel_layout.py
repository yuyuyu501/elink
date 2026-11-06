"""声道布局定义与上下混。

上混（mono -> stereo）复制声道；下混（stereo -> mono）取平均，避免直接丢弃
一个声道导致响度塌陷。5.1 只做「前二声道参与下混」，环绕声学处理不在本层。
"""

from __future__ import annotations

import array
import enum
import sys

from backend.media.frame import Frame


class ChannelLayout(str, enum.Enum):
    MONO = "mono"
    STEREO = "stereo"
    SURROUND_5_1 = "5.1"

    @property
    def channels(self) -> int:
        return {ChannelLayout.MONO: 1, ChannelLayout.STEREO: 2, ChannelLayout.SURROUND_5_1: 6}[
            self
        ]

    @classmethod
    def from_channels(cls, channels: int) -> "ChannelLayout":
        return {1: cls.MONO, 2: cls.STEREO, 6: cls.SURROUND_5_1}.get(
            channels, cls.STEREO
        )


def _read_s16(data: bytes) -> array.array:
    samples = array.array("h")
    samples.frombytes(data)
    if sys.byteorder == "big":
        samples.byteswap()
    return samples


def _write_s16(samples: array.array) -> bytes:
    if sys.byteorder == "big":
        samples.byteswap()
    return samples.tobytes()


def upmix_mono_to_stereo(data: bytes) -> bytes:
    mono = _read_s16(data)
    stereo = array.array("h", bytes(len(mono) * 4))
    stereo[0::2] = mono
    stereo[1::2] = mono
    return _write_s16(stereo)


def downmix_stereo_to_mono(data: bytes) -> bytes:
    stereo = _read_s16(data)
    mono = array.array("h", [0]) * (len(stereo) // 2)
    left = stereo[0::2]
    right = stereo[1::2]
    for index in range(len(mono)):
        mono[index] = (left[index] + right[index]) // 2
    return _write_s16(mono)


class ChannelLayoutConverter:
    """声道数转换滤镜（仅支持 mono <-> stereo 与 5.1 -> stereo 的简化下混）。"""

    name = "channel_layout"

    def __init__(self, target: ChannelLayout) -> None:
        self.target = target

    def process(self, frame: Frame) -> Frame:
        source = ChannelLayout.from_channels(frame.channels)
        if source is self.target:
            return frame

        data = bytes(frame.data)
        if source is ChannelLayout.MONO and self.target is ChannelLayout.STEREO:
            data = upmix_mono_to_stereo(data)
        elif source is ChannelLayout.STEREO and self.target is ChannelLayout.MONO:
            data = downmix_stereo_to_mono(data)
        elif source is ChannelLayout.SURROUND_5_1 and self.target is ChannelLayout.STEREO:
            data = _downmix_51_to_stereo(data)
        else:
            raise ValueError(f"暂不支持 {source.value} -> {self.target.value}")

        return frame.with_data(
            data,
            channels=self.target.channels,
            samples=len(data) // (2 * self.target.channels),
        )

    def close(self) -> None:
        pass


def _downmix_51_to_stereo(data: bytes) -> bytes:
    """5.1 -> 立体声：前左/前右 + 部分中置，环绕按 -3dB 混入。"""
    samples = _read_s16(data)
    frames = len(samples) // 6
    out = array.array("h", [0]) * (frames * 2)
    for index in range(frames):
        base = index * 6
        left = samples[base] + samples[base + 2] // 2 + samples[base + 4] // 4
        right = samples[base + 1] + samples[base + 2] // 2 + samples[base + 5] // 4
        out[index * 2] = max(-32768, min(32767, left))
        out[index * 2 + 1] = max(-32768, min(32767, right))
    return _write_s16(out)
