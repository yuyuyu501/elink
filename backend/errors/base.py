"""统一错误基类。

所有模块抛出的异常都应继承 :class:`ElinkError`，并带上 :class:`ErrorCode`，
以便上层按类别决定「重试 / 回退 / 上报」。
"""

from __future__ import annotations

import enum
from typing import Any


class ErrorCode(str, enum.Enum):
    """错误大类。"""

    UNKNOWN = "unknown"
    INVALID_ARGUMENT = "invalid_argument"
    INVALID_STATE = "invalid_state"
    NOT_SUPPORTED = "not_supported"
    IO = "io"
    CODEC = "codec"
    NETWORK = "network"
    PERMISSION = "permission"


class ElinkError(Exception):
    """项目内所有异常的根。"""

    code: ErrorCode = ErrorCode.UNKNOWN

    def __init__(self, message: str, *, detail: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def __str__(self) -> str:  # pragma: no cover - 仅影响日志可读性
        if self.detail in (None, ""):
            return f"[{self.code.value}] {self.message}"
        return f"[{self.code.value}] {self.message} ({self.detail})"
