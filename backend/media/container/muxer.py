"""封装器抽象。

封装层只做一件事：把 :class:`~backend.media.packet.Packet` 序列化成字节流，
写入一个「可写的二进制 sink」。sink 是文件还是网络通道由上层决定，封装层
不感知传输细节。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import BinaryIO

from backend.media.packet import Packet
from backend.media.stream import StreamInfo


class Muxer(ABC):
    """封装器接口。"""

    format: str = ""

    def __init__(self, sink: BinaryIO) -> None:
        self.sink = sink
        self.streams: list[StreamInfo] = []
        self._opened = False

    @abstractmethod
    def open(self, streams: list[StreamInfo]) -> None:
        """写入文件头/表结构，之后才可以 write。"""

    @abstractmethod
    def write(self, packet: Packet) -> int:
        """写入一个包，返回写入的字节数。"""

    def close(self) -> None:
        """冲刷并结束；默认只刷新 sink。"""
        self._opened = False
        try:
            self.sink.flush()
        except (AttributeError, ValueError):
            pass

    def __enter__(self) -> "Muxer":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
