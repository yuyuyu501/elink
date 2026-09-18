"""ElinkPad v1 local IOCTL client and bounded input worker. No driver installer."""
from __future__ import annotations

import ctypes as c
from ctypes import wintypes as w
import struct
import threading
import time
import uuid

VERSION = 1
QUERY, CLAIM, UPDATE, POLL, RELEASE = (0x83376000, 0x8337E004, 0x8337E008, 0x8337E00C, 0x8337E010)
HEADER = struct.pack("<II", VERSION, 8)
INPUT = struct.Struct("<IIQHBBhhhh")
FEEDBACK = struct.Struct("<IIIHH")


class Overlapped(c.Structure):
    _fields_ = [("internal", c.c_size_t), ("internal_high", c.c_size_t),
                ("offset", w.DWORD), ("offset_high", w.DWORD), ("event", w.HANDLE)]


class Device:
    def __init__(self):
        self.handle = None
        self.sequence = 0
        self.kernel = c.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateFileW": ([w.LPCWSTR, w.DWORD, w.DWORD, c.c_void_p, w.DWORD, w.DWORD, w.HANDLE], w.HANDLE),
            "CloseHandle": ([w.HANDLE], w.BOOL),
            "CreateEventW": ([c.c_void_p, w.BOOL, w.BOOL, w.LPCWSTR], w.HANDLE),
            "DeviceIoControl": ([w.HANDLE, w.DWORD, c.c_void_p, w.DWORD, c.c_void_p, w.DWORD, c.POINTER(w.DWORD), c.POINTER(Overlapped)], w.BOOL),
            "GetOverlappedResultEx": ([w.HANDLE, c.POINTER(Overlapped), c.POINTER(w.DWORD), w.DWORD, w.BOOL], w.BOOL),
            "CancelIoEx": ([w.HANDLE, c.POINTER(Overlapped)], w.BOOL),
            "GetOverlappedResult": ([w.HANDLE, c.POINTER(Overlapped), c.POINTER(w.DWORD), w.BOOL], w.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = arguments, result
        paths = self.paths()
        if len(paths) != 1:
            raise RuntimeError("ElinkPad 实验设备未就绪或存在多个设备；需在测试系统加载驱动，并只保留一个 ElinkPadDevice。")
        handle = self.kernel.CreateFileW(paths[0], 0xC0000000, 3, None, 3, 0x40000000, None)
        if handle == c.c_void_p(-1).value:
            raise c.WinError(c.get_last_error())
        self.handle = handle
        try:
            info = self.ioctl(QUERY, output_size=16)
            if len(info) != 16 or struct.unpack("<IIII", info) != (VERSION, 16, 1, 500):
                raise RuntimeError("ElinkPad 驱动协议版本或能力不匹配。")
            self.ioctl(CLAIM, HEADER)
        except BaseException:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
            raise

    @staticmethod
    def paths():
        manager = c.WinDLL("cfgmgr32")
        guid = c.create_string_buffer(uuid.UUID("654cd75a-921d-434f-94bb-807daa86b076").bytes_le, 16)
        size_fn = manager.CM_Get_Device_Interface_List_SizeW
        size_fn.argtypes = [c.POINTER(w.ULONG), c.c_void_p, w.LPCWSTR, w.ULONG]
        size_fn.restype = w.ULONG
        list_fn = manager.CM_Get_Device_Interface_ListW
        list_fn.argtypes = [c.c_void_p, w.LPCWSTR, w.LPWSTR, w.ULONG, w.ULONG]
        list_fn.restype = w.ULONG
        for _ in range(3):
            length = w.ULONG()
            if size_fn(c.byref(length), guid, None, 0) or not 1 <= length.value <= 65536:
                raise RuntimeError("无法枚举 ElinkPad 设备接口。")
            buffer = c.create_unicode_buffer(length.value)
            result = list_fn(guid, None, buffer, length.value, 0)
            if result == 0:
                return [path for path in buffer[:].split("\0") if path]
            if result != 0x1A:
                break
        raise RuntimeError("ElinkPad 设备列表在枚举期间发生变化，请重试。")

    def ioctl(self, code, data=b"", output_size=0):
        incoming = c.create_string_buffer(data) if data else None
        outgoing = c.create_string_buffer(output_size) if output_size else None
        count = w.DWORD()
        event = self.kernel.CreateEventW(None, True, False, None)
        if not event:
            raise c.WinError(c.get_last_error())
        operation = Overlapped(event=event)
        try:
            ok = self.kernel.DeviceIoControl(self.handle, code, incoming, len(data), outgoing,
                                            output_size, c.byref(count), c.byref(operation))
            if not ok:
                error = c.get_last_error()
                if error != 997:
                    raise c.WinError(error)
                if not self.kernel.GetOverlappedResultEx(self.handle, c.byref(operation), c.byref(count), 500, False):
                    error = c.get_last_error()
                    self.kernel.CancelIoEx(self.handle, c.byref(operation))
                    # Keep buffers/OVERLAPPED alive until WDF acknowledges cancellation.
                    self.kernel.GetOverlappedResult(self.handle, c.byref(operation), c.byref(count), True)
                    raise c.WinError(error)
            if count.value > output_size:
                raise RuntimeError("ElinkPad 返回长度无效。")
            return outgoing.raw[:count.value] if outgoing is not None else b""
        finally:
            self.kernel.CloseHandle(event)

    def update(self, values):
        self.sequence += 1
        self.ioctl(UPDATE, INPUT.pack(VERSION, INPUT.size, self.sequence, *values))

    def poll(self):
        raw = self.ioctl(POLL, output_size=FEEDBACK.size)
        if len(raw) != FEEDBACK.size:
            raise RuntimeError("ElinkPad 震动反馈长度无效。")
        version, size, serial, left, right = FEEDBACK.unpack(raw)
        if (version, size) != (VERSION, FEEDBACK.size):
            raise RuntimeError("ElinkPad 震动反馈版本无效。")
        return serial, left, right

    def close(self):
        if self.handle is not None:
            try:
                self.ioctl(RELEASE, HEADER)
            finally:
                self.kernel.CloseHandle(self.handle)
                self.handle = None


class ElinkPads:
    """Single target; slow device operations never run on the media event loop."""
    def __init__(self, feedback, notify=lambda message: None, *, device_factory=Device):
        self.feedback, self.notify, self.device_factory = feedback, notify, device_factory
        self.lock = threading.Lock()
        self.wake, self.stop = threading.Event(), threading.Event()
        self.index = None
        self.values = (0,) * 7
        self.updated = 0.0
        self.error = ""
        self.thread = None

    def update(self, index, values):
        with self.lock:
            if self.stop.is_set():
                raise RuntimeError("ElinkPad 会话已关闭。")
            if self.error:
                raise RuntimeError(self.error)
            if self.index is not None and self.index != index:
                if any(values):
                    raise RuntimeError("ElinkPad 实验后端只支持一个手柄；请仅连接一个控制端手柄。")
                return
            self.index = index
            self.values, self.updated = tuple(values), time.monotonic()
            if self.thread is None:
                self.thread = threading.Thread(target=self.run, name="ElinkPad", daemon=True)
                self.thread.start()
        self.wake.set()

    def run(self):
        device = None
        serial = None
        last_send = 0.0
        previous = None
        try:
            device = self.device_factory()
            self.notify("ElinkPad 实验设备已取得控制权（不代表游戏兼容性已验证）。")
            while not self.stop.is_set():
                self.wake.wait(0.008)
                self.wake.clear()
                if self.stop.is_set():
                    break
                with self.lock:
                    now = time.monotonic()
                    fresh = now - self.updated < 0.5
                    values = self.values if fresh else (0,) * 7
                    index = self.index
                if values != previous or now - last_send >= 0.1:
                    device.update(values)
                    previous, last_send = values, now
                current, left, right = device.poll()
                rumble_state = (current, fresh)
                if rumble_state != serial:
                    serial = rumble_state
                    if not fresh:
                        left = right = 0
                    self.feedback({"type": "rumble", "index": index, "left": left, "right": right})
        except Exception as exc:
            with self.lock:
                self.error = f"ElinkPad 不可用：{exc}；画面、声音和键鼠仍可使用。"
            self.notify(self.error)
        finally:
            if device is not None:
                try:
                    device.close()
                except Exception as exc:
                    self.notify(f"ElinkPad 释放失败，驱动将超时归零：{exc}")
            if self.index is not None:
                self.feedback({"type": "rumble", "index": self.index, "left": 0, "right": 0})

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=2)
            if self.thread.is_alive():
                self.notify("ElinkPad I/O 尚未退出，驱动超时归零保护仍生效。")
