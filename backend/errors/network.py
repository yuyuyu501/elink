"""网络与传输相关异常。"""

from __future__ import annotations

from backend.errors.base import ElinkError, ErrorCode


class NetworkError(ElinkError):
    """网络层通用错误。"""

    code = ErrorCode.NETWORK


class TransportError(NetworkError):
    """传输通道收发失败。"""


class TransportClosedError(TransportError):
    """通道已关闭或对端断开。"""


class TransportTimeoutError(TransportError):
    """通道建立或读写超时。"""


class CapabilityMismatchError(NetworkError):
    """能力协商无交集。"""
