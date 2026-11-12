"""桌面端路径解析（源码运行 / PyInstaller 冻结都要能工作）。

冻结后资源在 ``sys._MEIPASS`` 下，数据目录必须在用户可写位置——
把可执行文件所在目录当数据目录会在 ``C:\\Program Files`` 下直接失败。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "Elink"
#: 数据目录名。刻意与 E:\elink 桌面端（``%LOCALAPPDATA%\Elink``）区分开：
#: 两个应用的设置/日志/身份文件不能混在一个目录里，否则会互相覆盖。
APP_ID = "elink-remote"


def bundle_dir() -> Path:
    """程序根目录：源码运行时是项目根，冻结后是 exe 所在目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def resource_dir() -> Path:
    """只读资源目录（图标等）。"""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "frontend" / "desktop" / "resources"
    return Path(__file__).resolve().parent / "resources"


def data_dir() -> Path:
    """用户数据目录：日志、设置、截图。"""
    override = os.environ.get("ELINK_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform.startswith("linux"):
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        return base / APP_ID
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / APP_NAME


def ensure_dirs(root: Path) -> None:
    for name in ("logs", "captures", "config", "error"):
        (root / name).mkdir(parents=True, exist_ok=True)


def settings_file() -> Path:
    return data_dir() / "config" / "desktop.json"


def log_file() -> Path:
    return data_dir() / "logs" / "desktop.log"


def error_dir() -> Path:
    return data_dir() / "error"
