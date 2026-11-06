"""事件总线。

订阅方按事件类型注册；``publish`` 在当前线程同步派发，派发过程对订阅表加锁，
因此订阅方回调内不应再长时间阻塞，也不应在回调内再次 publish（避免长链）。
"""

from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Callable
from typing import TypeVar

from backend.event.events import Event
from backend.logging.logger import get_logger

E = TypeVar("E", bound=Event)
Handler = Callable[[Event], None]

logger = get_logger(__name__)


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[type[Event], list[Handler]] = defaultdict(list)
        self._lock = threading.RLock()

    def subscribe(self, event_type: type[E], handler: Handler) -> Callable[[], None]:
        """注册订阅，返回取消订阅函数。"""
        with self._lock:
            self._handlers[event_type].append(handler)

        def unsubscribe() -> None:
            with self._lock:
                if handler in self._handlers.get(event_type, []):
                    self._handlers[event_type].remove(handler)

        return unsubscribe

    def publish(self, event: Event) -> None:
        """派发到所有匹配订阅方（含基类 Event 订阅）。"""
        with self._lock:
            handlers = [
                *self._handlers.get(type(event), ()),
                *self._handlers.get(Event, ()),
            ]
        for handler in handlers:
            try:
                handler(event)
            except Exception:  # 单个订阅方异常不能中断管线
                logger.exception("事件处理失败: %s", event.name)

    def clear(self) -> None:
        with self._lock:
            self._handlers.clear()
