"""FLAC 编码（``-c:a flac -f flac``）。

FLAC 帧没有长度字段，只能靠帧头的同步字（``0xFFF8``~``0xFFFB``）划定边界，
但块大小写在帧头里，因此 PTS 仍然可以精确推算。
"""

from __future__ import annotations

from backend.media.audio.codec.base import FfmpegAudioEncoderBase
from backend.media.stream import CodecId

_FLAC_SYNC_MASK = 0xFFFE  # 14 位同步字 + reserved


def _is_frame_sync(buffer: bytearray, index: int) -> bool:
    if index + 2 > len(buffer):
        return False
    word = (buffer[index] << 8) | buffer[index + 1]
    return (word & 0xFFFC) == 0xFFF8


def _find_sync(buffer: bytearray, start: int) -> int:
    index = buffer.find(b"\xFF", start)
    while index >= 0:
        if _is_frame_sync(buffer, index):
            return index
        index = buffer.find(b"\xFF", index + 1)
    return -1


def _block_size(buffer: bytearray, index: int) -> int:
    """按 FLAC 7.1 的 block size code 解析帧内采样数。"""
    if index + 4 > len(buffer):
        return 4096
    code = (buffer[index + 2] >> 4) & 0x0F
    if code == 1:
        return 192
    if 2 <= code <= 5:
        return 576 << (code - 2)
    if code == 6:
        return buffer[index + 4] + 1 if index + 5 <= len(buffer) else 4096
    if code == 7:
        if index + 6 <= len(buffer):
            return ((buffer[index + 4] << 8) | buffer[index + 5]) + 1
        return 4096
    if 8 <= code <= 15:
        return 256 << (code - 8)
    return 4096


def split_flac_frames(buffer: bytearray) -> list[tuple[bytes, int]]:
    frames: list[tuple[bytes, int]] = []

    while True:
        start = _find_sync(buffer, 0)
        if start < 0:
            break
        if start:
            del buffer[:start]
            start = 0

        next_start = _find_sync(buffer, 2)
        if next_start < 0:
            break  # 末帧可能还没收全

        samples = _block_size(buffer, start)
        frames.append((bytes(buffer[:next_start]), samples))
        del buffer[:next_start]

    return frames


class FlacEncoder(FfmpegAudioEncoderBase):
    name = "flac"
    codec = CodecId.FLAC.value

    ffmpeg_encoder = "flac"
    output_format = "flac"
    samples_per_frame = 4096

    def split_packets(self, buffer: bytearray) -> list[tuple[bytes, int]]:
        return split_flac_frames(buffer)
