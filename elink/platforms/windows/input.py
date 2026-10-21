"""Validated session input and the small Windows hook used by captured sessions."""
from __future__ import annotations

import ctypes as c
import math
import os
import time
from ctypes import wintypes as w

from ...paths import resource_dir
from ...core.input_session import InputSession, RecordingInput, PAD_BACKENDS, integer


class Mouse(c.Structure):
    _fields_ = [("dx", w.LONG), ("dy", w.LONG), ("data", w.DWORD), ("flags", w.DWORD),
                ("time", w.DWORD), ("extra", c.c_size_t)]


class Keyboard(c.Structure):
    _fields_ = [("vk", w.WORD), ("scan", w.WORD), ("flags", w.DWORD),
                ("time", w.DWORD), ("extra", c.c_size_t)]


class Union(c.Union):
    _fields_ = [("mouse", Mouse), ("keyboard", Keyboard)]


class Input(c.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", w.DWORD), ("u", Union)]


class _KeyboardHookData(c.Structure):
    _fields_ = [("vkCode", w.DWORD), ("scanCode", w.DWORD), ("flags", w.DWORD),
                ("time", w.DWORD), ("extra", c.c_void_p)]


class _RawInputDevice(c.Structure):
    _fields_ = [("usUsagePage", w.USHORT), ("usUsage", w.USHORT),
                ("dwFlags", w.DWORD), ("hwndTarget", w.HWND)]


class _RawInputHeader(c.Structure):
    _fields_ = [("dwType", w.DWORD), ("dwSize", w.DWORD), ("hDevice", w.HANDLE),
                ("wParam", w.WPARAM)]


class _RawMouse(c.Structure):
    _fields_ = [("usFlags", w.USHORT), ("usButtonFlags", w.USHORT),
                ("usButtonData", w.USHORT), ("usReserved", w.USHORT),
                ("ulRawButtons", w.ULONG), ("lLastX", w.LONG), ("lLastY", w.LONG),
                ("ulExtraInformation", w.ULONG)]


class _RawInput(c.Structure):
    _anonymous_ = ("data",)
    _fields_ = [("header", _RawInputHeader), ("data", _RawMouse)]


class _WindowsMessage(c.Structure):
    _fields_ = [("hwnd", w.HWND), ("message", w.UINT), ("wParam", w.WPARAM), ("lParam", w.LPARAM),
                ("time", w.DWORD), ("pt_x", w.LONG), ("pt_y", w.LONG)]


class CursorConfinement:
    """Constrain the native pointer, using native client pixels for mixed DPI."""

    def __init__(self):
        self._bounds = None
        self._requested = None
        self._user32 = None
        if os.name == 'nt':
            self._user32 = c.WinDLL('user32', use_last_error=True)
            for name, args in (
                ('GetClientRect', [w.HWND, c.POINTER(w.RECT)]),
                ('ClientToScreen', [w.HWND, c.POINTER(w.POINT)]),
                ('ClipCursor', [c.POINTER(w.RECT)]),
                ('GetClipCursor', [c.POINTER(w.RECT)]),
            ):
                function = getattr(self._user32, name)
                function.argtypes, function.restype = args, w.BOOL

    def confine(self, hwnd, bounds, size):
        if self._user32 is None:
            return
        client, origin = w.RECT(), w.POINT()
        if not self._user32.GetClientRect(hwnd, c.byref(client)) or not self._user32.ClientToScreen(hwnd, c.byref(origin)):
            raise c.WinError(c.get_last_error())
        x, y, width, height = bounds
        sx, sy = client.right / max(1, size[0]), client.bottom / max(1, size[1])
        rect = (origin.x + math.floor(x * sx), origin.y + math.floor(y * sy),
                origin.x + math.ceil((x + width) * sx), origin.y + math.ceil((y + height) * sy))
        if width <= 0 or height <= 0 or rect[2] <= rect[0] or rect[3] <= rect[1]:
            self.release()
            return
        if rect != self._requested:
            native = w.RECT(*rect)
            if not self._user32.ClipCursor(c.byref(native)):
                raise c.WinError(c.get_last_error())
            # Windows intersects offscreen rectangles with the virtual desktop.
            # Ownership must use the effective rectangle, or release can miss it.
            actual = w.RECT()
            if not self._user32.GetClipCursor(c.byref(actual)):
                error = c.get_last_error()
                self._user32.ClipCursor(None)
                raise c.WinError(error)
            self._requested = rect
            self._bounds = (actual.left, actual.top, actual.right, actual.bottom)

    def release(self):
        if self._bounds is not None:
            rect = w.RECT()
            # Do not undo a constraint installed by another foreground window.
            if self._user32.GetClipCursor(c.byref(rect)) and (rect.left, rect.top, rect.right, rect.bottom) == self._bounds:
                self._user32.ClipCursor(None)
            self._bounds = None
            self._requested = None


class RawMouseInput:
    """Read hardware mouse deltas without Windows pointer acceleration."""

    _WM_INPUT = 0x00FF
    _RID_INPUT = 0x10000003
    _RIM_TYPEMOUSE = 0
    _RIDEV_INPUTSINK = 0x00000100
    _MOUSE_MOVE_ABSOLUTE = 0x0001

    @classmethod
    def _relative_delta(cls, raw):
        """Return a relative mouse delta from a decoded RAWINPUT value.

        ``_RawInput`` exposes its payload as ``data`` (and anonymously exposes
        the mouse fields themselves).  Keeping this access in one small helper
        prevents the Windows hook from accidentally using the unrelated
        high-level ``Input`` union, which has a ``mouse`` member but RAWINPUT
        does not.
        """
        if raw.header.dwType != cls._RIM_TYPEMOUSE or raw.data.usFlags & cls._MOUSE_MOVE_ABSOLUTE:
            return None
        return int(raw.data.lLastX), int(raw.data.lLastY)

    def __init__(self):
        self._user32 = None
        self._hwnd = None
        self._active = False

    @property
    def active(self):
        return self._active

    def start(self, hwnd):
        if os.name != "nt" or not hwnd or self._active:
            return False
        user32 = c.WinDLL("user32", use_last_error=True)
        user32.RegisterRawInputDevices.argtypes = [c.POINTER(_RawInputDevice), w.UINT, w.UINT]
        user32.RegisterRawInputDevices.restype = w.BOOL
        user32.GetRawInputData.argtypes = [c.c_void_p, w.UINT, c.c_void_p, c.POINTER(w.UINT), w.UINT]
        user32.GetRawInputData.restype = w.UINT
        device = _RawInputDevice(0x01, 0x02, self._RIDEV_INPUTSINK, w.HWND(hwnd))
        if not user32.RegisterRawInputDevices(c.byref(device), 1, c.sizeof(device)):
            raise c.WinError(c.get_last_error())
        self._user32, self._hwnd, self._active = user32, hwnd, True
        return True

    def stop(self):
        if self._active and self._user32 is not None:
            device = _RawInputDevice(0x01, 0x02, 0x00000001, None)
            self._user32.RegisterRawInputDevices(c.byref(device), 1, c.sizeof(device))
        self._active = False
        self._hwnd = None

    def delta(self, message):
        if not self._active or message is None:
            return None
        msg = c.cast(int(message), c.POINTER(_WindowsMessage)).contents
        if msg.message != self._WM_INPUT:
            return None
        size = w.UINT(0)
        header_size = c.sizeof(_RawInputHeader)
        if self._user32.GetRawInputData(c.c_void_p(msg.lParam), self._RID_INPUT, None,
                                        c.byref(size), header_size) == 0xffffffff or size.value < c.sizeof(_RawInput):
            return None
        buffer = (c.c_ubyte * size.value)()
        result = self._user32.GetRawInputData(c.c_void_p(msg.lParam), self._RID_INPUT, buffer,
                                               c.byref(size), header_size)
        if result == 0xffffffff:
            return None
        raw = c.cast(buffer, c.POINTER(_RawInput)).contents
        return self._relative_delta(raw)

    def __del__(self):
        try:
            self.stop()
        except Exception:
            pass


class RemoteKeyboardHook:
    """Suppress Windows shell shortcuts while a Player owns keyboard input.

    The hook is deliberately limited to the Windows key and Alt+Tab. Normal
    keys continue through Qt so the existing focused-widget path remains the
    source of truth. It is a no-op on other platforms and never installs a
    driver or global input backend.
    """

    _WH_KEYBOARD_LL = 13
    _WM_KEYDOWN = 0x0100
    _WM_KEYUP = 0x0101
    _WM_SYSKEYDOWN = 0x0104
    _WM_SYSKEYUP = 0x0105
    _LLKHF_EXTENDED = 0x01
    _LLKHF_INJECTED = 0x10
    _VK_TAB = 0x09
    _VK_LMENU = 0xA4
    _VK_RMENU = 0xA5
    _VK_LWIN = 0x5B
    _VK_RWIN = 0x5C

    def __init__(self, send, active=lambda: True):
        self.send = send
        self.active = active
        self._user32 = None
        self._kernel32 = None
        self._hook = None
        self._proc = None
        self._alt_down = False

    @property
    def installed(self):
        return self._hook is not None

    def start(self):
        if os.name != "nt" or self._hook is not None:
            return False
        callback_type = c.WINFUNCTYPE(c.c_ssize_t, c.c_int, w.WPARAM, w.LPARAM)
        self._user32 = c.WinDLL("user32", use_last_error=True)
        self._kernel32 = c.WinDLL("kernel32", use_last_error=True)
        self._user32.SetWindowsHookExW.argtypes = [c.c_int, callback_type, c.c_void_p, w.DWORD]
        self._user32.SetWindowsHookExW.restype = c.c_void_p
        self._user32.CallNextHookEx.argtypes = [c.c_void_p, c.c_int, w.WPARAM, w.LPARAM]
        self._user32.CallNextHookEx.restype = c.c_ssize_t
        self._user32.UnhookWindowsHookEx.argtypes = [c.c_void_p]
        self._user32.UnhookWindowsHookEx.restype = w.BOOL
        self._kernel32.GetModuleHandleW.argtypes = [w.LPCWSTR]
        self._kernel32.GetModuleHandleW.restype = c.c_void_p
        self._proc = callback_type(self._callback)
        module = self._kernel32.GetModuleHandleW(None)
        self._hook = self._user32.SetWindowsHookExW(self._WH_KEYBOARD_LL, self._proc, module, 0)
        if not self._hook:
            self._proc = None
            raise c.WinError(c.get_last_error())
        return True

    def stop(self):
        if self._hook is not None and self._user32 is not None:
            self._user32.UnhookWindowsHookEx(self._hook)
        self._hook = None
        self._proc = None
        self._alt_down = False

    def _callback(self, code, message, lparam):
        if code < 0 or not self.active():
            return self._user32.CallNextHookEx(None, code, message, lparam)
        data = c.cast(lparam, c.POINTER(_KeyboardHookData)).contents
        if data.flags & self._LLKHF_INJECTED:
            return self._user32.CallNextHookEx(None, code, message, lparam)
        down = message in (self._WM_KEYDOWN, self._WM_SYSKEYDOWN)
        up = message in (self._WM_KEYUP, self._WM_SYSKEYUP)
        if not (down or up):
            return self._user32.CallNextHookEx(None, code, message, lparam)
        vk = int(data.vkCode)
        if vk in (self._VK_LMENU, self._VK_RMENU):
            self._alt_down = down
        intercept = vk in (self._VK_LWIN, self._VK_RWIN) or (vk == self._VK_TAB and self._alt_down)
        if intercept:
            event = {"type": "key", "vk": vk, "scan": int(data.scanCode) & 255,
                     "extended": bool(data.flags & self._LLKHF_EXTENDED), "down": down}
            try:
                self.send(event)
            except Exception:
                pass
            return 1
        return self._user32.CallNextHookEx(None, code, message, lparam)

    def __del__(self):
        try:
            self.stop()
        except Exception:
            pass


class Pad(c.Structure):
    _fields_ = [("buttons", c.c_ushort), ("lt", c.c_ubyte), ("rt", c.c_ubyte),
                ("lx", c.c_short), ("ly", c.c_short), ("rx", c.c_short), ("ry", c.c_short)]


class PadState(c.Structure):
    _fields_ = [("packet", w.DWORD), ("pad", Pad)]


class Vibration(c.Structure):
    _fields_ = [("left", c.c_ushort), ("right", c.c_ushort)]


class XInput:
    def __init__(self):
        self.dll = c.WinDLL("xinput1_4")
        self.dll.XInputGetState.argtypes = [w.DWORD, c.POINTER(PadState)]
        self.dll.XInputSetState.argtypes = [w.DWORD, c.POINTER(Vibration)]

    def read(self, index):
        state = PadState()
        if self.dll.XInputGetState(index, c.byref(state)):
            return None
        return [getattr(state.pad, name) for name, _ in Pad._fields_]

    def rumble(self, index, left=0, right=0):
        self.dll.XInputSetState(index, c.byref(Vibration(left, right)))


class VirtualPads:
    def __init__(self, feedback):
        self.dll = c.CDLL(str(resource_dir() / "ViGEmClient.dll"))
        signatures = {
            "vigem_alloc": ([], c.c_void_p), "vigem_free": ([c.c_void_p], None),
            "vigem_connect": ([c.c_void_p], c.c_uint), "vigem_disconnect": ([c.c_void_p], None),
            "vigem_target_x360_alloc": ([], c.c_void_p), "vigem_target_free": ([c.c_void_p], None),
            "vigem_target_add": ([c.c_void_p, c.c_void_p], c.c_uint),
            "vigem_target_remove": ([c.c_void_p, c.c_void_p], c.c_uint),
            "vigem_target_x360_update": ([c.c_void_p, c.c_void_p, Pad], c.c_uint),
            "vigem_target_x360_get_user_index": ([c.c_void_p, c.c_void_p, c.POINTER(c.c_ulong)], c.c_uint),
            "vigem_target_x360_register_notification": ([c.c_void_p, c.c_void_p, c.c_void_p, c.c_void_p], c.c_uint),
            "vigem_target_x360_unregister_notification": ([c.c_void_p], None),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.dll, name)
            function.argtypes, function.restype = args, result
        self.client = self.dll.vigem_alloc()
        self.targets = {}
        self.feedback = feedback
        if not self.client or self.dll.vigem_connect(self.client) != 0x20000000:
            if self.client:
                self.dll.vigem_free(self.client)
            raise RuntimeError("手柄注入需要主机已安装 ViGEmBus；键鼠和串流仍可使用。")

    def update(self, index, values):
        if index not in self.targets:
            target = self.dll.vigem_target_x360_alloc()
            if not target or self.dll.vigem_target_add(self.client, target) != 0x20000000:
                if target:
                    self.dll.vigem_target_free(target)
                raise RuntimeError("无法创建虚拟 Xbox 360 手柄。")
            callback_type = c.CFUNCTYPE(None, c.c_void_p, c.c_void_p, c.c_ubyte, c.c_ubyte, c.c_ubyte, c.c_void_p)
            callback = callback_type(lambda client, pad, large, small, led, user:
                                     self.feedback({"type": "rumble", "index": index, "left": large * 257, "right": small * 257}))
            self.targets[index] = (target, callback)
            self.dll.vigem_target_x360_register_notification(self.client, target, callback, None)
        if self.dll.vigem_target_x360_update(self.client, self.targets[index][0], Pad(*values)) != 0x20000000:
            raise RuntimeError("虚拟手柄状态更新失败。")

    def close(self):
        for target, callback in self.targets.values():
            self.dll.vigem_target_x360_update(self.client, target, Pad())
            self.dll.vigem_target_x360_unregister_notification(target)
            self.dll.vigem_target_remove(self.client, target)
            self.dll.vigem_target_free(target)
        self.targets.clear()
        self.dll.vigem_disconnect(self.client)
        self.dll.vigem_free(self.client)

    def user_index(self, index):
        result = c.c_ulong()
        if self.dll.vigem_target_x360_get_user_index(self.client, self.targets[index][0], c.byref(result)) != 0x20000000:
            raise RuntimeError("无法读取虚拟手柄 XInput 索引。")
        return result.value




def create_pads(backend, feedback, notify):
    if backend == "vigem":
        return VirtualPads(feedback)
    if backend == "elinkpad":
        from .elinkpad import ElinkPads
        return ElinkPads(feedback, notify)
    raise RuntimeError("本机已禁用虚拟手柄。" if backend == "disabled" else "未知手柄后端。")


class WindowsInput:
    def __init__(self, feedback, pad_backend="vigem", notify=lambda message: None):
        if pad_backend not in PAD_BACKENDS:
            raise ValueError("未知手柄后端。")
        self.user = c.WinDLL("user32", use_last_error=True)
        self.user.SendInput.argtypes = [w.UINT, c.POINTER(Input), c.c_int]
        self.user.SendInput.restype = w.UINT
        self.pads = None
        self.pad_error = ""
        self.feedback = feedback
        self.pad_backend, self.notify = pad_backend, notify

    def emit(self, event):
        kind = event["type"]
        if kind == "pad":
            if self.pad_error:
                raise RuntimeError(self.pad_error)
            if self.pads is None:
                try:
                    self.pads = create_pads(self.pad_backend, self.feedback, self.notify)
                except Exception as exc:
                    self.pad_error = str(exc)
                    raise
            self.pads.update(event["index"], event["values"])
            return
        value = Input()
        if kind == "key":
            value.type = 1
            value.keyboard = Keyboard(event["vk"] if not event["scan"] else 0, event["scan"],
                                      (8 if event["scan"] else 0) | (1 if event["extended"] else 0) | (0 if event["down"] else 2), 0, 0)
        elif kind == "move":
            value.mouse = Mouse(event["x"], event["y"], 0, 1 | (0x8000 if event["absolute"] else 0), 0, 0)
        elif kind == "button":
            flags = {"left": (2, 4), "right": (8, 16), "middle": (32, 64)}
            value.mouse.flags = flags[event["button"]][0 if event["down"] else 1]
        elif kind == "wheel":
            value.mouse.flags, value.mouse.data = 0x800, event["delta"] & 0xffffffff
        if self.user.SendInput(1, c.byref(value), c.sizeof(value)) != 1:
            raise RuntimeError("Windows 拒绝输入注入（请检查目标程序权限或安全桌面）。")

    def close(self):
        if self.pads:
            self.pads.close()
            self.pads = None


