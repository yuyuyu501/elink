"""Validate captured input and release held buttons when a session loses focus."""

from __future__ import annotations

import threading
import time
from dataclasses import replace

from backend.control_channel.protocol import ControlCommand, EventType, InputEvent


def _integer(value, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high


class InputSession:
    def __init__(self, inject, *, timeout_s: float = 2.0) -> None:
        self._inject = inject
        self._lock = threading.RLock()
        self._held: dict[tuple, InputEvent] = {}
        self.timeout_s = timeout_s
        self.epoch = -1
        self.active = False
        self.heartbeat = 0.0

    def receive(self, message) -> bool:
        with self._lock:
            if isinstance(message, ControlCommand):
                if message.name != "input_focus" or not isinstance(message.args, dict):
                    return False
                epoch, active = message.args.get("epoch"), message.args.get("active")
                if not _integer(epoch, 0, 2**53) or type(active) is not bool or epoch < self.epoch:
                    return False
                # A released/timed-out epoch can never be reactivated by a late heartbeat.
                if epoch == self.epoch and active and not self.active:
                    return False
                if epoch != self.epoch or not active:
                    self.release()
                self.epoch, self.active = epoch, active
                self.heartbeat = time.monotonic()
                return True
            if not isinstance(message, InputEvent) or not isinstance(message.extra, dict):
                return False
            if not self.active or message.extra.get("epoch") != self.epoch:
                return False
            kind = message.event_type
            key = None
            if kind == EventType.KEY:
                if not (_integer(message.key_code, 1, 255)
                        and _integer(message.scan_code, 0, 255)
                        and type(message.pressed) is bool
                        and type(message.extra.get("extended", False)) is bool):
                    return False
                key = (kind, message.key_code, message.scan_code, message.extra.get("extended", False))
            elif kind == EventType.MOUSE_BUTTON:
                if not _integer(message.button, 1, 3) or type(message.pressed) is not bool:
                    return False
                key = (kind, message.button)
            elif kind == EventType.MOUSE_MOVE:
                mode = message.extra.get("coordinates", "pixels")
                bounds = {"normalized": (0, 65535), "relative": (-32767, 32767),
                          "pixels": (-100000, 100000)}.get(mode)
                if bounds is None or not all(_integer(v, *bounds) for v in (message.x, message.y)):
                    return False
            elif kind == EventType.MOUSE_WHEEL:
                if not _integer(message.delta, -12000, 12000):
                    return False
            else:
                return False
            # Record downs before injection: partial OS failures must also be released.
            if key is not None and message.pressed:
                self._held[key] = replace(message, pressed=False,
                                          extra={**message.extra, "positioned": True})
            result = self._inject(message)
            if key is not None and not message.pressed:
                self._held.pop(key, None)
            return bool(result)

    def release(self) -> None:
        with self._lock:
            self.active = False
            first_error = None
            for key, event in list(self._held.items()):
                try:
                    self._inject(event)
                    self._held.pop(key, None)
                except Exception as exc:
                    first_error = first_error or exc
            if first_error is not None:
                raise first_error

    def watchdog(self) -> None:
        with self._lock:
            if self.active and time.monotonic() - self.heartbeat > self.timeout_s:
                self.release()
