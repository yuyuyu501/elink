import threading
import time

import pytest

from elink.core.elinkpad import ElinkPads, Device, INPUT, FEEDBACK, VERSION
from elink.core.input import WindowsInput, create_pads


def until(predicate, timeout=2):
    end = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= end:
            raise AssertionError("worker did not reach expected state")
        time.sleep(0.005)


class FakeDevice:
    def __init__(self):
        self.inputs = []
        self.closed = False
        self.rumble = (1, 1000, 2000)

    def update(self, values):
        self.inputs.append(tuple(values))

    def poll(self):
        return self.rumble

    def close(self):
        self.closed = True


def test_worker_keeps_latest_input_during_slow_open_and_releases():
    opening, proceed = threading.Event(), threading.Event()
    device, feedback = FakeDevice(), []

    def slow_device():
        opening.set()
        assert proceed.wait(2)
        return device

    pads = ElinkPads(feedback.append, device_factory=slow_device)
    try:
        pads.update(2, [1, 0, 0, 0, 0, 0, 0])
        assert opening.wait(1)
        # Input remains nonblocking while PnP/driver opening is delayed.
        for button in range(2, 100):
            pads.update(2, [button, 0, 0, 0, 0, 0, 0])
        proceed.set()
        until(lambda: bool(device.inputs) and bool(feedback))
        assert device.inputs[0][0] == 99
        assert feedback[0] == {"type": "rumble", "index": 2, "left": 1000, "right": 2000}
        with pytest.raises(RuntimeError, match="一个手柄"):
            pads.update(0, [1] * 7)
        pads.update(0, [0] * 7)
        with pads.lock:
            pads.updated = time.monotonic() - 1
        pads.wake.set()
        until(lambda: device.inputs[-1] == (0,) * 7 and feedback[-1]["left"] == 0)
    finally:
        proceed.set()
        pads.close()
    assert device.closed and not pads.thread.is_alive()
    assert feedback[-1]["left"] == feedback[-1]["right"] == 0
    pads.close()


def test_close_during_open_never_submits_old_input():
    entered, proceed = threading.Event(), threading.Event()
    device = FakeDevice()

    def factory():
        entered.set()
        assert proceed.wait(2)
        return device

    pads = ElinkPads(lambda event: None, device_factory=factory)
    try:
        pads.update(0, [0x1000, 0, 0, 0, 0, 0, 0])
        assert entered.wait(1)
        pads.stop.set()
        proceed.set()
    finally:
        proceed.set()
        pads.close()
    assert device.closed and not device.inputs


def test_missing_driver_is_reported_without_fallback(monkeypatch):
    messages = []
    monkeypatch.setattr(Device, "paths", staticmethod(lambda: []))
    backend = WindowsInput(lambda event: None, "elinkpad", messages.append)
    try:
        backend.emit({"type": "pad", "index": 0, "values": [0] * 7})
        until(lambda: any("不可用" in message for message in messages))
        assert isinstance(backend.pads, ElinkPads)
        with pytest.raises(RuntimeError, match="未就绪"):
            backend.emit({"type": "pad", "index": 0, "values": [0] * 7})
        assert backend.pad_backend == "elinkpad"
    finally:
        backend.close()
    assert backend.pads is None


def test_backend_selection_is_explicit():
    with pytest.raises(ValueError):
        WindowsInput(lambda event: None, "unknown")
    with pytest.raises(RuntimeError, match="禁用"):
        create_pads("disabled", lambda event: None, lambda message: None)


def test_local_abi_pack_and_rumble_validation():
    assert INPUT.size == 28 and FEEDBACK.size == 16
    device = Device.__new__(Device)
    device.sequence = 0
    calls = []
    device.ioctl = lambda code, data=b"", output_size=0: calls.append(data)
    device.update([0x1000, 255, 128, -32768, 32767, -1, 1])
    assert calls[0].hex() == "010000001c00000001000000000000000010ff800080ff7fffff0100"
    for raw in (b"", FEEDBACK.pack(VERSION + 1, 16, 0, 1, 2), FEEDBACK.pack(VERSION, 15, 0, 1, 2)):
        device.ioctl = lambda *args, **kwargs: raw
        with pytest.raises(RuntimeError):
            device.poll()
    device.ioctl = lambda *args, **kwargs: FEEDBACK.pack(VERSION, 16, 42, 0, 65535)
    assert device.poll() == (42, 0, 65535)
