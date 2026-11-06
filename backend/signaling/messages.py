"""信令消息定义与编解码。

所有消息都是不可变数据对象，序列化成单层 JSON（``{"type": ..., "payload": ...}``）。
类型注册表让「加一种消息」只需要在 :data:`MESSAGE_TYPES` 里登记一次，
避免收发两侧各自 if/else 到不一致。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from backend.errors.base import ElinkError, ErrorCode


class SignalingError(ElinkError):
    code = ErrorCode.NETWORK


@dataclass(frozen=True)
class Message:
    """信令消息基类。"""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    timestamp: float = field(default_factory=time.time)

    @property
    def type(self) -> str:
        return type(self).__name__

    def to_json(self) -> str:
        payload = {key: value for key, value in asdict(self).items()}
        return json.dumps(
            {"type": self.type, "payload": payload}, separators=(",", ":"), ensure_ascii=False
        )

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "Message":
        return cls(**payload)


@dataclass(frozen=True)
class Hello(Message):
    """握手：声明身份、角色与能力摘要。"""

    client_id: str = ""
    role: str = "publisher"  # publisher / subscriber / both
    version: str = "1"


@dataclass(frozen=True)
class Join(Message):
    """加入会话（房间）。"""

    session_id: str = ""
    token: str = ""


@dataclass(frozen=True)
class Leave(Message):
    session_id: str = ""
    reason: str = ""


@dataclass(frozen=True)
class Offer(Message):
    sdp: str = ""
    session_id: str = ""


@dataclass(frozen=True)
class Answer(Message):
    sdp: str = ""
    session_id: str = ""


@dataclass(frozen=True)
class IceCandidate(Message):
    candidate: str = ""
    sdp_mid: str = "0"
    sdp_m_line_index: int = 0


@dataclass(frozen=True)
class CapabilityOffer(Message):
    """SRT 等无 SDP 的通道用它交换能力。"""

    codecs: tuple[str, ...] = ()
    max_width: int = 0
    max_height: int = 0
    max_fps: int = 0
    encoders: tuple[str, ...] = ()


@dataclass(frozen=True)
class CapabilityAnswer(Message):
    codec: str = ""
    encoder: str = ""
    width: int = 0
    height: int = 0
    fps: int = 0
    rejected_reason: str = ""


@dataclass(frozen=True)
class Bye(Message):
    session_id: str = ""
    reason: str = "normal"


@dataclass(frozen=True)
class ErrorMessage(Message):
    code: str = ""
    message: str = ""
    fatal: bool = False


@dataclass(frozen=True)
class Ping(Message):
    nonce: str = ""


@dataclass(frozen=True)
class Pong(Message):
    nonce: str = ""
    latency_ms: float = 0.0


MESSAGE_TYPES: dict[str, type[Message]] = {
    cls.__name__: cls
    for cls in (
        Hello,
        Join,
        Leave,
        Offer,
        Answer,
        IceCandidate,
        CapabilityOffer,
        CapabilityAnswer,
        Bye,
        ErrorMessage,
        Ping,
        Pong,
    )
}


def encode(message: Message) -> str:
    return message.to_json()


def decode(text: str | bytes) -> Message:
    """反序列化；未知类型 / 字段不匹配都抛 ``SignalingError``。"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SignalingError("信令消息不是合法 JSON", detail=str(exc)) from exc

    if not isinstance(data, dict) or "type" not in data:
        raise SignalingError("信令消息缺少 type 字段")

    message_type = data["type"]
    cls = MESSAGE_TYPES.get(message_type)
    if cls is None:
        raise SignalingError(f"未知信令消息类型: {message_type}")

    payload = data.get("payload") or {}
    if not isinstance(payload, dict):
        raise SignalingError("payload 必须是对象")

    try:
        return cls.from_payload(payload)
    except TypeError as exc:
        raise SignalingError(
            f"消息字段不匹配: {message_type}", detail=str(exc)
        ) from exc


def is_control_message(message: Message) -> bool:
    """控制类消息（不携带媒体协商内容）。"""
    return isinstance(message, (Hello, Join, Leave, Bye, Ping, Pong, ErrorMessage))
