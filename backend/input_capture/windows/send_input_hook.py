"""Windows 输入事件捕获（低级钩子）。

用 ``WH_KEYBOARD_LL`` / ``WH_MOUSE_LL`` 全局低级钩子采集键鼠事件：

- 钩子回调必须**极快返回**，否则系统会注入延迟甚至移除钩子 —— 所以回调里
  只做「翻译成 InputEvent 并塞进队列」，不做任何 I/O；
- 钩子必须跑在有消息循环的线程上，因此用独立线程 + ``GetMessage`` 循环，
  退出时用 ``PostThreadMessage(WM_QUIT)`` 唤醒；
- 低级钩子看得到系统所有输入，包括发给自己进程的——远端回放时要避免
  「自己的注入被自己捕获」形成回环，用 ``injected`` 标记过滤。
"""

from __future__ import annotations

import ctypes
import queue
import threading
import time
from ctypes import wintypes

from backend.control_channel import protocol
from backend.errors.base import ElinkError, ErrorCode
from backend.logging.logger import get_logger

logger = get_logger(__name__)

WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0203
WM_RBUTTONUP = 0x0204
WM_MBUTTONDOWN = 0x0205
WM_MBUTTONUP = 0x0206
WM_MOUSEWHEEL = 0x020A
WM_QUIT = 0x0012
LLKHF_INJECTED = 0x00000010
LLMHF_INJECTED = 0x00000001

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

user32.SetWindowsHookExW.restype = ctypes.c_void_p
user32.SetWindowsHookExW.argtypes = [
    ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD
]
user32.CallNextHookEx.restype = ctypes.c_ssize_t
user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]

# 64 位下句柄必须声明为指针宽度，否则会被截断成 32 位
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
kernel32.GetModuleHandleW.restype = ctypes.c_void_p
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]


class HookError(ElinkError):
    code = ErrorCode.IO


class SendInputHookCapture:
    """全局键鼠捕获：``read()`` 从队列取事件。"""

    backend = "lowlevel_hook"

    def __init__(self, *, capture_injected: bool = False, keyboard_only: bool = False,
                 active=lambda: True) -> None:
        self.capture_injected = capture_injected
        self.keyboard_only = keyboard_only
        self.active = active
        self._error = None
        self._alt_down = False
        self._queue: "queue.Queue[protocol.InputEvent]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._keyboard_hook: int | None = None
        self._mouse_hook: int | None = None
        self._running = threading.Event()
        self._keyboard_proc = HOOKPROC(self._keyboard_callback)
        self._mouse_proc = HOOKPROC(self._mouse_callback)
        self.modifiers = 0

    # ---- 生命周期 ----
    def open(self) -> None:
        if self._thread is not None:
            return
        self._running.clear()
        self._error = None
        self._thread = threading.Thread(target=self._run, name="input-hook", daemon=True)
        self._thread.start()
        if not self._running.wait(3):
            self.close()
            raise HookError("低级钩子安装超时")
        if self._error is not None:
            error = self._error
            self.close()
            raise error
        logger.info("输入捕获已启动（后端=%s）", self.backend)

    def close(self) -> None:
        if self._thread is None:
            return
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        self._thread.join(timeout=3)
        self._thread = None
        self._running.clear()

    # ---- 读取 ----
    def read(self, timeout_s: float | None = 1.0) -> protocol.InputEvent | None:
        try:
            return self._queue.get(timeout=timeout_s)
        except queue.Empty:
            return None

    def read_all(self) -> list[protocol.InputEvent]:
        events = []
        while True:
            try:
                events.append(self._queue.get_nowait())
            except queue.Empty:
                return events

    # ---- 钩子线程 ----
    def _run(self) -> None:
        self._thread_id = kernel32.GetCurrentThreadId()
        module = kernel32.GetModuleHandleW(None)

        self._keyboard_hook = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, self._keyboard_proc, module, 0
        )
        try:
            if not self.keyboard_only:
                self._mouse_hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._mouse_proc, module, 0)
            if not self._keyboard_hook or (not self.keyboard_only and not self._mouse_hook):
                raise HookError("钩子安装失败", detail={"win32": ctypes.get_last_error()})
            message = wintypes.MSG()
            user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)
            self._running.set()
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        except Exception as exc:
            self._error = exc
        finally:
            if self._keyboard_hook:
                user32.UnhookWindowsHookEx(self._keyboard_hook)
            if self._mouse_hook:
                user32.UnhookWindowsHookEx(self._mouse_hook)
            self._keyboard_hook = self._mouse_hook = None
            self._running.set()

    def _keyboard_callback(self, code, wparam, lparam):  # pragma: no cover - 系统回调
        if code >= 0 and self.active():
            data = ctypes.cast(lparam, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
            injected = bool(data.flags & LLKHF_INJECTED)
            if self.capture_injected or not injected:
                pressed = wparam in (WM_KEYDOWN, WM_SYSKEYDOWN)
                vk = int(data.vkCode)
                if self.keyboard_only:
                    if vk in (0x12, 0xA4, 0xA5):
                        self._alt_down = pressed
                    if vk not in (0x5B, 0x5C) and not (vk == 0x09 and self._alt_down):
                        return user32.CallNextHookEx(None, code, wparam, lparam)
                self._enqueue(
                    protocol.InputEvent(
                        event_type=protocol.EventType.KEY,
                        timestamp_ms=int(time.time() * 1000),
                        key_code=int(data.vkCode),
                        scan_code=int(data.scanCode),
                        pressed=pressed,
                        modifiers=self.modifiers,
                        extra={"injected": injected, "extended": bool(data.flags & 1)},
                    )
                )
                if self.keyboard_only:
                    return 1
        return user32.CallNextHookEx(None, code, wparam, lparam)

    def _mouse_callback(self, code, wparam, lparam):  # pragma: no cover - 系统回调
        if code >= 0:
            data = ctypes.cast(lparam, ctypes.POINTER(_MSLLHOOKSTRUCT)).contents
            injected = bool(data.flags & LLMHF_INJECTED)
            if self.capture_injected or not injected:
                event = self._translate_mouse(wparam, data, injected)
                if event is not None:
                    self._enqueue(event)
        return user32.CallNextHookEx(None, code, wparam, lparam)

    def _translate_mouse(self, message, data, injected: bool) -> protocol.InputEvent | None:
        now = int(time.time() * 1000)
        x, y = data.pt.x, data.pt.y
        extra = {"injected": injected}

        if message == WM_MOUSEMOVE:
            return protocol.InputEvent(
                event_type=protocol.EventType.MOUSE_MOVE,
                timestamp_ms=now,
                x=x,
                y=y,
                extra=extra,
            )
        if message == WM_MOUSEWHEEL:
            delta = ctypes.c_short((data.mouseData >> 16) & 0xFFFF).value
            return protocol.InputEvent(
                event_type=protocol.EventType.MOUSE_WHEEL,
                timestamp_ms=now,
                x=x,
                y=y,
                delta=int(delta),
                extra=extra,
            )

        buttons = {
            WM_LBUTTONDOWN: (1, True),
            WM_LBUTTONUP: (1, False),
            WM_RBUTTONDOWN: (2, True),
            WM_RBUTTONUP: (2, False),
            WM_MBUTTONDOWN: (3, True),
            WM_MBUTTONUP: (3, False),
        }
        if message not in buttons:
            return None
        button, pressed = buttons[message]
        return protocol.InputEvent(
            event_type=protocol.EventType.MOUSE_BUTTON,
            timestamp_ms=now,
            x=x,
            y=y,
            button=button,
            pressed=pressed,
            extra=extra,
        )

    def _enqueue(self, event: protocol.InputEvent) -> None:
        # 控制事件不丢：队列无界，回调内只做入队
        self._queue.put(event)

    def __enter__(self) -> "SendInputHookCapture":
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
