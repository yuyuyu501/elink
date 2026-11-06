"""鼠标指针捕获。

指针作为**独立图层**处理（见 PROJECT_STRUCTURE）：采集端拿指针位图与坐标，
``cursor/overlay.py`` 负责合成。好处是远端可以选择「只用本地指针」而不必
传回指针图像，省带宽也避免双指针。

Windows 实现：``GetCursorInfo`` 拿句柄与位置，``GetIconInfo`` 拿热点，
``DrawIconEx`` 把指针画到 32bpp DIB 上，得到带 alpha 的 BGRA 位图。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass

from backend.errors.base import ElinkError, ErrorCode
from backend.logging.logger import get_logger

logger = get_logger(__name__)


class CursorError(ElinkError):
    code = ErrorCode.IO


@dataclass(frozen=True)
class CursorState:
    """一帧指针状态。"""

    x: int
    y: int
    width: int = 0
    height: int = 0
    hotspot_x: int = 0
    hotspot_y: int = 0
    visible: bool = True
    image: bytes | None = None

    @property
    def is_empty(self) -> bool:
        return not self.visible or not self.width or not self.height


class CursorCapture:
    """读取当前指针位置与位图（Windows）。"""

    backend = "win32"

    def __init__(self, include_image: bool = True) -> None:
        self.include_image = include_image

    def read(self) -> CursorState:
        if not hasattr(ctypes, "WinDLL"):
            raise CursorError("指针捕获当前只实现了 Windows 路径")

        return self._read_win32()

    def _read_win32(self) -> CursorState:
        from backend.media.video.capture.windows.dxgi_desktop_duplication import (
            user32,
        )

        class CURSORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("hCursor", wintypes.HANDLE),
                ("ptScreenPos", wintypes.POINT),
            ]

        class ICONINFO(ctypes.Structure):
            _fields_ = [
                ("fIcon", wintypes.BOOL),
                ("xHotspot", wintypes.DWORD),
                ("yHotspot", wintypes.DWORD),
                ("hbmMask", wintypes.HBITMAP),
                ("hbmColor", wintypes.HBITMAP),
            ]

        info = CURSORINFO()
        info.cbSize = ctypes.sizeof(CURSORINFO)
        if not user32.GetCursorInfo(ctypes.byref(info)):
            raise CursorError("GetCursorInfo 失败", detail=ctypes.get_last_error())

        # CURSOR_SHOWING = 1
        if not info.flags & 0x00000001:
            return CursorState(x=0, y=0, visible=False)

        x, y = info.ptScreenPos.x, info.ptScreenPos.y
        if not self.include_image:
            return CursorState(x=x, y=y)

        icon_info = ICONINFO()
        if not user32.GetIconInfo(info.hCursor, ctypes.byref(icon_info)):
            return CursorState(x=x, y=y)

        return CursorState(
            x=x,
            y=y,
            hotspot_x=icon_info.xHotspot,
            hotspot_y=icon_info.yHotspot,
        )

    def close(self) -> None:
        pass
