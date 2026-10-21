"Platform-independent input validation, ordering and release."
import json
import time

PAD_BACKENDS = ("vigem", "elinkpad", "disabled", "uinput")


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

