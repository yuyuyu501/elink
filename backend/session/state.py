"""会话状态机。

状态与允许的迁移集中在一处，避免「某个模块把状态随手改成 connected」这类事故。
之所以要状态机而不是若干布尔量：远程控制里「已连接但正在重连」和
「正在协商」是完全不同的行为分支，用布尔组合迟早会漏。

    idle -> connecting -> negotiating -> streaming -> reconnecting -> closed
                                          \\-> failed
"""

from __future__ import annotations

import enum
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from backend.errors.base import ElinkError, ErrorCode
from backend.logging.logger import get_logger

logger = get_logger(__name__)


class SessionState(str, enum.Enum):
    IDLE = "idle"
    CONNECTING = "connecting"
    NEGOTIATING = "negotiating"
    STREAMING = "streaming"
    RECONNECTING = "reconnecting"
    CLOSED = "closed"
    FAILED = "failed"


_TRANSITIONS: dict[SessionState, frozenset[SessionState]] = {
    SessionState.IDLE: frozenset({SessionState.CONNECTING, SessionState.CLOSED}),
    SessionState.CONNECTING: frozenset(
        {SessionState.NEGOTIATING, SessionState.RECONNECTING, SessionState.FAILED, SessionState.CLOSED}
    ),
    SessionState.NEGOTIATING: frozenset(
        {SessionState.STREAMING, SessionState.FAILED, SessionState.CLOSED}
    ),
    SessionState.STREAMING: frozenset(
        {SessionState.RECONNECTING, SessionState.CLOSED, SessionState.FAILED}
    ),
    SessionState.RECONNECTING: frozenset(
        {SessionState.CONNECTING, SessionState.FAILED, SessionState.CLOSED}
    ),
    SessionState.FAILED: frozenset({SessionState.CLOSED, SessionState.CONNECTING}),
    SessionState.CLOSED: frozenset(),
}


class InvalidTransitionError(ElinkError):
    code = ErrorCode.INVALID_STATE


@dataclass
class StateChange:
    from_state: SessionState
    to_state: SessionState
    reason: str = ""
    timestamp: float = field(default_factory=time.monotonic)


class SessionStateMachine:
    """线程安全的状态机；迁移会通知监听者。"""

    def __init__(self, initial: SessionState = SessionState.IDLE) -> None:
        self._state = initial
        self._lock = threading.RLock()
        self._listeners: list[Callable[[StateChange], None]] = []
        self.history: list[StateChange] = []

    @property
    def state(self) -> SessionState:
        with self._lock:
            return self._state

    @property
    def is_active(self) -> bool:
        with self._lock:
            return self._state in {
                SessionState.CONNECTING,
                SessionState.NEGOTIATING,
                SessionState.STREAMING,
                SessionState.RECONNECTING,
            }

    def on_change(self, listener: Callable[[StateChange], None]) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return unsubscribe

    def can_transition(self, target: SessionState) -> bool:
        with self._lock:
            return target in _TRANSITIONS[self._state]

    def transition(self, target: SessionState, reason: str = "") -> StateChange:
        with self._lock:
            current = self._state
            if target == current:
                return StateChange(current, target, reason or "noop")
            if target not in _TRANSITIONS[current]:
                raise InvalidTransitionError(
                    f"非法会话状态迁移: {current.value} -> {target.value}",
                    detail=reason,
                )
            self._state = target
            change = StateChange(current, target, reason)
            self.history.append(change)
            listeners = list(self._listeners)

        logger.info("会话状态: %s -> %s%s", current.value, target.value, f" ({reason})" if reason else "")
        for listener in listeners:
            try:
                listener(change)
            except Exception:
                logger.exception("状态监听回调异常")
        return change

    def describe(self) -> str:
        with self._lock:
            return f"{self._state.value} (历史 {len(self.history)} 次迁移)"
