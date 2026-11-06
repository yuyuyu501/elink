"""开机自启动（Windows 当前用户 Run 键）。

只写当前用户、只写一条 ``Elink`` 值；不碰系统级服务、不安装计划任务。
关闭时删除该值。非 Windows 或权限不足时返回 False，由界面提示，不静默假装成功。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from backend.logging.logger import get_logger
from .paths import bundle_dir

logger = get_logger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "Elink"


def startup_command() -> str:
    """返回写入 Run 键的完整命令。"""
    executable = Path(sys.executable)
    if getattr(sys, "frozen", False):
        return f'"{executable}" --ui'
    main = bundle_dir() / "main.py"
    return f'"{executable}" "{main}" --ui'


def _winreg():
    if not sys.platform.startswith("win"):
        return None
    try:
        import winreg  # noqa: PLC0415 - 仅在 Windows 导入

        return winreg
    except ImportError:
        return None


def is_enabled() -> bool:
    winreg = _winreg()
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, VALUE_NAME)
        return str(value).strip().lower() == startup_command().strip().lower()
    except OSError:
        return False


def set_enabled(enabled: bool) -> bool:
    """写入/删除启动项，返回操作后实际状态。"""
    winreg = _winreg()
    if winreg is None:
        logger.warning("当前平台不支持开机自启动")
        return False
    try:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE
        ) as key:
            if enabled:
                winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, startup_command())
            else:
                try:
                    winreg.DeleteValue(key, VALUE_NAME)
                except FileNotFoundError:
                    pass
    except OSError:
        logger.warning("开机自启动设置失败", exc_info=True)
        return is_enabled()
    return is_enabled()


def supports_startup() -> bool:
    return os.name == "nt"
