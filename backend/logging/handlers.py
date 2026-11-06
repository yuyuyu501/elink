"""日志输出端构造。"""

from __future__ import annotations

import logging
import sys

DEFAULT_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-5s [%(name)s] %(message)s"
DEFAULT_DATEFMT = "%H:%M:%S"


def build_console_handler(
    level: int = logging.INFO,
    stream=None,
) -> logging.Handler:
    """控制台输出端；stdout 为管道时同样可用。"""
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(DEFAULT_FORMAT, DEFAULT_DATEFMT))
    return handler


def build_file_handler(path: str, level: int = logging.DEBUG) -> logging.Handler:
    """文件输出端。"""
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(DEFAULT_FORMAT, DEFAULT_DATEFMT))
    return handler
