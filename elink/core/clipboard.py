"""Small Windows text clipboard bridge used by both session peers."""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import os
import time
import threading


MAX_CLIPBOARD_CHARS = 64_000
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

# The host and client may both poll in this process. Serialize complete native
# clipboard transactions, including reads which can trigger format conversion.
_clipboard_lock = threading.Lock()


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
    _kernel32.GlobalSize.argtypes = [W.HGLOBAL]
    _kernel32.GlobalSize.restype = C.c_size_t


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
    if os.name != 'nt':
        from ..platforms.linux.clipboard import read_text as read_linux
        return read_linux()
    with _clipboard_lock:
        return _read_text_locked()


def _read_text_locked():
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
            size = min(_kernel32.GlobalSize(handle), (MAX_CLIPBOARD_CHARS + 1) * 2)
            if size < 2:
                return None
            # Bound the read by the actual allocation, even for malformed text
            # without a terminating NUL. Decode only complete UTF-16 units.
            return C.string_at(pointer, size // 2 * 2).decode('utf-16-le', errors='replace').split('\x00', 1)[0][:MAX_CLIPBOARD_CHARS]
        finally:
            _kernel32.GlobalUnlock(handle)
    except (OSError, ValueError):
        return None
    finally:
        _user32.CloseClipboard()


def write_text(value: str) -> bool:
    """Replace the clipboard with Unicode text; return whether it succeeded."""
    if os.name != 'nt':
        from ..platforms.linux.clipboard import write_text as write_linux
        return write_linux(value)
    with _clipboard_lock:
        return _write_text_locked(value)


def _write_text_locked(value):
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
