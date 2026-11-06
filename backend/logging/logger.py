"""日志器。

约定：模块内一律通过 ``get_logger(__name__)`` 取日志器，名字统一挂在 ``elink``
根日志器下，便于单点配置级别与输出端。
"""

from __future__ import annotations

import logging
import sys

from backend.logging.handlers import build_console_handler

ROOT_NAME = "elink"
_configured = False


def _prefer_utf8(stream) -> None:
    """Windows 控制台默认 GBK，中文日志会乱码；能改就改成 UTF-8。"""
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        reconfigure(encoding="utf-8", errors="replace")
    except (ValueError, OSError):
        pass


def setup_logging(level: int | str = logging.INFO, *, log_file: str | None = None) -> None:
    """初始化根日志器；重复调用只更新级别。"""
    global _configured

    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)

    _prefer_utf8(sys.stderr)
    _prefer_utf8(sys.stdout)

    root = logging.getLogger(ROOT_NAME)
    root.setLevel(level)
    root.propagate = False

    if not _configured:
        root.handlers.clear()
        root.addHandler(build_console_handler(level))
        _configured = True

    for handler in root.handlers:
        handler.setLevel(level)

    if log_file:
        from backend.logging.handlers import build_file_handler

        root.addHandler(build_file_handler(log_file, level))


def get_logger(name: str) -> logging.Logger:
    """取带 ``elink.`` 前缀的日志器。"""
    if name.startswith(f"{ROOT_NAME}."):
        return logging.getLogger(name)
    return logging.getLogger(f"{ROOT_NAME}.{name}")
