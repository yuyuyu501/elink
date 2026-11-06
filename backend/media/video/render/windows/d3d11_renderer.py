"""Windows 窗口渲染。

说明清楚当前实现边界：D3D11 **交换链**上屏（``IDXGISwapChain`` + 纹理拷贝）
需要完整的 COM/纹理封装，尚未接入；这里提供的是同一套 ``open/render/close``
契约下的 **GDI 窗口上屏**（``StretchDIBits``），用于把解码帧真实显示出来、
验证 ``Frame`` 契约与配色（BGRA 通道顺序、行序）是否正确。

``gpu_available`` 会探测 ``d3d11.dll``，交换链接入后只需替换 :meth:`render`
内部实现，上层管线不用改。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

from backend.errors.base import ElinkError, ErrorCode
from backend.logging.logger import get_logger
from backend.media.frame import Frame, PixelFormat

logger = get_logger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_VISIBLE = 0x10000000
SW_SHOW = 5
CS_HREDRAW = 0x0002
CS_VREDRAW = 0x0001
COLOR_WINDOW = 5
DIB_RGB_COLORS = 0
BI_RGB = 0
SRCCOPY = 0x00CC0020
PM_REMOVE = 0x0001
WM_DESTROY = 0x0002


class RenderError(ElinkError):
    code = ErrorCode.IO


class _WNDCLASSEX(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


class _BITMAPINFOHEADER(ctypes.Structure):
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


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class _POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class _MSG(ctypes.Structure):
    _fields_ = [
        ("hWnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", _POINT),
    ]


_WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)

user32.DefWindowProcW.restype = ctypes.c_void_p
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
]
gdi32.StretchDIBits.argtypes = [
    wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ctypes.c_void_p, ctypes.POINTER(_BITMAPINFO), wintypes.UINT, wintypes.DWORD,
]


def gpu_available() -> bool:
    """D3D11 运行时是否可用（仅表示系统具备，不代表已接入交换链）。"""
    try:
        ctypes.WinDLL("d3d11")
    except OSError:
        return False
    return True


class WindowPresenter:
    """把 ``Frame`` 画到原生窗口上。"""

    name = "window_presenter"

    def __init__(self, title: str = "elink", width: int = 0, height: int = 0) -> None:
        self.title = title
        self.width = width
        self.height = height
        self._hwnd: int | None = None
        self._wndproc = _WNDPROC(self._on_message)
        self._frames_rendered = 0

    def open(self) -> None:
        instance = user32.GetModuleHandleW(None)
        class_name = f"elink_render_{id(self)}"

        window_class = _WNDCLASSEX()
        window_class.cbSize = ctypes.sizeof(_WNDCLASSEX)
        window_class.style = CS_HREDRAW | CS_VREDRAW
        window_class.lpfnWndProc = ctypes.cast(self._wndproc, ctypes.c_void_p)
        window_class.hInstance = instance
        window_class.hbrBackground = gdi32.GetStockObject(COLOR_WINDOW)
        window_class.lpszClassName = class_name

        if not user32.RegisterClassExW(ctypes.byref(window_class)):
            raise RenderError("RegisterClassExW 失败", detail=ctypes.get_last_error())

        self._hwnd = user32.CreateWindowExW(
            0,
            class_name,
            self.title,
            WS_OVERLAPPEDWINDOW | WS_VISIBLE,
            100, 100,
            self.width or 640,
            self.height or 360,
            None, None, instance, None,
        )
        if not self._hwnd:
            raise RenderError("CreateWindowExW 失败", detail=ctypes.get_last_error())

        user32.ShowWindow(self._hwnd, SW_SHOW)
        logger.info("渲染窗口就绪: %s (GPU 交换链未接入，当前 GDI 上屏)", self.title)

    def render(self, frame: Frame) -> bool:
        if self._hwnd is None:
            raise RenderError("渲染器尚未打开")
        if frame.pixel_format not in (PixelFormat.BGRA, PixelFormat.RGBA):
            raise RenderError(f"窗口渲染需要 32 位像素格式，收到 {frame.pixel_format}")

        rect = _RECT()
        user32.GetClientRect(self._hwnd, ctypes.byref(rect))
        client_width = max(1, rect.right - rect.left) or frame.width
        client_height = max(1, rect.bottom - rect.top) or frame.height

        info = _BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        info.bmiHeader.biWidth = frame.width
        info.bmiHeader.biHeight = -frame.height  # 自上而下
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = BI_RGB

        device_context = user32.GetDC(self._hwnd)
        if not device_context:
            raise RenderError("GetDC 失败")
        try:
            gdi32.StretchDIBits(
                device_context,
                0, 0, client_width, client_height,
                0, 0, frame.width, frame.height,
                ctypes.c_char_p(bytes(frame.data)),
                ctypes.byref(info),
                DIB_RGB_COLORS,
                SRCCOPY,
            )
        finally:
            user32.ReleaseDC(self._hwnd, device_context)

        self._frames_rendered += 1
        self.pump()
        return True

    def pump(self) -> None:
        """抽干消息队列，避免窗口「未响应」。"""
        message = _MSG()
        while user32.PeekMessageW(ctypes.byref(message), None, 0, 0, PM_REMOVE):
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))

    def close(self) -> None:
        if self._hwnd is not None:
            user32.DestroyWindow(self._hwnd)
            self._hwnd = None
        logger.info("渲染窗口关闭，共显示 %d 帧", self._frames_rendered)

    def _on_message(self, hwnd, message, wparam, lparam):  # pragma: no cover - 回调
        if message == WM_DESTROY:
            self._hwnd = None
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def __enter__(self) -> "WindowPresenter":
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


class D3D11Renderer(WindowPresenter):
    """D3D11 渲染端（对外契约同窗口渲染，交换链尚未接入）。"""

    name = "d3d11"

    @staticmethod
    def available() -> bool:
        return gpu_available()
