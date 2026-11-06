"""Windows 输入注入（``SendInput``）。

要点：

- 鼠标绝对定位要求把屏幕像素换算到 0~65535 的归一化坐标（``MOUSEEVENTF_ABSOLUTE``），
  并且默认是「虚拟桌面」坐标系，多屏时要用 ``MOUSEEVENTF_VIRTUALDESK``；
- 注入的事件会被低级钩子看到，所以必须带 ``LLKHF_INJECTED`` 标记，
  采集侧据此过滤，避免自己捕获自己的回放形成回环；
- 键盘注入用扫描码（``KEYEVENTF_SCANCODE``）更可靠：某些游戏只认扫描码，
  而远程控制场景经常包含游戏窗口。
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

from backend.control_channel import protocol
from backend.errors.base import ElinkError, ErrorCode
from backend.logging.logger import get_logger

logger = get_logger(__name__)

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008
VK_PACKET = 0xE7

user32 = ctypes.WinDLL("user32", use_last_error=True)


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", _INPUTUNION)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79


class InjectionError(ElinkError):
    code = ErrorCode.IO


class SendInputInjector:
    """把 ``InputEvent`` 注入系统。"""

    backend = "sendinput"

    def __init__(self, *, use_scan_codes: bool = True) -> None:
        self.use_scan_codes = use_scan_codes
        self.injected = 0

    # ---- 对外接口 ----
    def inject(self, event: protocol.InputEvent) -> bool:
        handler = {
            protocol.EventType.MOUSE_MOVE: self._mouse_move,
            protocol.EventType.MOUSE_BUTTON: self._mouse_button,
            protocol.EventType.MOUSE_WHEEL: self._mouse_wheel,
            protocol.EventType.KEY: self._key,
            protocol.EventType.TEXT: self._text,
            protocol.EventType.CONTROL: self._control,
        }.get(event.event_type)

        if handler is None:
            logger.debug("忽略不支持的注入类型: %s", event.event_type)
            return False
        return bool(handler(event))

    def close(self) -> None:
        pass

    # ---- 鼠标 ----
    def _mouse_move(self, event: protocol.InputEvent) -> bool:
        return self._absolute_move(event.x, event.y)

    def _mouse_button(self, event: protocol.InputEvent) -> bool:
        flags = {
            (1, True): MOUSEEVENTF_LEFTDOWN,
            (1, False): MOUSEEVENTF_LEFTUP,
            (2, True): MOUSEEVENTF_RIGHTDOWN,
            (2, False): MOUSEEVENTF_RIGHTUP,
            (3, True): MOUSEEVENTF_MIDDLEDOWN,
            (3, False): MOUSEEVENTF_MIDDLEUP,
        }.get((event.button, event.pressed))
        if flags is None:
            return False

        # 先移动到目标点，避免「在旧位置点击」——这是远程控制里最常见的错位
        if not self._absolute_move(event.x, event.y):
            return False
        return self._send_mouse(flags=flags, data=0)

    def _mouse_wheel(self, event: protocol.InputEvent) -> bool:
        return self._send_mouse(flags=MOUSEEVENTF_WHEEL, data=event.delta & 0xFFFFFFFF)

    def _absolute_move(self, x: int, y: int) -> bool:
        screen_x = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        screen_y = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        screen_w = max(1, user32.GetSystemMetrics(SM_CXVIRTUALSCREEN))
        screen_h = max(1, user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))

        normalized_x = int((x - screen_x) * 65535 / max(1, screen_w - 1))
        normalized_y = int((y - screen_y) * 65535 / max(1, screen_h - 1))
        dx = max(0, min(65535, normalized_x))
        dy = max(0, min(65535, normalized_y))

        return self._send_mouse(
            flags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
            data=0,
            dx=dx,
            dy=dy,
        )

    def _send_mouse(self, *, flags: int, data: int, dx: int = 0, dy: int = 0) -> bool:
        item = _INPUT()
        item.type = INPUT_MOUSE
        item.union.mi = _MOUSEINPUT(dx=dx, dy=dy, mouseData=data, dwFlags=flags, time=0, dwExtraInfo=0)
        return self._send(item)

    # ---- 键盘 ----
    def _key(self, event: protocol.InputEvent) -> bool:
        flags = 0
        scan = 0
        vk = 0

        if self.use_scan_codes and event.scan_code:
            scan = event.scan_code & 0xFF
            flags |= KEYEVENTF_SCANCODE
            if event.extra.get("extended"):
                flags |= KEYEVENTF_EXTENDEDKEY
        else:
            vk = event.key_code & 0xFF

        if not event.pressed:
            flags |= KEYEVENTF_KEYUP

        item = _INPUT()
        item.type = INPUT_KEYBOARD
        item.union.ki = _KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
        return self._send(item)

    def _text(self, event: protocol.InputEvent) -> bool:
        """按字符逐个注入：非 ASCII 用 ``SendInput`` 的 VK_PACKET 通道。"""
        success = True
        for char in event.text:
            # KEYEVENTF_UNICODE：wScan 直接放 UTF-16 码元，绕开键盘布局，
            # 这样中文/表情也能输入（VK 通道只能表达按键，不能表达字符）
            for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
                item = _INPUT()
                item.type = INPUT_KEYBOARD
                item.union.ki = _KEYBDINPUT(
                    wVk=0, wScan=ord(char), dwFlags=flags, time=0, dwExtraInfo=0
                )
                success = self._send(item) and success
            time.sleep(0.001)
        return success

    def _control(self, event: protocol.InputEvent) -> bool:
        action = event.extra.get("action", "")
        if action == "screenshot_pause":
            time.sleep(0.1)
            return True
        logger.debug("忽略未知控制注入: %s", action)
        return False

    def _send(self, item: _INPUT, count: int = 1) -> bool:
        sent = user32.SendInput(count, ctypes.byref(item), ctypes.sizeof(_INPUT))
        if sent != count:
            logger.warning("SendInput 部分失败: %s/%s err=%s", sent, count, ctypes.get_last_error())
            return False
        self.injected += count
        return True
