"""解封装器抽象。

解封装层只做一件事：把字节流还原成 :class:`~backend.media.packet.Packet`，
数据源是一个「可读的二进制 source」，不感知它来自文件还是 SRT。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import BinaryIO

from backend.media.packet import Packet
from backend.media.stream import StreamInfo


class Demuxer(ABC):
    """解封装器接口：``read`` 返回 ``None`` 表示流结束。"""

    format: str = ""

    def __init__(self, source: BinaryIO) -> None:
        self.source = source
        self._streams: list[StreamInfo] = []
        self._opened = False

    @abstractmethod
    def open(self) -> None:
        """读取头部/表结构，填充 ``streams``。"""

    @abstractmethod
    def read(self) -> Packet | None:
        """读出下一个包；无更多数据返回 None。"""

    @property
    def streams(self) -> list[StreamInfo]:
        return list(self._streams)

    def close(self) -> None:
        self._opened = False

    def __enter__(self) -> "Demuxer":
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
