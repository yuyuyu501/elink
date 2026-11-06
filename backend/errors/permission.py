"""权限与能力相关异常。"""

from __future__ import annotations

from backend.errors.base import ElinkError, ErrorCode


class PermissionError(ElinkError):  # noqa: A001 - 与内置异常同名但语义更窄
    """本端缺少系统权限（录屏、麦克风、输入监听等）。"""

    code = ErrorCode.PERMISSION


class PermissionDeniedError(PermissionError):
    """用户或系统明确拒绝授权。"""


class PermissionRequestError(PermissionError):
    """授权流程无法发起（缺少交互会话、系统不支持等）。"""


class CapabilityNotSupportedError(PermissionError):
    """平台不具备该能力（例如 Linux 上请求 D3D11）。"""

    code = ErrorCode.NOT_SUPPORTED
