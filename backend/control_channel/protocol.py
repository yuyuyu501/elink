"""控制消息定义与协议编解码。

三条通道（TCP / WebSocket / DataChannel）共用这一份协议：消息定义在这里，
通道只负责搬字节。协议分成两类帧：

- **JSON 帧**：低频、可读、便于调试（键鼠事件、剪贴板、控制指令）；
- **二进制帧**：高频、低开销（滚轮连发、批量键鼠）。

帧头统一为 ``1 字节魔数 + 1 字节版本 + 1 字节类型 + 1 字节保留 + 4 字节长度``，
其中长度指负载长度；解析时先读头再读体，天然支持流式拆包。
"""

from __future__ import annotations

import enum
import json
import struct
from dataclasses import asdict, dataclass, field
from typing import Any

from backend.errors.base import ElinkError, ErrorCode

MAGIC = 0xE1
VERSION = 1
HEADER = struct.Struct("!BBBB I")
MAX_PAYLOAD = 4 * 1024 * 1024


class ProtocolError(ElinkError):
    code = ErrorCode.INVALID_STATE


class FrameType(enum.IntEnum):
    JSON = 1
    BINARY = 2
    PING = 3
    PONG = 4


class EventType(str, enum.Enum):
    """输入事件类型（与 ``input_capture`` / ``input_injection`` 共用）。"""

    MOUSE_MOVE = "mouse_move"
    MOUSE_BUTTON = "mouse_button"
    MOUSE_WHEEL = "mouse_wheel"
    KEY = "key"
    TEXT = "text"
    TOUCH = "touch"
    CLIPBOARD = "clipboard"
    GAMEPAD = "gamepad"
    CONTROL = "control"


@dataclass
class InputEvent:
    """一个标准化输入事件。"""

    event_type: EventType
    timestamp_ms: int = 0
    x: int = 0
    y: int = 0
    button: int = 0
    pressed: bool = False
    delta: int = 0
    key_code: int = 0
    scan_code: int = 0
    modifiers: int = 0
    text: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["event_type"] = self.event_type.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "InputEvent":
        payload = dict(data)
        payload["event_type"] = EventType(payload["event_type"])
        return cls(**payload)


@dataclass
class ControlCommand:
    """控制类指令（非输入事件）。"""

    name: str
    args: dict[str, Any] = field(default_factory=dict)
    timestamp_ms: int = 0


def encode_frame(frame_type: FrameType, payload: bytes) -> bytes:
    if len(payload) > MAX_PAYLOAD:
        raise ProtocolError("控制帧过大", detail=f"{len(payload)} 字节")
    return HEADER.pack(MAGIC, VERSION, int(frame_type), 0, len(payload)) + payload


def encode_json(message: dict[str, Any] | InputEvent | ControlCommand) -> bytes:
    if isinstance(message, InputEvent):
        body = {"kind": "input", **message.to_dict()}
    elif isinstance(message, ControlCommand):
        body = {"kind": "command", "name": message.name, "args": message.args, "timestamp_ms": message.timestamp_ms}
    else:
        body = message
    payload = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return encode_frame(FrameType.JSON, payload)


class FrameDecoder:
    """流式拆包：喂入任意长度的字节，吐出完整帧。"""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self.dropped = 0

    def push(self, data: bytes) -> list[tuple[FrameType, bytes]]:
        self._buffer += data
        frames: list[tuple[FrameType, bytes]] = []

        while True:
            if len(self._buffer) < HEADER.size:
                break
            magic, version, frame_type, _reserved, length = HEADER.unpack(
                self._buffer[: HEADER.size]
            )
            if magic != MAGIC or version != VERSION:
                # 失步：丢 1 字节重新寻找魔数，而不是整体清空（可能只是脏字节）
                self._buffer = self._buffer[1:]
                self.dropped += 1
                continue
            if length > MAX_PAYLOAD:
                raise ProtocolError("控制帧长度超限", detail=str(length))
            if len(self._buffer) < HEADER.size + length:
                break

            payload = bytes(self._buffer[HEADER.size : HEADER.size + length])
            del self._buffer[: HEADER.size + length]
            frames.append((FrameType(frame_type), payload))

        return frames

    def reset(self) -> None:
        self._buffer.clear()


def decode_json(payload: bytes) -> InputEvent | ControlCommand | dict[str, Any]:
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        # UnicodeDecodeError 说明负载根本不是文本（例如把协议帧头当负载），
        # 同样归为协议错误，避免把收包线程直接打死
        raise ProtocolError("控制帧不是合法 JSON", detail=str(exc)) from exc

    kind = data.pop("kind", "")
    if kind == "input":
        return InputEvent.from_dict(data)
    if kind == "command":
        return ControlCommand(**data)
    return data


def build_key_event(
    key_code: int, pressed: bool, *, scan_code: int = 0, modifiers: int = 0
) -> InputEvent:
    return InputEvent(
        event_type=EventType.KEY,
        key_code=key_code,
        scan_code=scan_code,
        pressed=pressed,
        modifiers=modifiers,
    )


def build_mouse_move(x: int, y: int) -> InputEvent:
    return InputEvent(event_type=EventType.MOUSE_MOVE, x=x, y=y)


def build_mouse_button(x: int, y: int, button: int, pressed: bool) -> InputEvent:
    return InputEvent(event_type=EventType.MOUSE_BUTTON, x=x, y=y, button=button, pressed=pressed)


def build_mouse_wheel(x: int, y: int, delta: int) -> InputEvent:
    return InputEvent(event_type=EventType.MOUSE_WHEEL, x=x, y=y, delta=delta)
