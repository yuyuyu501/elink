"""WebRTC DataChannel 封装。

DataChannel 是 SCTP 上的可靠/半可靠消息通道，这里把它包成本项目的
``ControlChannel``（见 ``control_channel/datachannel.py``），上层就能无视
「控制消息到底走 TCP、WS 还是 DataChannel」。

两种可靠性模式：

- ``ordered=True``：控制事件必须保序（键鼠顺序错乱会直接表现为错字）；
- ``max_retransmits``：给「指针位置」这类高频事件留半可靠模式，
  丢几个旧位置无所谓，但别为了重传堵住后面的新位置。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

from backend.logging.logger import get_logger

logger = get_logger(__name__)


@dataclass
class DataChannelConfig:
    label: str = "control"
    ordered: bool = True
    max_retransmits: int = -1  # -1 表示完全可靠
    protocol: str = "elink-control"


class DataChannel:
    """持有底层 SCTP 通道的引用，统一收发接口。"""

    def __init__(self, config: DataChannelConfig | None = None, backend=None) -> None:
        self.config = config or DataChannelConfig()
        self.backend = backend
        self._listeners: dict[str, list[Callable]] = {}
        self._lock = threading.RLock()
        self.ready_state = "connecting"

    # ---- 事件 ----
    def on(self, event: str, callback: Callable) -> None:
        """支持 ``open`` / ``message`` / ``close`` / ``error``。"""
        with self._lock:
            self._listeners.setdefault(event, []).append(callback)

    def _emit(self, event: str, *args) -> None:
        with self._lock:
            listeners = list(self._listeners.get(event, ()))
        for listener in listeners:
            try:
                listener(*args)
            except Exception:
                logger.exception("DataChannel 事件回调异常: %s", event)

    # ---- 收发 ----
    def send(self, data: bytes | str) -> None:
        if self.ready_state != "open":
            raise RuntimeError(f"DataChannel 未就绪: {self.ready_state}")
        if self.backend is None:
            raise RuntimeError("DataChannel 没有底层实现")

        payload = data.encode("utf-8") if isinstance(data, str) else data
        sender = getattr(self.backend, "send", None)
        if sender is None:
            raise RuntimeError("底层实现缺少 send")
        sender(payload)

    def close(self) -> None:
        if self.backend is not None:
            closer = getattr(self.backend, "close", None)
            if callable(closer):
                closer()
        self.ready_state = "closed"
        self._emit("close")

    # ---- 状态回调（由底层栈调用） ----
    def notify_open(self) -> None:
        self.ready_state = "open"
        self._emit("open")

    def notify_message(self, payload: bytes | str) -> None:
        self._emit("message", payload)

    def notify_error(self, error: Exception) -> None:
        self._emit("error", error)

    def describe(self) -> str:
        return (
            f"DataChannel[{self.config.label}] ordered={self.config.ordered} "
            f"retransmits={self.config.max_retransmits} state={self.ready_state}"
        )


def create_control_channel(config: DataChannelConfig | None = None, backend=None):
    """构造一个可直接给控制协议用的通道。"""
    from backend.control_channel.datachannel import DataChannelControlChannel

    channel = DataChannel(config, backend)
    return DataChannelControlChannel(channel)
