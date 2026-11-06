"""Linux 输入捕获（libinput / Wayland）。

Wayland 下应用拿不到全局输入，但 ``libinput`` 可以从设备层读到原始事件；
再往上是合成器（通过 portal/RemoteDesktop 接口转发）。本文件走的是前者：
调用 ``libinput debug-events`` 并解析它的文本输出。

为什么用子进程而不是绑 libinput 的 C API：libinput 是面向事件循环的回调式
库，绑定它的成本远高于解析 ``debug-events`` 的稳定文本格式；而且
``libinput`` 本体必须由有权限的进程（通常是 root 或 input 组）启动，
子进程模型反而更贴合这一点。
"""

from __future__ import annotations

import shutil
import subprocess
from collections import deque

from backend.control_channel import protocol
from backend.errors.base import ElinkError, ErrorCode
from backend.logging.logger import get_logger

logger = get_logger(__name__)


class CaptureError(ElinkError):
    code = ErrorCode.IO


class LibinputCapture:
    """解析 ``libinput debug-events`` 的文本输出。"""

    backend = "libinput"

    def __init__(self) -> None:
        self._proc: subprocess.Popen[str] | None = None
        self._x = 0
        self._y = 0
        self._stderr_tail: deque[str] = deque(maxlen=20)

    def open(self) -> None:
        binary = shutil.which("libinput")
        if not binary:
            raise CaptureError(
                "未找到 libinput 命令（Debian/Ubuntu: apt install libinput-tools）"
            )
        try:
            self._proc = subprocess.Popen(
                [binary, "debug-events"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise CaptureError("启动 libinput debug-events 失败", detail=str(exc)) from exc
        logger.info("libinput 采集就绪（Wayland/X11 均可）")

    def close(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except (subprocess.TimeoutExpired, OSError):
            proc.kill()
        self._proc = None

    def read(self) -> list[protocol.InputEvent]:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return []

        events: list[protocol.InputEvent] = []
        while True:
            line = proc.stdout.readline()
            if not line:
                break
            event = self._parse_line(line.strip())
            if event is not None:
                events.append(event)
            # 每次只读一行就返回，避免阻塞调用方
            break
        return events

    def _parse_line(self, line: str) -> protocol.InputEvent | None:
        # 形如： event4   POINTER_MOTION   +2.34s	  -3.00/  1.00
        if "POINTER_MOTION" in line:
            delta = line.rsplit("\t", 1)[-1].strip()
            try:
                dx, dy = (float(part) for part in delta.replace(" ", "").split("/"))
            except ValueError:
                return None
            self._x += int(dx)
            self._y += int(dy)
            return protocol.InputEvent(
                event_type=protocol.EventType.MOUSE_MOVE, x=self._x, y=self._y
            )

        if "POINTER_BUTTON" in line:
            tokens = line.split()
            pressed = "pressed" in line
            button_code = 1
            if "BTN_RIGHT" in line:
                button_code = 2
            elif "BTN_MIDDLE" in line:
                button_code = 3
            del tokens
            return protocol.InputEvent(
                event_type=protocol.EventType.MOUSE_BUTTON,
                x=self._x,
                y=self._y,
                button=button_code,
                pressed=pressed,
            )

        if "POINTER_AXIS" in line or "SCROLL" in line:
            return protocol.InputEvent(
                event_type=protocol.EventType.MOUSE_WHEEL,
                x=self._x,
                y=self._y,
                delta=120 if "vertical" in line else -120,
            )

        if "KEYBOARD_KEY" in line:
            tokens = line.split()
            pressed = "pressed" in line
            key_code = 0
            for token in tokens:
                if token in {"KEY_A", "KEY_ENTER", "KEY_ESC"}:
                    key_code = {"KEY_A": 30, "KEY_ENTER": 28, "KEY_ESC": 1}.get(token, 0)
            return protocol.InputEvent(
                event_type=protocol.EventType.KEY, key_code=key_code, pressed=pressed
            )

        return None

    def __enter__(self) -> "LibinputCapture":
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
