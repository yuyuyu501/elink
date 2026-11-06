"""Windows 桌面像素采集。

当前内部实现走 GDI ``BitBlt`` 通道：不依赖第三方库、在任何交互桌面会话都能
出图，适合先把 `Frame` 契约跑通。DXGI Desktop Duplication 的零拷贝通道尚未
接入，但对外契约（``open`` / ``read`` -> ``Frame`` / ``close``）与之一致，
替换实现时上层管线无需改动。

输出统一为 32 位 BGRA、自上而下的行序（与 ffmpeg 的 ``bgra`` 像素格式一致）。
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

from backend.config.schema import CaptureConfig
from backend.errors.base import ElinkError, ErrorCode
from backend.logging.logger import get_logger
from backend.media.frame import Frame, FrameDomain, PixelFormat
from backend.media.timebase import Timebase

logger = get_logger(__name__)

if not hasattr(ctypes, "WinDLL"):  # pragma: no cover - 非 Windows 平台直接拒绝
    raise ImportError("Windows 桌面采集只能在 Windows 上导入")

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000
DIB_RGB_COLORS = 0
SM_CXSCREEN = 0
SM_CYSCREEN = 1

HANDLE = wintypes.HANDLE
HDC = HANDLE
HBITMAP = HANDLE
HGDIOBJ = HANDLE


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BitmapInfo(ctypes.Structure):
    _fields_ = [("bmiHeader", _BitmapInfoHeader), ("bmiColors", wintypes.DWORD * 3)]


user32.GetDC.restype = HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.restype = ctypes.c_int
user32.ReleaseDC.argtypes = [wintypes.HWND, HDC]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.GetSystemMetrics.argtypes = [ctypes.c_int]

gdi32.CreateCompatibleDC.restype = HDC
gdi32.CreateCompatibleDC.argtypes = [HDC]
gdi32.CreateDIBSection.restype = HBITMAP
gdi32.CreateDIBSection.argtypes = [
    HDC,
    ctypes.POINTER(_BitmapInfo),
    wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p),
    HANDLE,
    wintypes.DWORD,
]
gdi32.SelectObject.restype = HGDIOBJ
gdi32.SelectObject.argtypes = [HDC, HGDIOBJ]
gdi32.BitBlt.restype = wintypes.BOOL
gdi32.BitBlt.argtypes = [
    HDC,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    HDC,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.DWORD,
]
gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.DeleteObject.argtypes = [HGDIOBJ]
gdi32.DeleteDC.restype = wintypes.BOOL
gdi32.DeleteDC.argtypes = [HDC]


class CaptureError(ElinkError):
    code = ErrorCode.IO


class DesktopCapture:
    """屏幕采集：``read()`` 产出一帧 BGRA 的 :class:`Frame`。"""

    backend = "gdi"

    def __init__(
        self,
        config: CaptureConfig | None = None,
        timebase: Timebase | None = None,
    ) -> None:
        self.config = config or CaptureConfig()
        self.timebase = timebase or Timebase.mpegts()
        self._screen_dc: int | None = None
        self._mem_dc: int | None = None
        self._bitmap: int | None = None
        self._bits: ctypes.c_void_p | None = None
        self._origin = (0, 0)
        self._width = 0
        self._height = 0
        self._start = 0.0

    # ---- 属性 ----
    @property
    def width(self) -> int:
        return self._width

    @property
    def height(self) -> int:
        return self._height

    @property
    def pixel_format(self) -> PixelFormat:
        return PixelFormat.BGRA

    # ---- 生命周期 ----
    def open(self) -> None:
        self._screen_dc = user32.GetDC(None)
        if not self._screen_dc:
            raise CaptureError("GetDC 失败", detail=ctypes.get_last_error())

        region = self.config.clamped_region()
        if region is None:
            width = user32.GetSystemMetrics(SM_CXSCREEN)
            height = user32.GetSystemMetrics(SM_CYSCREEN)
            region = (0, 0, width, height)

        x, y, width, height = region
        if width <= 0 or height <= 0:
            self.close()
            raise CaptureError(f"非法采集区域: {region}")

        info = _BitmapInfo()
        info.bmiHeader.biSize = ctypes.sizeof(_BitmapInfoHeader)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height  # 负值 = 自上而下
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = 0  # BI_RGB

        self._mem_dc = gdi32.CreateCompatibleDC(self._screen_dc)
        if not self._mem_dc:
            self.close()
            raise CaptureError("CreateCompatibleDC 失败", detail=ctypes.get_last_error())

        bits = ctypes.c_void_p()
        self._bitmap = gdi32.CreateDIBSection(
            self._mem_dc, ctypes.byref(info), DIB_RGB_COLORS, ctypes.byref(bits), None, 0
        )
        if not self._bitmap or not bits:
            self.close()
            raise CaptureError("CreateDIBSection 失败", detail=ctypes.get_last_error())

        if not gdi32.SelectObject(self._mem_dc, self._bitmap):
            self.close()
            raise CaptureError("SelectObject 失败", detail=ctypes.get_last_error())

        self._bits = bits
        self._origin = (x, y)
        self._width = width
        self._height = height
        self._start = time.perf_counter()
        logger.info(
            "屏幕采集就绪: 区域=(%d,%d) %dx%d 后端=%s", x, y, width, height, self.backend
        )

    def close(self) -> None:
        if self._mem_dc and self._bitmap:
            gdi32.SelectObject(self._mem_dc, None)
        if self._bitmap:
            gdi32.DeleteObject(self._bitmap)
            self._bitmap = None
        if self._mem_dc:
            gdi32.DeleteDC(self._mem_dc)
            self._mem_dc = None
        if self._screen_dc:
            user32.ReleaseDC(None, self._screen_dc)
            self._screen_dc = None
        self._bits = None

    # ---- 取帧 ----
    def read(self) -> Frame:
        if not self._screen_dc or not self._mem_dc or not self._bits:
            raise CaptureError("采集器未打开")

        x, y = self._origin
        if not gdi32.BitBlt(
            self._mem_dc,
            0,
            0,
            self._width,
            self._height,
            self._screen_dc,
            x,
            y,
            SRCCOPY | CAPTUREBLT,
        ):
            raise CaptureError("BitBlt 失败", detail=ctypes.get_last_error())

        data = ctypes.string_at(self._bits, self._width * self._height * 4)
        pts = self.timebase.ticks(time.perf_counter() - self._start)
        return Frame(
            data=data,
            timebase=self.timebase,
            pts=pts,
            width=self._width,
            height=self._height,
            pixel_format=PixelFormat.BGRA,
            domain=FrameDomain.CPU,
        )

    def __enter__(self) -> "DesktopCapture":
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
