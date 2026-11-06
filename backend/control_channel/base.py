"""控制通道抽象。

TCP / WebSocket / DataChannel 三种承载共用这一份接口：上层只看到
``send_event`` / ``on_event``，不知道底下是 socket 还是 DataChannel。

语义约定（三种实现必须一致）：

- **不丢事件**：控制事件比媒体帧重要，队列满时阻塞而不是丢弃；
- **保序**：同一方向上的事件严格按调用顺序到达；
- **粘包无关**：实现内部必须处理流式拆包（TCP/WS），上层收到的一定是完整帧。
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from backend.control_channel import protocol
from backend.event.bus import EventBus
from backend.event.events import Event
from backend.logging.logger import get_logger

logger = get_logger(__name__)


class ChannelState(str):
    CLOSED = "closed"
    CONNECTING = "connecting"
    OPEN = "open"
    FAILED = "failed"


class ControlEvent(Event):
    """收到一条控制消息时投递的事件。"""

    channel: str = ""
    payload: Any = None


class ControlChannel(ABC):
    """控制通道接口。"""

    kind: str = ""

    def __init__(self, bus: EventBus | None = None) -> None:
        self.bus = bus
        self.state = ChannelState.CLOSED
        self._handlers: list[Callable[[Any], None]] = []
        self._lock = threading.RLock()
        self.sent_bytes = 0
        self.received_bytes = 0

    # ---- 生命周期 ----
    @abstractmethod
    def open(self) -> None:
        ...

    @abstractmethod
    def close(self) -> None:
        ...

    # ---- 收发 ----
    @abstractmethod
    def _write(self, data: bytes) -> None:
        ...

    def send_event(self, event: protocol.InputEvent) -> None:
        """发送输入事件（JSON 编码）。"""
        self.send_raw(protocol.encode_json(event))

    def send_command(self, command: protocol.ControlCommand) -> None:
        self.send_raw(protocol.encode_json(command))

    def send_raw(self, data: bytes) -> None:
        if self.state != ChannelState.OPEN:
            raise protocol.ProtocolError(f"控制通道未就绪: {self.state}")
        self._write(data)
        with self._lock:
            self.sent_bytes += len(data)

    def on_message(self, handler: Callable[[Any], None]) -> Callable[[], None]:
        """注册消息回调（在收包线程触发），返回取消函数。"""
        with self._lock:
            self._handlers.append(handler)

        def unsubscribe() -> None:
            with self._lock:
                if handler in self._handlers:
                    self._handlers.remove(handler)

        return unsubscribe

    def _dispatch(self, payload: bytes) -> None:
        self.received_bytes += len(payload)
        try:
            message = protocol.decode_json(payload)
        except protocol.ProtocolError:
            logger.exception("控制消息解析失败")
            return

        if self.bus is not None:
            self.bus.publish(ControlEvent(channel=self.kind, payload=message))

        with self._lock:
            handlers = list(self._handlers)
        for handler in handlers:
            try:
                handler(message)
            except Exception:
                logger.exception("控制消息回调异常")

    # ---- 便捷 ----
    def __enter__(self) -> "ControlChannel":
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def stats(self) -> dict[str, int]:
        return {
            "sent_bytes": self.sent_bytes,
            "received_bytes": self.received_bytes,
        }
