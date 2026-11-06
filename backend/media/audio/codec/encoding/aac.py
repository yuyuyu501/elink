"""AAC 编码（``-c:a aac -f adts``）。

ADTS 每帧自带长度字段，因此可以精确切包：从头里读出 ``frame_length`` 与
``number_of_raw_data_blocks``，一帧就是 ``1024 * blocks`` 个采样。
"""

from __future__ import annotations

from backend.media.audio.codec.base import FfmpegAudioEncoderBase
from backend.media.stream import CodecId


def split_adts_frames(buffer: bytearray) -> list[tuple[bytes, int]]:
    """从缓冲区切出完整 ADTS 帧，返回 ``(字节, 采样数)``。"""
    frames: list[tuple[bytes, int]] = []

    while len(buffer) >= 7:
        if buffer[0] != 0xFF or (buffer[1] & 0xF0) != 0xF0:
            # 失步：跳到下一个同步字，丢弃前导垃圾字节
            index = buffer.find(b"\xFF")
            while index >= 0 and index + 1 < len(buffer) and (buffer[index + 1] & 0xF0) != 0xF0:
                index = buffer.find(b"\xFF", index + 1)
            if index < 0:
                buffer.clear()
                return frames
            del buffer[:index]
            continue

        frame_length = (
            ((buffer[3] & 0x03) << 11) | (buffer[4] << 3) | (buffer[5] >> 5)
        )
        if frame_length < 7:
            del buffer[:1]
            continue
        if len(buffer) < frame_length:
            break

        blocks = (buffer[6] & 0x03) + 1
        frames.append((bytes(buffer[:frame_length]), 1024 * blocks))
        del buffer[:frame_length]

    return frames


class AacEncoder(FfmpegAudioEncoderBase):
    name = "aac"
    codec = CodecId.AAC.value

    ffmpeg_encoder = "aac"
    output_format = "adts"
    samples_per_frame = 1024

    def split_packets(self, buffer: bytearray) -> list[tuple[bytes, int]]:
        return split_adts_frames(buffer)
