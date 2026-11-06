"""Android 输入事件捕获。

Android 应用层拿不到「全局」输入，只有两种可行路径：

1. **getevent**（需要 root 或 shell 权限）：直接读内核输入事件，能看到所有
   设备的原始事件，本文件实现这条；
2. AccessibilityService：无需 root，但要用户手动开启无障碍服务，且 API 受限。

``getevent -lt`` 的文本格式稳定（``/dev/input/eventN: EV_KEY KEY_A DOWN``），
解析它比自己维护 JNI 层要省事得多，也方便现场排障。
"""

from __future__ import annotations

import shutil
import subprocess

from backend.control_channel import protocol
from backend.errors.base import ElinkError, ErrorCode
from backend.errors.permission import PermissionDeniedError
from backend.logging.logger import get_logger

logger = get_logger(__name__)

KEY_CODE_MAP = {
    "KEY_A": 29, "KEY_B": 30, "KEY_C": 31, "KEY_D": 32, "KEY_E": 33,
    "KEY_ENTER": 66, "KEY_ESC": 111, "KEY_SPACE": 62, "KEY_TAB": 61,
    "KEY_BACK": 4, "KEY_HOME": 3, "KEY_VOLUMEUP": 115, "KEY_VOLUMEDOWN": 114,
}

BTN_MAP = {
    "BTN_TOUCH": 1,
    "BTN_LEFT": 1,
    "BTN_RIGHT": 2,
}


class CaptureError(ElinkError):
    code = ErrorCode.IO


class InputEventCapture:
    """解析 ``getevent -lt`` 输出。"""

    backend = "getevent"

    def __init__(self) -> None:
        self._proc: subprocess.Popen[str] | None = None
        self._x = 0
        self._y = 0

    def open(self) -> None:
        binary = shutil.which("getevent")
        if not binary:
            raise PermissionDeniedError(
                "找不到 getevent（需要 root shell 或 adb shell 环境）"
            )
        try:
            self._proc = subprocess.Popen(
                [binary, "-lt"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise CaptureError("启动 getevent 失败", detail=str(exc)) from exc
        logger.info("Android 输入捕获就绪（getevent）")

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
            event = self._parse(line.strip())
            if event is not None:
                events.append(event)
            break
        return events

    def _parse(self, line: str) -> protocol.InputEvent | None:
        if "EV_ABS" in line:
            tokens = line.split()
            code = tokens[-2] if len(tokens) >= 2 else ""
            try:
                value = int(tokens[-1], 16) if tokens[-1].startswith("0x") else int(tokens[-1])
            except (ValueError, IndexError):
                return None
            if code == "ABS_MT_POSITION_X":
                self._x = value
            elif code == "ABS_MT_POSITION_Y":
                self._y = value
            else:
                return None
            return protocol.InputEvent(
                event_type=protocol.EventType.TOUCH, x=self._x, y=self._y
            )

        if "EV_KEY" in line:
            tokens = line.split()
            pressed = "DOWN" in line or "0x00000001" in line
            for token in tokens:
                if token in KEY_CODE_MAP:
                    return protocol.InputEvent(
                        event_type=protocol.EventType.KEY,
                        key_code=KEY_CODE_MAP[token],
                        pressed=pressed,
                    )
                if token in BTN_MAP:
                    return protocol.InputEvent(
                        event_type=protocol.EventType.MOUSE_BUTTON,
                        x=self._x,
                        y=self._y,
                        button=BTN_MAP[token],
                        pressed=pressed,
                    )
        return None

    def __enter__(self) -> "InputEventCapture":
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
