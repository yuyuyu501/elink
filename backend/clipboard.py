"""剪贴板文本桥（Windows 原生实现）。

只同步 **Unicode 文本**，不碰图片、文件、HTML 等格式；进入控制通道前会截断到
64000 字符，避免一次把控制通道塞满。读取使用 ``GetClipboardSequenceNumber``
判断变更，比轮询内容便宜，也能避免自己写回时触发回声。

这个模块不依赖 Qt：后台线程可以直接调用，Linux 目前返回“不可用”，
由界面在非 Windows 上隐藏/禁用同步开关。
"""

from __future__ import annotations

import ctypes as C
import os
import threading
import time
from ctypes import wintypes as W

MAX_CLIPBOARD_CHARS = 64_000
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

_lock = threading.Lock()

if os.name == "nt":
    _user32 = C.WinDLL("user32", use_last_error=True)
    _kernel32 = C.WinDLL("kernel32", use_last_error=True)
    _user32.OpenClipboard.argtypes = [W.HWND]
    _user32.OpenClipboard.restype = W.BOOL
    _user32.CloseClipboard.argtypes = []
    _user32.CloseClipboard.restype = W.BOOL
    _user32.EmptyClipboard.argtypes = []
    _user32.EmptyClipboard.restype = W.BOOL
    _user32.GetClipboardData.argtypes = [W.UINT]
    _user32.GetClipboardData.restype = W.HANDLE
    _user32.SetClipboardData.argtypes = [W.UINT, W.HANDLE]
    _user32.SetClipboardData.restype = W.HANDLE
    _user32.GetClipboardSequenceNumber.argtypes = []
    _user32.GetClipboardSequenceNumber.restype = W.DWORD
    _kernel32.GlobalLock.argtypes = [W.HGLOBAL]
    _kernel32.GlobalLock.restype = C.c_void_p
    _kernel32.GlobalUnlock.argtypes = [W.HGLOBAL]
    _kernel32.GlobalUnlock.restype = W.BOOL
    _kernel32.GlobalAlloc.argtypes = [W.UINT, C.c_size_t]
    _kernel32.GlobalAlloc.restype = W.HGLOBAL
    _kernel32.GlobalFree.argtypes = [W.HGLOBAL]
    _kernel32.GlobalFree.restype = W.HGLOBAL
    _kernel32.GlobalSize.argtypes = [W.HGLOBAL]
    _kernel32.GlobalSize.restype = C.c_size_t


def available() -> bool:
    return os.name == "nt"


def sequence() -> int:
    if os.name != "nt":
        return 0
    try:
        return int(_user32.GetClipboardSequenceNumber())
    except OSError:
        return 0


def _open() -> bool:
    if os.name != "nt":
        return False
    for _ in range(3):
        if _user32.OpenClipboard(None):
            return True
        time.sleep(0.01)
    return False


def read_text() -> str:
    """读取 Unicode 文本；剪贴板被占用时返回空串而不是抛异常。"""
    if os.name != "nt":
        return ""
    with _lock:
        if not _open():
            return ""
        try:
            handle = _user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return ""
            pointer = _kernel32.GlobalLock(handle)
            if not pointer:
                return ""
            try:
                size = min(_kernel32.GlobalSize(handle), (MAX_CLIPBOARD_CHARS + 1) * 2)
                if size < 2:
                    return ""
                raw = C.string_at(pointer, size // 2 * 2)
                text = raw.decode("utf-16-le", errors="replace").split("\x00", 1)[0]
                return text[:MAX_CLIPBOARD_CHARS]
            finally:
                _kernel32.GlobalUnlock(handle)
        except (OSError, ValueError):
            return ""
        finally:
            _user32.CloseClipboard()


def write_text(value: str) -> bool:
    """把剪贴板替换为 Unicode 文本；失败返回 False。"""
    if os.name != "nt" or not isinstance(value, str) or not value:
        return False
    value = value[:MAX_CLIPBOARD_CHARS]
    with _lock:
        if not _open():
            return False
        handle = None
        try:
            payload = value.encode("utf-16-le") + b"\x00\x00"
            handle = _kernel32.GlobalAlloc(GMEM_MOVEABLE, len(payload))
            if not handle:
                return False
            pointer = _kernel32.GlobalLock(handle)
            if not pointer:
                return False
            try:
                C.memmove(pointer, payload, len(payload))
            finally:
                _kernel32.GlobalUnlock(handle)
            if not _user32.EmptyClipboard():
                return False
            if not _user32.SetClipboardData(CF_UNICODETEXT, handle):
                return False
            handle = None  # ownership transferred to Windows
            return True
        except (OSError, ValueError):
            return False
        finally:
            if handle:
                _kernel32.GlobalFree(handle)
            _user32.CloseClipboard()
