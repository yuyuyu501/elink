"""设备热插拔监听。

轮询而不是订阅：Windows 有 ``WM_DEVICECHANGE``、Linux 有 udev，但两者都要求
消息循环/事件源线程，而本项目的设备枚举本身就足够便宜（毫秒级），轮询周期
放在 2 秒对「插上摄像头」这类场景完全够用，且不需要任何平台特权。

变更以事件形式投递到 ``event/bus.py``，消费方在自己的线程里重建采集管线。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from backend.event.bus import EventBus
from backend.event.events import Event
from backend.logging.logger import get_logger
from backend.media.device.enumerate import DeviceInfo, DeviceKind, enumerate_devices

logger = get_logger(__name__)


class DeviceAdded(Event):
    device: str = ""
    kind: str = ""


class DeviceRemoved(Event):
    device: str = ""
    kind: str = ""


def _snapshot(kinds: list[DeviceKind]) -> dict[str, DeviceInfo]:
    snapshot: dict[str, DeviceInfo] = {}
    for kind in kinds:
        for device in enumerate_devices(kind):
            snapshot[f"{device.kind.value}:{device.identifier}"] = device
    return snapshot


class DeviceWatcher:
    """周期性比对设备列表并投递事件。"""

    def __init__(
        self,
        kinds: list[DeviceKind] | None = None,
        interval_s: float = 2.0,
        bus: EventBus | None = None,
        on_changed: Callable[[list[DeviceInfo], list[DeviceInfo]], None] | None = None,
    ) -> None:
        self.kinds = kinds or [DeviceKind.DISPLAY, DeviceKind.CAMERA, DeviceKind.MICROPHONE]
        self.interval_s = interval_s
        self.bus = bus
        self.on_changed = on_changed
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._known = _snapshot(self.kinds)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="device-hotplug", daemon=True)
        self._thread.start()
        logger.info("设备热插拔监听已启动（间隔 %.1fs）", self.interval_s)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=self.interval_s + 1)
        self._thread = None

    def poll_once(self) -> tuple[list[DeviceInfo], list[DeviceInfo]]:
        current = _snapshot(self.kinds)
        added_keys = current.keys() - self._known.keys()
        removed_keys = self._known.keys() - current.keys()
        added = [current[key] for key in added_keys]
        removed = [self._known[key] for key in removed_keys]
        self._known = current
        return added, removed

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                added, removed = self.poll_once()
            except Exception:
                logger.exception("设备轮询失败")
                continue
            for device in added:
                logger.info("设备接入: %s", device.name)
                self._publish(DeviceAdded(device=device.identifier, kind=device.kind.value))
            for device in removed:
                logger.info("设备移除: %s", device.name)
                self._publish(DeviceRemoved(device=device.identifier, kind=device.kind.value))
            if (added or removed) and self.on_changed is not None:
                self.on_changed(added, removed)

    def _publish(self, event: Event) -> None:
        if self.bus is not None:
            self.bus.publish(event)
