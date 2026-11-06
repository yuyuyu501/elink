"""显示器信息。

采集区域、指针坐标换算、多屏拼接都依赖这里：远端看到的是「某台显示器的
某块区域」，所以必须能枚举出每台显示器的物理坐标与分辨率，而不是只拿主屏尺寸。

Windows 用 ``EnumDisplayMonitors`` 枚举；其它平台退回主屏尺寸（够用但不完整）。
"""

from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass

from backend.logging.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class DisplayInfo:
    """一台显示器（左上角坐标 + 尺寸，单位为物理像素）。"""

    index: int
    name: str
    x: int
    y: int
    width: int
    height: int
    primary: bool = False
    scale: float = 1.0

    @property
    def region(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.width, self.height)

    def describe(self) -> str:
        flag = "主屏" if self.primary else "副屏"
        return f"#{self.index} {self.name} ({flag}) {self.width}x{self.height}+{self.x}+{self.y}"


def list_displays() -> list[DisplayInfo]:
    """枚举全部显示器；失败时至少返回一台主屏。"""
    if sys.platform.startswith("win"):
        displays = _list_windows_displays()
        if displays:
            return displays
    return _primary_display()


def primary_display() -> DisplayInfo:
    displays = list_displays()
    for display in displays:
        if display.primary:
            return display
    return displays[0]


def _primary_display() -> DisplayInfo:
    width, height = 1920, 1080
    scale = 1.0
    if sys.platform.startswith("win"):
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        width = user32.GetSystemMetrics(0) or width
        height = user32.GetSystemMetrics(1) or height
        try:
            scale = ctypes.WinDLL("shcore").GetScaleFactorForDevice(0) / 100.0
        except (OSError, AttributeError):
            scale = 1.0
    return DisplayInfo(
        index=0, name="primary", x=0, y=0, width=width, height=height, primary=True, scale=scale
    )


def _list_windows_displays() -> list[DisplayInfo]:  # pragma: no cover - 平台相关
    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_ulong),
            ("rcMonitor", RECT),
            ("rcWork", RECT),
            ("dwFlags", ctypes.c_ulong),
            ("szDevice", ctypes.c_wchar * 32),
        ]

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    monitors: list[DisplayInfo] = []
    monitor_callback = ctypes.WINFUNCTYPE(
        ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(RECT), ctypes.c_double
    )

    def callback(hmonitor, hdc, rect, data):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
            width = info.rcMonitor.right - info.rcMonitor.left
            height = info.rcMonitor.bottom - info.rcMonitor.top
            try:
                shcore = ctypes.WinDLL("shcore")
                shcore.GetDpiForMonitor(hmonitor, 0, ctypes.byref(ctypes.c_uint()), ctypes.byref(ctypes.c_uint()))
            except (OSError, AttributeError):
                pass
            monitors.append(
                DisplayInfo(
                    index=len(monitors),
                    name=info.szDevice or f"monitor{len(monitors)}",
                    x=info.rcMonitor.left,
                    y=info.rcMonitor.top,
                    width=width,
                    height=height,
                    # MONITORINFOF_PRIMARY = 0x1
                    primary=bool(info.dwFlags & 0x1),
                )
            )
        return 1

    try:
        user32.EnumDisplayMonitors(None, None, monitor_callback(callback), 0)
    except OSError:
        logger.debug("EnumDisplayMonitors 失败", exc_info=True)
        return []

    if not monitors:
        return []
    if not any(monitor.primary for monitor in monitors):
        monitors[0] = DisplayInfo(
            index=monitors[0].index,
            name=monitors[0].name,
            x=monitors[0].x,
            y=monitors[0].y,
            width=monitors[0].width,
            height=monitors[0].height,
            primary=True,
            scale=monitors[0].scale,
        )
    return monitors


def virtual_desktop_region() -> tuple[int, int, int, int]:
    """全部显示器拼成的虚拟桌面区域（多屏采集用）。"""
    displays = list_displays()
    left = min(display.x for display in displays)
    top = min(display.y for display in displays)
    right = max(display.x + display.width for display in displays)
    bottom = max(display.y + display.height for display in displays)
    return (left, top, right - left, bottom - top)
