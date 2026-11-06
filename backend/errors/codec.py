"""编解码相关异常。"""

from __future__ import annotations

from typing import Any

from backend.errors.base import ElinkError, ErrorCode


class CodecError(ElinkError):
    """编解码通用错误。"""

    code = ErrorCode.CODEC


class CodecNotFoundError(CodecError):
    """注册表中找不到指定编解码实现。"""


class CodecOpenError(CodecError):
    """编解码器初始化失败（后端不可用、参数非法等）。"""


class EncodeError(CodecError):
    """编码过程失败。"""


class DecodeError(CodecError):
    """解码过程失败。"""


class BitstreamError(CodecError):
    """码流解析失败（Annex-B 切分、参数集缺失等）。"""

    def __init__(self, message: str, *, detail: Any = None) -> None:
        super().__init__(message, detail=detail)
