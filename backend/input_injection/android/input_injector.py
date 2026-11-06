"""Android 输入注入（``InputManager.injectInputEvent``）。

这是一条需要系统权限的路径：应用必须是**系统应用**或持有
``INJECT_EVENTS`` 签名权限，否则 ``injectInputEvent`` 会返回 false。
普通应用无法做到这一点——这是 Android 的安全设计，不应该绕过。

实现通过 pyjnius 构造 ``MotionEvent`` / ``KeyEvent`` 再注入；
坐标使用屏幕物理像素。
"""

from __future__ import annotations

from backend.control_channel import protocol
from backend.errors.base import ElinkError, ErrorCode
from backend.errors.permission import PermissionDeniedError
from backend.logging.logger import get_logger

logger = get_logger(__name__)

try:  # pragma: no cover - 仅 Android 运行环境可用
    from jnius import autoclass, cast  # type: ignore
except ImportError:  # pragma: no cover
    autoclass = None
    cast = None

INJECT_INPUT_EVENT_MODE_ASYNC = 0
ACTION_DOWN = 0
ACTION_UP = 1
ACTION_MOVE = 2
ACTION_SCROLL = 8


class InjectionError(ElinkError):
    code = ErrorCode.IO


class InputInjector:
    """把 ``InputEvent`` 通过 InputManager 注入系统。"""

    backend = "inputmanager"

    def __init__(self) -> None:
        if autoclass is None:
            raise InjectionError("Android 注入需要 pyjnius（pip install pyjnius）")
        self._manager = None
        self.injected = 0

    def open(self) -> None:
        context = autoclass("org.kivy.android.PythonActivity").mActivity
        self._manager = context.getSystemService("input")
        if self._manager is None:
            raise InjectionError("无法获取 InputManager")
        logger.info("Android 输入注入就绪（需要 INJECT_EVENTS 权限）")

    def close(self) -> None:
        self._manager = None

    def inject(self, event: protocol.InputEvent) -> bool:
        if self._manager is None:
            raise InjectionError("注入器未初始化")

        if event.event_type is protocol.EventType.TOUCH:
            return self._inject_touch(event)
        if event.event_type in (protocol.EventType.MOUSE_BUTTON, protocol.EventType.MOUSE_MOVE):
            return self._inject_touch(event, move_only=event.event_type is protocol.EventType.MOUSE_MOVE)
        if event.event_type is protocol.EventType.KEY:
            return self._inject_key(event)
        if event.event_type is protocol.EventType.MOUSE_WHEEL:
            return self._inject_scroll(event)
        return False

    def _inject_touch(self, event: protocol.InputEvent, move_only: bool = False) -> bool:
        motion_event = autoclass("android.view.MotionEvent")
        system_clock = autoclass("android.os.SystemClock")

        action = ACTION_MOVE if move_only else (ACTION_DOWN if event.pressed else ACTION_UP)
        down_time = system_clock.uptimeMillis()

        motion = motion_event.obtain(
            down_time,
            down_time,
            action,
            float(event.x),
            float(event.y),
            0,
        )
        try:
            # source = SOURCE_TOUCHSCREEN(0x1002) 或 SOURCE_MOUSE(0x2002)
            motion.setSource(0x1002)
            if self._manager.injectInputEvent(motion, INJECT_INPUT_EVENT_MODE_ASYNC):
                self.injected += 1
                return True
            logger.warning("injectInputEvent 被拒绝（缺少 INJECT_EVENTS 权限）")
            return False
        finally:
            motion.recycle()

    def _inject_key(self, event: protocol.InputEvent) -> bool:
        key_event = autoclass("android.view.KeyEvent")
        android_event = key_event(
            ACTION_DOWN if event.pressed else ACTION_UP, event.key_code
        )
        if self._manager.injectInputEvent(android_event, INJECT_INPUT_EVENT_MODE_ASYNC):
            self.injected += 1
            return True
        raise PermissionDeniedError("KeyEvent 注入被拒绝（需要系统签名权限）")

    def _inject_scroll(self, event: protocol.InputEvent) -> bool:
        motion_event = autoclass("android.view.MotionEvent")
        system_clock = autoclass("android.os.SystemClock")
        now = system_clock.uptimeMillis()
        motion = motion_event.obtain(now, now, ACTION_SCROLL, float(event.x), float(event.y), 0)
        try:
            motion.setSource(0x2002)  # SOURCE_MOUSE
            motion.setAxisValue(0x0000000a, float(event.delta))  # AXIS_VSCROLL
            if self._manager.injectInputEvent(motion, INJECT_INPUT_EVENT_MODE_ASYNC):
                self.injected += 1
                return True
            return False
        finally:
            motion.recycle()

    def __enter__(self) -> "InputInjector":
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
