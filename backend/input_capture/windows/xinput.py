"""XInput 手柄采集（Windows）。

轮询最多 4 个物理手柄，状态变化时回调 ``(index, values)``；values 与
``input_injection/windows/vigem.py`` 的 ``_Pad`` 字段顺序一致：

    buttons, left_trigger, right_trigger, left_x, left_y, right_x, right_y

只做本机读取，不写注册表、不安装驱动。无手柄时线程静默等待。
"""

from __future__ import annotations

import ctypes as C
import os
import threading
from ctypes import wintypes as W


class _Pad(C.Structure):
    _fields_ = [
        ("buttons", C.c_ushort),
        ("lt", C.c_ubyte),
        ("rt", C.c_ubyte),
        ("lx", C.c_short),
        ("ly", C.c_short),
        ("rx", C.c_short),
        ("ry", C.c_short),
    ]


class _PadState(C.Structure):
    _fields_ = [("packet", W.DWORD), ("pad", _Pad)]


class _Vibration(C.Structure):
    _fields_ = [("left", C.c_ushort), ("right", C.c_ushort)]


def available() -> bool:
    return os.name == "nt"


class XInputPoller:
    """独立线程轮询 XInput；仅在状态变化时回调，默认 60Hz。"""

    def __init__(self, on_state, indices=range(4), interval_s: float = 1 / 60) -> None:
        self._on_state = on_state
        self._indices = tuple(indices)
        self._interval = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last: dict[int, tuple[int, ...]] = {}
        self._dll = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        if self.running or os.name != "nt":
            return False
        try:
            self._dll = C.WinDLL("xinput1_4")
            self._dll.XInputGetState.argtypes = [W.DWORD, C.POINTER(_PadState)]
            self._dll.XInputGetState.restype = W.DWORD
            self._dll.XInputSetState.argtypes = [W.DWORD, C.POINTER(_Vibration)]
            self._dll.XInputSetState.restype = W.DWORD
        except OSError:
            try:
                self._dll = C.WinDLL("xinput9_1_0")
                self._dll.XInputGetState.argtypes = [W.DWORD, C.POINTER(_PadState)]
                self._dll.XInputGetState.restype = W.DWORD
                self._dll.XInputSetState.argtypes = [W.DWORD, C.POINTER(_Vibration)]
                self._dll.XInputSetState.restype = W.DWORD
            except OSError:
                return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="xinput-poller", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        self._thread = None
        self._last.clear()

    def set_rumble(self, index: int, left: int, right: int) -> None:
        if self._dll is None:
            return
        try:
            left_value = max(0, min(65535, int(left)))
            right_value = max(0, min(65535, int(right)))
            self._dll.XInputSetState(int(index), C.byref(_Vibration(left_value, right_value)))
        except Exception:
            pass

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            for index in self._indices:
                state = _PadState()
                try:
                    if self._dll.XInputGetState(index, C.byref(state)) != 0:
                        continue
                    values = tuple(getattr(state.pad, name) for name, _ in _Pad._fields_)
                except Exception:
                    continue
                if self._last.get(index) == values:
                    continue
                self._last[index] = values
                try:
                    self._on_state(index, list(values))
                except Exception:
                    continue
