"""Authenticated, sequenced motion for an already negotiated SRT session."""

import hmac
import socket
import threading
from dataclasses import replace

from .protocol import InputEvent, EventType, encode_json, FrameDecoder, decode_json


class MotionSender:
    def __init__(self):
        self.epoch = None
        self.sequence = 0
        self.total = [0, 0]
        self.latest = None

    def prepare(self, event):
        epoch = event.extra.get("epoch")
        if epoch != self.epoch:
            self.epoch = epoch
            self.sequence = 0
            self.total = [0, 0]
        self.sequence += 1
        extra = {**event.extra, "motion_seq": self.sequence}
        if event.extra.get("coordinates") == "relative":
            self.total[0] += event.x
            self.total[1] += event.y
            extra["motion_total"] = list(self.total)
        self.latest = replace(event, extra=extra)
        return self.latest


class GamepadOrder:
    def __init__(self):
        self.sequence = 0
        self.received = {}

    def prepare(self, event):
        if event.event_type == EventType.GAMEPAD:
            self.sequence += 1
            return replace(event, extra={**event.extra, "state_seq": self.sequence})
        return event

    def accept(self, event):
        if not isinstance(event, InputEvent) or event.event_type != EventType.GAMEPAD:
            return True
        sequence, index = event.extra.get("state_seq"), event.extra.get("index", 0)
        if (type(sequence) is not int or type(index) is not int
                or sequence <= self.received.get(index, 0)):
            return False
        self.received[index] = sequence
        return True


class UdpMotionChannel:
    def __init__(self, host, port, token, receive, *, server=False, peer=None):
        self.host, self.port, self.receive = host, port, receive
        self.key = token.encode("ascii")
        self.server, self.peer = server, peer
        self.socket = None
        self.closed = threading.Event()
        self.worker = None
        self.order = GamepadOrder()

    def open(self):
        sock = self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((self.host, self.port) if self.server else ("0.0.0.0", 0))
        sock.settimeout(.2)
        self.worker = threading.Thread(target=self._read, name="udp-motion", daemon=True)
        self.worker.start()

    def send_event(self, event):
        data = encode_json(self.order.prepare(event))
        if len(data) > 4096:
            raise ValueError("移动消息过长")
        self.socket.sendto(hmac.digest(self.key, data, "sha256") + data, (self.host, self.port))

    def _read(self):
        while not self.closed.is_set():
            try:
                data, address = self.socket.recvfrom(8192)
                if self.peer and address[0] != self.peer:
                    continue
                if len(data) < 32 or not hmac.compare_digest(
                    data[:32], hmac.digest(self.key, data[32:], "sha256")):
                    continue
                decoder = FrameDecoder()
                frames = decoder.push(data[32:])
                if len(frames) != 1:
                    continue
                event = decode_json(frames[0][1])
                if (isinstance(event, InputEvent) and self.order.accept(event)
                        and event.event_type in (EventType.MOUSE_MOVE, EventType.GAMEPAD)):
                    self.receive(event)
            except socket.timeout:
                continue
            except OSError:
                break
            except (ValueError, TypeError, KeyError):
                continue

    def close(self):
        self.closed.set()
        if self.socket:
            self.socket.close()
        if self.worker and self.worker is not threading.current_thread():
            self.worker.join(timeout=1)
