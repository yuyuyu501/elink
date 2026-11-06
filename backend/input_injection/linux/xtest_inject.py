"""Linux 输入注入（X11 XTest）。

XTest 是 X11 的合成输入扩展：把事件塞进 X 服务端的事件队列，效果等同于真实
按键。优点是简单可靠，缺点是**只对 X11 生效**（Wayland 下没有 XTest 语义，
需要用 uinput）。

用 ctypes 调 libXtst，而不是绑 python-xlib：只需要 4 个函数，绑定成本更低，
也避免引入额外依赖。
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import time

from backend.control_channel import protocol
from backend.errors.base import ElinkError, ErrorCode
from backend.logging.logger import get_logger

logger = get_logger(__name__)


class InjectionError(ElinkError):
    code = ErrorCode.IO


def _load(name: str) -> ctypes.CDLL:
    path = ctypes.util.find_library(name)
    if not path:
        raise InjectionError(f"未找到 {name} 库")
    return ctypes.CDLL(path)


class XTestInjector:
    """X11 下的键鼠注入。"""

    backend = "xtest"

    def __init__(self, display: str = "") -> None:
        self.display_name = display or os.environ.get("DISPLAY", ":0")
        self._display = None
        self._x11 = None
        self._xtst = None
        self.injected = 0

    def open(self) -> None:
        if os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("DISPLAY"):
            raise InjectionError(
                "Wayland 会话没有 XTest 语义，请改用 uinput 注入（uinput_inject.py）"
            )

        self._x11 = _load("X11")
        self._xtst = _load("Xtst")

        self._x11.XOpenDisplay.restype = ctypes.c_void_p
        self._x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self._x11.XFlush.argtypes = [ctypes.c_void_p]
        self._x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        self._x11.XWarpPointer.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
            ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint,
            ctypes.c_int, ctypes.c_int,
        ]
        self._xtst.XTestFakeButtonEvent.argtypes = [
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong
        ]
        self._xtst.XTestFakeKeyEvent.argtypes = [
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong
        ]
        self._xtst.XTestFakeMotionEvent.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_ulong
        ]

        display = self._x11.XOpenDisplay(self.display_name.encode())
        if not display:
            raise InjectionError(f"无法打开 X display: {self.display_name}")
        self._display = display
        logger.info("XTest 注入就绪（display=%s）", self.display_name)

    def close(self) -> None:
        if self._display is not None and self._x11 is not None:
            self._x11.XCloseDisplay(self._display)
            self._display = None

    def inject(self, event: protocol.InputEvent) -> bool:
        if self._display is None:
            raise InjectionError("XTest 未初始化")

        if event.event_type is protocol.EventType.MOUSE_MOVE:
            self._move(event.x, event.y)
        elif event.event_type is protocol.EventType.MOUSE_BUTTON:
            self._move(event.x, event.y)
            self._xtst.XTestFakeButtonEvent(
                self._display, event.button, 1 if event.pressed else 0, 0
            )
        elif event.event_type is protocol.EventType.MOUSE_WHEEL:
            button = 4 if event.delta > 0 else 5
            self._xtst.XTestFakeButtonEvent(self._display, button, 1, 0)
            self._xtst.XTestFakeButtonEvent(self._display, button, 0, 0)
        elif event.event_type is protocol.EventType.KEY:
            self._xtst.XTestFakeKeyEvent(
                self._display, event.scan_code or event.key_code, 1 if event.pressed else 0, 0
            )
        else:
            return False

        self._x11.XFlush(self._display)
        self.injected += 1
        return True

    def _move(self, x: int, y: int) -> None:
        self._xtst.XTestFakeMotionEvent(self._display, -1, x, y, 0)
        time.sleep(0)

    def __enter__(self) -> "XTestInjector":
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
