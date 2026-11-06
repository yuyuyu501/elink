"""Linux 输入注入（``/dev/uinput`` 虚拟设备）。

做法：创建一台虚拟键鼠设备（``uinput``），把 ``input_event`` 写进去，
内核就会像真实设备一样把事件分发给当前合成器/应用。这是 Wayland 下唯一
不依赖合成器接口的通用注入方式。

权限：需要 ``/dev/uinput`` 可写（``sudo usermod -aG input`` 或 udev 规则），
否则注入会失败——这是设计上的安全边界，不应该绕过。
"""

from __future__ import annotations

import os
import struct
import time

from backend.control_channel import protocol
from backend.errors.base import ElinkError, ErrorCode
from backend.errors.permission import PermissionDeniedError
from backend.logging.logger import get_logger

logger = get_logger(__name__)

UINPUT_PATH = "/dev/uinput"

EV_SYN = 0x00
EV_KEY = 0x01
EV_REL = 0x02
SYN_REPORT = 0

REL_X = 0x00
REL_Y = 0x01
REL_WHEEL = 0x08
BTN_LEFT = 0x110
BTN_RIGHT = 0x111
BTN_MIDDLE = 0x112

UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_SET_RELBIT = 0x40045566
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502

EVENT_STRUCT = struct.Struct("llHHi")


class InjectionError(ElinkError):
    code = ErrorCode.IO


class UinputInjector:
    """虚拟键鼠设备。"""

    backend = "uinput"

    def __init__(self, device_name: str = "elink-virtual-input") -> None:
        self.device_name = device_name
        self._fd: int | None = None
        self.injected = 0

    def open(self) -> None:
        global fcntl
        import fcntl as _fcntl  # 仅 Linux 提供；延迟导入保证模块在其它平台可导入

        fcntl = _fcntl
        try:
            self._fd = os.open(UINPUT_PATH, os.O_WRONLY | os.O_NONBLOCK)
        except PermissionError as exc:
            raise PermissionDeniedError(
                f"无权写入 {UINPUT_PATH}",
                detail="sudo usermod -aG input $USER 并重新登录",
            ) from exc
        except OSError as exc:
            raise InjectionError(f"无法打开 {UINPUT_PATH}", detail=str(exc)) from exc

        self._setup_capabilities()
        self._create_device()
        logger.info("uinput 虚拟设备已创建: %s", self.device_name)

    def _setup_capabilities(self) -> None:
        assert self._fd is not None
        fcntl.ioctl(self._fd, UI_SET_EVBIT, EV_KEY)
        fcntl.ioctl(self._fd, UI_SET_EVBIT, EV_REL)
        for code in (BTN_LEFT, BTN_RIGHT, BTN_MIDDLE):
            fcntl.ioctl(self._fd, UI_SET_KEYBIT, code)
        for code in (REL_X, REL_Y, REL_WHEEL):
            fcntl.ioctl(self._fd, UI_SET_RELBIT, code)

    def _create_device(self) -> None:
        """写 ``uinput_user_dev`` 结构体并创建设备。"""
        assert self._fd is not None
        name = self.device_name.encode()[:80].ljust(80, b"\x00")
        # struct uinput_user_dev { char name[80]; input_id id; ... absmax/absmin... }
        payload = name + struct.pack("HHH", 0x03, 0x1234, 0x5678)
        payload += struct.pack("i", 0) * 4  # ff_effects_max
        payload += struct.pack("i", 0) * 256  # absmax
        payload += struct.pack("i", 0) * 256  # absmin
        payload += struct.pack("i", 0) * 256  # absfuzz
        payload += struct.pack("i", 0) * 256  # absflat
        os.write(self._fd, payload)
        fcntl.ioctl(self._fd, UI_DEV_CREATE)
        time.sleep(0.1)  # 等 udev 建好节点，否则立即写事件会丢

    def close(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.ioctl(self._fd, UI_DEV_DESTROY)
        except OSError:
            pass
        os.close(self._fd)
        self._fd = None

    # ---- 注入 ----
    def inject(self, event: protocol.InputEvent) -> bool:
        if self._fd is None:
            raise InjectionError("uinput 设备未打开")

        handlers = {
            protocol.EventType.MOUSE_MOVE: self._move,
            protocol.EventType.MOUSE_BUTTON: self._button,
            protocol.EventType.MOUSE_WHEEL: self._wheel,
            protocol.EventType.KEY: self._key,
            protocol.EventType.TEXT: self._text,
        }
        handler = handlers.get(event.event_type)
        if handler is None:
            return False
        handler(event)
        self.injected += 1
        return True

    def _emit(self, event_type: int, code: int, value: int) -> None:
        assert self._fd is not None
        now = time.time()
        payload = EVENT_STRUCT.pack(int(now), int((now % 1) * 1_000_000), event_type, code, value)
        os.write(self._fd, payload)

    def _sync(self) -> None:
        self._emit(EV_SYN, SYN_REPORT, 0)

    def _move(self, event: protocol.InputEvent) -> None:
        # uinput 只支持相对移动：这里按「当前位置 -> 目标位置」的差值发送
        self._emit(EV_REL, REL_X, event.extra.get("dx", 0))
        self._emit(EV_REL, REL_Y, event.extra.get("dy", 0))
        self._sync()

    def _button(self, event: protocol.InputEvent) -> None:
        code = {1: BTN_LEFT, 2: BTN_RIGHT, 3: BTN_MIDDLE}.get(event.button, BTN_LEFT)
        self._emit(EV_KEY, code, 1 if event.pressed else 0)
        self._sync()

    def _wheel(self, event: protocol.InputEvent) -> None:
        self._emit(EV_REL, REL_WHEEL, -event.delta // 120)
        self._sync()

    def _key(self, event: protocol.InputEvent) -> None:
        self._emit(EV_KEY, event.key_code, 1 if event.pressed else 0)
        self._sync()

    def _text(self, event: protocol.InputEvent) -> None:
        logger.debug("uinput 文本注入需要 keymap（暂未实现）: %r", event.text)

    def __enter__(self) -> "UinputInjector":
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
