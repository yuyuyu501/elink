"""Opus 编码（``-c:a libopus``）。

ffmpeg 的 opus 封装是 Ogg，所以包的边界是 Ogg 页。一页里可能有多条 Opus 包，
按 lacing 表（255 表示续包，<255 表示包尾）可以数出条数，再乘以每包 960 采样。
"""

from __future__ import annotations

from backend.media.audio.codec.base import FfmpegAudioEncoderBase
from backend.media.stream import CodecId

OGG_CAPTURE = b"OggS"
OPUS_SAMPLES_PER_PACKET = 960


def _count_packets(lacing: bytes) -> int:
    count = 0
    for value in lacing:
        if value < 255:
            count += 1
    return max(1, count)


def split_ogg_pages(buffer: bytearray) -> list[tuple[bytes, int]]:
    pages: list[tuple[bytes, int]] = []

    while True:
        if len(buffer) < 27:
            break
        if bytes(buffer[:4]) != OGG_CAPTURE:
            index = buffer.find(OGG_CAPTURE)
            if index < 0:
                buffer.clear()
                break
            del buffer[:index]
            continue

        segments = buffer[26]
        header_end = 27 + segments
        if len(buffer) < header_end:
            break
        page_size = header_end + sum(buffer[27:header_end])
        if len(buffer) < page_size:
            break

        packets = _count_packets(bytes(buffer[27:header_end]))
        pages.append((bytes(buffer[:page_size]), OPUS_SAMPLES_PER_PACKET * packets))
        del buffer[:page_size]

    return pages


class LibOpusEncoder(FfmpegAudioEncoderBase):
    name = "libopus"
    codec = CodecId.OPUS.value

    ffmpeg_encoder = "libopus"
    output_format = "ogg"
    samples_per_frame = OPUS_SAMPLES_PER_PACKET

    def split_packets(self, buffer: bytearray) -> list[tuple[bytes, int]]:
        return split_ogg_pages(buffer)
