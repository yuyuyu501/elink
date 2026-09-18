"""Validated session input. No global hooks, background key capture or driver installer."""
from __future__ import annotations

import ctypes as c
import json
import time
from ctypes import wintypes as w

from ..paths import resource_dir


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


PAD_BACKENDS = ("vigem", "elinkpad", "disabled")


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


class RecordingInput:
    """Safe test sink: records events without manipulating the local desktop."""
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(dict(event))

    def close(self):
        pass


def integer(value, low, high):
    return type(value) is int and low <= value <= high


class InputSession:
    def __init__(self, backend):
        self.backend = backend
        self.keys, self.buttons, self.pads = {}, set(), set()
        self.sequences = {}
        self.heartbeat = time.monotonic()
        self.active = False
        self.epoch = -1

    def receive(self, raw, reliable=True):
        if not isinstance(raw, str) or len(raw) > 2048:
            return False
        try:
            event = json.loads(raw)
        except ValueError:
            return False
        if not isinstance(event, dict):
            return False
        kind = event.get("type")
        if kind == "heartbeat" and reliable and type(event.get("active")) is bool and integer(event.get("epoch"), 0, 2**53):
            self.heartbeat = time.monotonic()
            if event["epoch"] != self.epoch or not event["active"]:
                self.release()
                self.sequences.clear()
            self.epoch, self.active = event["epoch"], event["active"]
            return True
        if kind == "release" and reliable:
            self.release()
            self.active = False
            return True
        if not self.active or event.get("epoch") != self.epoch:
            return False
        if kind in ("move", "pad"):
            sequence = event.get("seq")
            category = f"{kind}:{event.get('index', 0)}"
            if not integer(sequence, 0, 2**53) or sequence <= self.sequences.get(category, -1):
                return False
        elif not reliable:
            return False
        if kind == "key":
            if not (integer(event.get("vk"), 1, 255) and integer(event.get("scan"), 0, 255)
                    and type(event.get("extended")) is bool and type(event.get("down")) is bool):
                return False
            key = (event["vk"], event["scan"], event["extended"])
            if event["down"]:
                self.keys[key] = dict(event, down=False)
            else:
                self.keys.pop(key, None)
        elif kind == "button":
            if event.get("button") not in ("left", "right", "middle") or type(event.get("down")) is not bool:
                return False
            if event["down"]:
                self.buttons.add(event["button"])
            else:
                self.buttons.discard(event["button"])
        elif kind == "move":
            if type(event.get("absolute")) is not bool:
                return False
            low, high = (0, 65535) if event["absolute"] else (-32767, 32767)
            if not all(integer(event.get(k), low, high) for k in ("x", "y")):
                return False
        elif kind == "wheel":
            if not integer(event.get("delta"), -12000, 12000):
                return False
        elif kind == "pad":
            values = event.get("values")
            if not integer(event.get("index"), 0, 3) or not isinstance(values, list) or len(values) != 7:
                return False
            if not all(integer(v, lo, hi) for v, (lo, hi) in zip(values, [(0, 65535), (0, 255), (0, 255)] + [(-32768, 32767)] * 4)):
                return False
            self.pads.add(event["index"])
        else:
            return False
        if kind in ("move", "pad"):
            self.sequences[category] = sequence
        self.backend.emit(event)
        return True

    def release(self):
        for event in list(self.keys.values()) + [{"type": "button", "button": b, "down": False} for b in self.buttons] + [{"type": "pad", "index": i, "values": [0] * 7} for i in self.pads]:
            try:
                self.backend.emit(event)
            except Exception:
                pass
        self.keys.clear()
        self.buttons.clear()
        self.pads.clear()

    def watchdog(self):
        if time.monotonic() - self.heartbeat > 2:
            self.release()
            self.active = False

    def close(self):
        self.release()
        self.active = False
        self.backend.close()
