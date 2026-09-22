"""Small Windows text clipboard bridge used by both session peers."""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import os
import time


MAX_CLIPBOARD_CHARS = 64_000
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


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
    _kernel32.GlobalLock.argtypes = [W.HGLOBAL]
    _kernel32.GlobalLock.restype = C.c_void_p
    _kernel32.GlobalUnlock.argtypes = [W.HGLOBAL]
    _kernel32.GlobalUnlock.restype = W.BOOL
    _kernel32.GlobalAlloc.argtypes = [W.UINT, C.c_size_t]
    _kernel32.GlobalAlloc.restype = W.HGLOBAL
    _kernel32.GlobalFree.argtypes = [W.HGLOBAL]
    _kernel32.GlobalFree.restype = W.HGLOBAL


def _open_clipboard():
    if os.name != "nt":
        return False
    for _ in range(3):
        if _user32.OpenClipboard(None):
            return True
        time.sleep(0.01)
    return False


def read_text() -> str | None:
    """Read Unicode text, or return None when the clipboard is unavailable."""
    if not _open_clipboard():
        return None
    try:
        handle = _user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        pointer = _kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return C.wstring_at(pointer)[:MAX_CLIPBOARD_CHARS]
        finally:
            _kernel32.GlobalUnlock(handle)
    except (OSError, ValueError):
        return None
    finally:
        _user32.CloseClipboard()


def write_text(value: str) -> bool:
    """Replace the clipboard with Unicode text; return whether it succeeded."""
    if os.name != "nt" or not isinstance(value, str):
        return False
    value = value[:MAX_CLIPBOARD_CHARS]
    if not _open_clipboard():
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
        handle = None  # Windows owns the memory after SetClipboardData.
        return True
    except (OSError, ValueError):
        return False
    finally:
        if handle:
            _kernel32.GlobalFree(handle)
        _user32.CloseClipboard()
