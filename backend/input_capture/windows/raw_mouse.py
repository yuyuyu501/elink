"""Windows Raw Input mouse deltas and DPI-aware cursor confinement."""

from __future__ import annotations

import ctypes as c
import math
import os
from ctypes import wintypes as w


class _RawInputDevice(c.Structure):
    _fields_ = [("page", w.USHORT), ("usage", w.USHORT), ("flags", w.DWORD), ("target", w.HWND)]


class _RawInputHeader(c.Structure):
    _fields_ = [("kind", w.DWORD), ("size", w.DWORD), ("device", w.HANDLE), ("wparam", w.WPARAM)]


class _RawMouse(c.Structure):
    _fields_ = [("flags", w.USHORT), ("buttons", w.USHORT), ("data", w.USHORT),
                ("reserved", w.USHORT), ("raw_buttons", w.ULONG),
                ("x", w.LONG), ("y", w.LONG), ("extra", w.ULONG)]


class _RawInput(c.Structure):
    _fields_ = [("header", _RawInputHeader), ("mouse", _RawMouse)]


class CursorConfinement:
    def __init__(self) -> None:
        self._bounds = None
        self._user = None
        if os.name == "nt":
            self._user = c.WinDLL("user32", use_last_error=True)
            for name, args in (
                ("GetClientRect", [w.HWND, c.POINTER(w.RECT)]),
                ("ClientToScreen", [w.HWND, c.POINTER(w.POINT)]),
                ("ClipCursor", [c.POINTER(w.RECT)]),
                ("GetClipCursor", [c.POINTER(w.RECT)]),
            ):
                func = getattr(self._user, name)
                func.argtypes, func.restype = args, w.BOOL

    def confine(self, hwnd: int, bounds: tuple, size: tuple) -> None:
        if self._user is None:
            return
        client, origin = w.RECT(), w.POINT()
        if not self._user.GetClientRect(hwnd, c.byref(client)) or not self._user.ClientToScreen(hwnd, c.byref(origin)):
            raise c.WinError(c.get_last_error())
        x, y, width, height = bounds
        if width <= 0 or height <= 0:
            self.release()
            return
        sx, sy = client.right / max(1, size[0]), client.bottom / max(1, size[1])
        rect = w.RECT(origin.x + math.floor(x * sx), origin.y + math.floor(y * sy),
                      origin.x + math.ceil((x + width) * sx), origin.y + math.ceil((y + height) * sy))
        if not self._user.ClipCursor(c.byref(rect)):
            raise c.WinError(c.get_last_error())
        actual = w.RECT()
        if not self._user.GetClipCursor(c.byref(actual)):
            self._user.ClipCursor(None)
            raise c.WinError(c.get_last_error())
        self._bounds = (actual.left, actual.top, actual.right, actual.bottom)

    def release(self) -> None:
        if self._bounds is not None:
            rect = w.RECT()
            if self._user.GetClipCursor(c.byref(rect)) and (rect.left, rect.top, rect.right, rect.bottom) == self._bounds:
                self._user.ClipCursor(None)
            self._bounds = None


class RawMouseInput:
    def __init__(self) -> None:
        self.active = False
        self._user = None

    def start(self, hwnd: int) -> bool:
        if os.name != "nt" or not hwnd:
            return False
        if self.active:
            return True
        self._user = c.WinDLL("user32", use_last_error=True)
        self._user.RegisterRawInputDevices.argtypes = [c.POINTER(_RawInputDevice), w.UINT, w.UINT]
        self._user.RegisterRawInputDevices.restype = w.BOOL
        self._user.GetRawInputData.argtypes = [w.HANDLE, w.UINT, c.c_void_p, c.POINTER(w.UINT), w.UINT]
        self._user.GetRawInputData.restype = w.UINT
        device = _RawInputDevice(1, 2, 0x100, hwnd)
        if not self._user.RegisterRawInputDevices(c.byref(device), 1, c.sizeof(device)):
            raise c.WinError(c.get_last_error())
        self.active = True
        return True

    def stop(self) -> None:
        if self.active:
            device = _RawInputDevice(1, 2, 1, None)
            self._user.RegisterRawInputDevices(c.byref(device), 1, c.sizeof(device))
        self.active = False

    @staticmethod
    def _relative_delta(raw) -> tuple[int, int] | None:
        if raw.header.kind != 0 or raw.mouse.flags & 1:
            return None
        return int(raw.mouse.x), int(raw.mouse.y)

    def delta(self, message) -> tuple[int, int] | None:
        if not self.active or message is None:
            return None
        msg = c.cast(int(message), c.POINTER(w.MSG)).contents
        if msg.message != 0xFF:
            return None
        size = w.UINT()
        header_size = c.sizeof(_RawInputHeader)
        if self._user.GetRawInputData(w.HANDLE(msg.lParam), 0x10000003, None,
                                     c.byref(size), header_size) == 0xFFFFFFFF:
            return None
        if not c.sizeof(_RawInput) <= size.value <= 65536:
            return None
        buffer = (c.c_ubyte * size.value)()
        result = self._user.GetRawInputData(w.HANDLE(msg.lParam), 0x10000003, buffer,
                                            c.byref(size), header_size)
        if result == 0xFFFFFFFF or result < c.sizeof(_RawInput):
            return None
        return self._relative_delta(c.cast(buffer, c.POINTER(_RawInput)).contents)
