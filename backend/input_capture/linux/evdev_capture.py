"""Linux 输入捕获（evdev，X11 会话）。

直接读 ``/dev/input/event*``：结构体是 ``struct input_event``（64 位下 24 字节：
``llHHi`` = 秒、微秒、type、code、value）。这种方式拿到的是**原始输入**，
不受窗口焦点影响，适合远程控制。

前置条件：当前用户要能读设备节点（通常需要加入 ``input`` 组或配置 udev 规则）。
Wayland 下读 evdev 也能拿到事件，但注入需要 uinput（见 ``input_injection/linux``）。
"""

from __future__ import annotations

import glob
import os
import select
import struct

from backend.control_channel import protocol
from backend.errors.base import ElinkError, ErrorCode
from backend.errors.permission import PermissionDeniedError
from backend.logging.logger import get_logger

logger = get_logger(__name__)

EV_SYN = 0x00
EV_KEY = 0x01
EV_REL = 0x02
EV_ABS = 0x03

REL_X = 0x00
REL_Y = 0x01
REL_WHEEL = 0x08

BTN_LEFT = 0x110
BTN_RIGHT = 0x111
BTN_MIDDLE = 0x112

EVENT_STRUCT = struct.Struct("llHHi")
EVENT_SIZE = EVENT_STRUCT.size


class CaptureError(ElinkError):
    code = ErrorCode.IO


def find_devices() -> list[str]:
    """找出键盘/鼠标设备节点（``/dev/input/event*``）。"""
    return sorted(glob.glob("/dev/input/event*"))


class EvdevCapture:
    """从多个 evdev 节点采集键鼠事件。"""

    backend = "evdev"

    def __init__(self, devices: list[str] | None = None) -> None:
        self.devices = devices if devices is not None else find_devices()
        self._fds: dict[int, str] = {}
        self._buffer: dict[int, bytearray] = {}
        self._pending: list[protocol.InputEvent] = []
        self._x = 0
        self._y = 0

    def open(self) -> None:
        if not self.devices:
            raise CaptureError("没有找到 /dev/input/event* 设备")
        for path in self.devices:
            try:
                handle = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            except PermissionError as exc:
                logger.warning("无权读取 %s（需要加入 input 组）: %s", path, exc)
                continue
            except OSError as exc:
                logger.debug("打开 %s 失败: %s", path, exc)
                continue
            self._fds[handle] = path
            self._buffer[handle] = bytearray()

        if not self._fds:
            raise PermissionDeniedError(
                "无法读取任何输入设备（权限不足）",
                detail="sudo usermod -aG input $USER 后重新登录",
            )
        logger.info("evdev 采集就绪: %d 个设备", len(self._fds))

    def close(self) -> None:
        for handle in list(self._fds):
            try:
                os.close(handle)
            except OSError:
                pass
        self._fds.clear()
        self._buffer.clear()

    def read(self, timeout_s: float = 0.1) -> list[protocol.InputEvent]:
        if not self._fds:
            return []
        readable, _, _ = select.select(list(self._fds), [], [], timeout_s)
        events: list[protocol.InputEvent] = []
        for handle in readable:
            try:
                data = os.read(handle, EVENT_SIZE * 64)
            except BlockingIOError:
                continue
            except OSError as exc:
                logger.debug("读取 %s 失败: %s", self._fds.get(handle), exc)
                continue
            events.extend(self._parse(handle, data))
        return events

    def _parse(self, handle: int, data: bytes) -> list[protocol.InputEvent]:
        buffer = self._buffer[handle]
        buffer += data
        events: list[protocol.InputEvent] = []

        while len(buffer) >= EVENT_SIZE:
            seconds, micros, event_type, code, value = EVENT_STRUCT.unpack(
                buffer[:EVENT_SIZE]
            )
            del buffer[:EVENT_SIZE]
            timestamp_ms = seconds * 1000 + micros // 1000

            if event_type == EV_KEY:
                if code >= BTN_LEFT and code <= BTN_MIDDLE:
                    events.append(
                        protocol.InputEvent(
                            event_type=protocol.EventType.MOUSE_BUTTON,
                            timestamp_ms=timestamp_ms,
                            x=self._x,
                            y=self._y,
                            button=code - BTN_LEFT + 1,
                            pressed=bool(value),
                        )
                    )
                elif value in (0, 1, 2):
                    events.append(
                        protocol.InputEvent(
                            event_type=protocol.EventType.KEY,
                            timestamp_ms=timestamp_ms,
                            key_code=code,
                            pressed=bool(value),
                        )
                    )
            elif event_type == EV_REL:
                if code == REL_X:
                    self._x += value
                elif code == REL_Y:
                    self._y += value
                elif code == REL_WHEEL:
                    events.append(
                        protocol.InputEvent(
                            event_type=protocol.EventType.MOUSE_WHEEL,
                            timestamp_ms=timestamp_ms,
                            x=self._x,
                            y=self._y,
                            delta=-value * 120,  # 与 Windows 的 120 步进对齐
                        )
                    )
                if code in (REL_X, REL_Y):
                    events.append(
                        protocol.InputEvent(
                            event_type=protocol.EventType.MOUSE_MOVE,
                            timestamp_ms=timestamp_ms,
                            x=self._x,
                            y=self._y,
                        )
                    )
            elif event_type == EV_SYN:
                # 一次同步代表一批事件结束，可以在这里做批处理优化
                pass

        return events

    def __enter__(self) -> "EvdevCapture":
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
