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
    background: bytes | None = None

    @property
    def is_empty(self) -> bool:
        return not self.visible or not self.width or not self.height


class CursorCapture:
    """读取当前指针位置与位图（Windows）。"""

    backend = "win32"

    def __init__(self, include_image: bool = True) -> None:
        self.include_image = include_image
        self._handle = None
        self._shape = None

    def read(self) -> CursorState:
        if not hasattr(ctypes, "WinDLL"):
            raise CursorError("指针捕获当前只实现了 Windows 路径")

        return self._read_win32()

    def _read_win32(self) -> CursorState:
        from backend.media.video.capture.windows.dxgi_desktop_duplication import (
            user32, gdi32, _BitmapInfo, _BitmapInfoHeader,
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

        class BITMAP(ctypes.Structure):
            _fields_ = [("type", wintypes.LONG), ("width", wintypes.LONG),
                        ("height", wintypes.LONG), ("stride", wintypes.LONG),
                        ("planes", wintypes.WORD), ("bits", wintypes.WORD),
                        ("data", ctypes.c_void_p)]

        user32.GetCursorInfo.argtypes = [ctypes.POINTER(CURSORINFO)]
        user32.GetCursorInfo.restype = wintypes.BOOL
        user32.GetIconInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(ICONINFO)]
        user32.GetIconInfo.restype = wintypes.BOOL
        user32.GetPhysicalCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        user32.GetPhysicalCursorPos.restype = wintypes.BOOL
        user32.DrawIconEx.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int,
                                     wintypes.HANDLE, ctypes.c_int, ctypes.c_int,
                                     wintypes.UINT, wintypes.HBRUSH, wintypes.UINT]
        user32.DrawIconEx.restype = wintypes.BOOL
        gdi32.GetObjectW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p]
        gdi32.GetObjectW.restype = ctypes.c_int
        info = CURSORINFO()
        info.cbSize = ctypes.sizeof(CURSORINFO)
        if not user32.GetCursorInfo(ctypes.byref(info)):
            raise CursorError("GetCursorInfo 失败", detail=ctypes.get_last_error())

        # CURSOR_SHOWING = 1
        if not info.flags & 0x00000001:
            return CursorState(x=0, y=0, visible=False)

        user32.GetPhysicalCursorPos(ctypes.byref(info.ptScreenPos))
        x, y = info.ptScreenPos.x, info.ptScreenPos.y
        if not self.include_image:
            return CursorState(x=x, y=y)

        if info.hCursor != self._handle or self._shape is None:
            icon = ICONINFO()
            if not user32.GetIconInfo(info.hCursor, ctypes.byref(icon)):
                return CursorState(x=x, y=y)
            dc = dib = previous = None
            try:
                bitmap = BITMAP()
                if not gdi32.GetObjectW(icon.hbmColor or icon.hbmMask,
                                       ctypes.sizeof(bitmap), ctypes.byref(bitmap)):
                    return CursorState(x=x, y=y)
                width, height = bitmap.width, bitmap.height // (1 if icon.hbmColor else 2)
                if not (0 < width <= 1024 and 0 < height <= 1024):
                    return CursorState(x=x, y=y)
                dc = gdi32.CreateCompatibleDC(None)
                header = _BitmapInfo()
                header.bmiHeader = _BitmapInfoHeader(
                    biSize=ctypes.sizeof(_BitmapInfoHeader), biWidth=width,
                    biHeight=-height, biPlanes=1, biBitCount=32)
                bits = ctypes.c_void_p()
                dib = gdi32.CreateDIBSection(dc, ctypes.byref(header), 0,
                                           ctypes.byref(bits), None, 0)
                if not dc or not dib or not bits:
                    raise CursorError("光标位图创建失败")
                previous = gdi32.SelectObject(dc, dib)
                images = []
                # Rendering on black and white preserves alpha and AND/XOR cursors.
                for fill in (0, 255):
                    ctypes.memset(bits, fill, width * height * 4)
                    if not user32.DrawIconEx(dc, 0, 0, info.hCursor, width, height, 0, None, 3):
                        raise CursorError("光标绘制失败")
                    gdi32.GdiFlush()
                    images.append(ctypes.string_at(bits, width * height * 4))
                self._shape = (width, height, icon.xHotspot, icon.yHotspot, *images)
                self._handle = info.hCursor
            finally:
                if previous:
                    gdi32.SelectObject(dc, previous)
                for obj in (dib, icon.hbmColor, icon.hbmMask):
                    if obj:
                        gdi32.DeleteObject(obj)
                if dc:
                    gdi32.DeleteDC(dc)
        width, height, hx, hy, image, background = self._shape
        return CursorState(x, y, width, height, hx, hy, True, image, background)

    def close(self) -> None:
        self._shape = self._handle = None
