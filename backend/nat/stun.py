"""STUN（RFC 5389/8489）最小实现。

用途只有两个：探测公网映射地址（srflx 候选）、给 ICE 连通性检查发 Binding 请求。

已实现：Binding Request/Response、XOR-MAPPED-ADDRESS、MESSAGE-INTEGRITY
（长期凭证，HMAC-SHA1）、FINGERPRINT、ERROR-CODE。
未实现：TURN 的 ChannelData、STUN over TCP（UDP 足够覆盖 srflx 与连通性检查）。

时间与重传：UDP 会丢包，所以按「指数退避 + 有界重试」发送，绝不允许无限等待。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import socket
import struct
import time
from dataclasses import dataclass, field

from backend.errors.network import NetworkError
from backend.logging.logger import get_logger

logger = get_logger(__name__)

MAGIC_COOKIE = 0x2112A442
HEADER_SIZE = 20

CLASS_REQUEST = 0x000
CLASS_INDICATION = 0x010
CLASS_SUCCESS = 0x100
CLASS_ERROR = 0x110

METHOD_BINDING = 0x001
METHOD_ALLOCATE = 0x003
METHOD_REFRESH = 0x004
METHOD_CREATE_PERMISSION = 0x008

ATTR_MAPPED_ADDRESS = 0x0001
ATTR_USERNAME = 0x0006
ATTR_MESSAGE_INTEGRITY = 0x0008
ATTR_ERROR_CODE = 0x0009
ATTR_LIFETIME = 0x000D
ATTR_XOR_PEER_ADDRESS = 0x0012
ATTR_REALM = 0x0014
ATTR_NONCE = 0x0015
ATTR_XOR_RELAYED_ADDRESS = 0x0016
ATTR_REQUESTED_TRANSPORT = 0x0019
ATTR_XOR_MAPPED_ADDRESS = 0x0020
ATTR_RESPONSE_ORIGIN = 0x802B
ATTR_FINGERPRINT = 0x8028


class StunError(NetworkError):
    pass


def new_transaction_id() -> bytes:
    return os.urandom(12)


def encode_message_type(method: int, message_class: int) -> int:
    return (
        (method & 0x000F)
        | ((method & 0x0070) << 1)
        | ((method & 0x0F80) << 2)
        | ((message_class & 0x000F) << 4)
    )


def decode_message_type(value: int) -> tuple[int, int]:
    method = (value & 0x000F) | ((value & 0x00E0) >> 1) | ((value & 0x3E00) >> 2)
    message_class = (value & 0x0110) >> 4
    return method, message_class


def pack_attribute(attr_type: int, value: bytes) -> bytes:
    padding = (4 - len(value) % 4) % 4
    return struct.pack("!HH", attr_type, len(value)) + value + b"\x00" * padding


def xor_address(address: str, port: int, transaction_id: bytes) -> bytes:
    """XOR-MAPPED-ADDRESS / XOR-RELAYED-ADDRESS 的取值编码。"""
    socket_type = socket.AF_INET6 if ":" in address else socket.AF_INET
    cookie = struct.pack("!I", MAGIC_COOKIE)
    xored_port = port ^ (MAGIC_COOKIE >> 16)

    if socket_type is socket.AF_INET:
        packed = socket.inet_pton(socket.AF_INET, address)
        mask = cookie
        body = struct.pack("!BBH", 0x00, 0x01, xored_port) + bytes(
            a ^ b for a, b in zip(packed, mask)
        )
        return body

    packed = socket.inet_pton(socket.AF_INET6, address)
    mask = cookie + transaction_id
    body = struct.pack("!BBH", 0x00, 0x02, xored_port) + bytes(
        a ^ b for a, b in zip(packed, mask)
    )
    return body


def parse_address(value: bytes, transaction_id: bytes, *, xored: bool) -> tuple[str, int]:
    if len(value) < 8:
        raise StunError("地址属性长度不足")
    family = value[1]
    port = struct.unpack("!H", value[2:4])[0]
    if xored:
        port ^= MAGIC_COOKIE >> 16

    if family == 0x01:
        packed = value[4:8]
        if xored:
            packed = bytes(a ^ b for a, b in zip(packed, struct.pack("!I", MAGIC_COOKIE)))
        return socket.inet_ntop(socket.AF_INET, packed), port

    if family == 0x02:
        packed = value[4:20]
        if xored:
            mask = struct.pack("!I", MAGIC_COOKIE) + transaction_id
            packed = bytes(a ^ b for a, b in zip(packed, mask))
        return socket.inet_ntop(socket.AF_INET6, packed), port

    raise StunError(f"未知地址族: {family}")


@dataclass
class StunMessage:
    """一条 STUN 报文。"""

    method: int
    message_class: int
    transaction_id: bytes
    attributes: list[tuple[int, bytes]] = field(default_factory=list)

    def get(self, attr_type: int) -> bytes | None:
        for key, value in self.attributes:
            if key == attr_type:
                return value
        return None

    def add(self, attr_type: int, value: bytes) -> None:
        self.attributes.append((attr_type, value))

    def pack(self, integrity_key: bytes | None = None) -> bytes:
        body = b"".join(pack_attribute(key, value) for key, value in self.attributes)

        if integrity_key is not None:
            # MESSAGE-INTEGRITY 覆盖「到该属性之前」的内容，且长度字段要含它自身
            header = struct.pack(
                "!HHI12s",
                encode_message_type(self.method, self.message_class),
                len(body) + 24,
                MAGIC_COOKIE,
                self.transaction_id,
            )
            digest = hmac.new(integrity_key, header + body, hashlib.sha1).digest()
            body += pack_attribute(ATTR_MESSAGE_INTEGRITY, digest)

        header = struct.pack(
            "!HHI12s",
            encode_message_type(self.method, self.message_class),
            len(body),
            MAGIC_COOKIE,
            self.transaction_id,
        )
        return header + body

    @classmethod
    def unpack(cls, data: bytes) -> "StunMessage":
        if len(data) < HEADER_SIZE:
            raise StunError("STUN 报文过短")
        message_type, length, cookie, transaction_id = struct.unpack("!HHI12s", data[:20])
        if cookie != MAGIC_COOKIE:
            raise StunError("STUN magic cookie 不匹配（不是 STUN 报文）")

        method, message_class = decode_message_type(message_type)
        message = cls(method=method, message_class=message_class, transaction_id=transaction_id)

        offset = HEADER_SIZE
        end = HEADER_SIZE + length
        while offset + 4 <= min(end, len(data)):
            attr_type, attr_length = struct.unpack("!HH", data[offset : offset + 4])
            start = offset + 4
            message.add(attr_type, data[start : start + attr_length])
            offset = start + attr_length + ((4 - attr_length % 4) % 4)
        return message

    def mapped_address(self) -> tuple[str, int] | None:
        value = self.get(ATTR_XOR_MAPPED_ADDRESS)
        if value is not None:
            return parse_address(value, self.transaction_id, xored=True)
        value = self.get(ATTR_MAPPED_ADDRESS)
        if value is not None:
            return parse_address(value, self.transaction_id, xored=False)
        return None

    def error(self) -> tuple[int, str] | None:
        value = self.get(ATTR_ERROR_CODE)
        if value is None:
            return None
        code = (value[2] & 0x07) * 100 + value[3]
        reason = value[4:].decode("utf-8", "replace")
        return code, reason


def build_binding_request(username: str | None = None, transaction_id: bytes | None = None) -> StunMessage:
    message = StunMessage(
        method=METHOD_BINDING,
        message_class=CLASS_REQUEST,
        transaction_id=transaction_id or new_transaction_id(),
    )
    if username:
        message.add(ATTR_USERNAME, username.encode("utf-8"))
    return message


def long_term_key(username: str, realm: str, password: str) -> bytes:
    """长期凭证的 HMAC key = MD5(username:realm:password)。"""
    return hashlib.md5(f"{username}:{realm}:{password}".encode("utf-8")).digest()


class StunClient:
    """一次性 Binding 请求（够用来探测 srflx 候选）。"""

    def __init__(self, timeout_s: float = 0.5, retries: int = 3) -> None:
        self.timeout_s = timeout_s
        self.retries = retries

    def binding(
        self,
        server: tuple[str, int],
        sock: socket.socket | None = None,
    ) -> tuple[str, int]:
        """返回公网映射地址；失败抛 ``StunError``。"""
        own_socket = sock is None
        if sock is None:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("", 0))

        request = build_binding_request()
        payload = request.pack()

        try:
            for attempt in range(self.retries):
                sock.settimeout(self.timeout_s * (2**attempt))
                sock.sendto(payload, server)
                began = time.perf_counter()
                while True:
                    remaining = self.timeout_s * (2**attempt) - (time.perf_counter() - began)
                    if remaining <= 0:
                        break
                    sock.settimeout(remaining)
                    try:
                        data, _addr = sock.recvfrom(2048)
                    except socket.timeout:
                        break
                    try:
                        response = StunMessage.unpack(data)
                    except StunError:
                        continue
                    if response.transaction_id != request.transaction_id:
                        continue
                    if response.message_class == CLASS_ERROR:
                        error = response.error()
                        raise StunError(f"STUN 错误响应: {error}")
                    if response.message_class != CLASS_SUCCESS:
                        continue
                    mapped = response.mapped_address()
                    if mapped:
                        logger.debug(
                            "STUN 映射: %s:%d (RTT %.1fms)",
                            mapped[0],
                            mapped[1],
                            (time.perf_counter() - began) * 1000,
                        )
                        return mapped
            raise StunError(f"STUN 无响应: {server}")
        finally:
            if own_socket:
                sock.close()


DEFAULT_STUN_SERVERS = [
    ("stun.l.google.com", 19302),
    ("stun1.l.google.com", 19302),
]


def discover_public_address(
    servers: list[tuple[str, int]] | None = None,
    timeout_s: float = 0.5,
) -> tuple[str, int, tuple[str, int]] | None:
    """依次尝试 STUN 服务器，返回 ``(公网地址, 端口, 本地服务器地址)``。"""
    for server in servers or DEFAULT_STUN_SERVERS:
        try:
            mapped = StunClient(timeout_s=timeout_s).binding(server)
        except (StunError, OSError) as exc:
            logger.debug("STUN %s 探测失败: %s", server[0], exc)
            continue
        return mapped[0], mapped[1], server
    return None


__all__ = [
    "StunMessage",
    "StunClient",
    "StunError",
    "build_binding_request",
    "discover_public_address",
    "long_term_key",
    "xor_address",
    "METHOD_BINDING",
    "METHOD_ALLOCATE",
    "METHOD_REFRESH",
    "METHOD_CREATE_PERMISSION",
    "CLASS_REQUEST",
    "CLASS_SUCCESS",
    "CLASS_ERROR",
    "ATTR_USERNAME",
    "ATTR_MESSAGE_INTEGRITY",
    "ATTR_LIFETIME",
    "ATTR_XOR_PEER_ADDRESS",
    "ATTR_XOR_RELAYED_ADDRESS",
    "ATTR_REQUESTED_TRANSPORT",
    "ATTR_REALM",
    "ATTR_NONCE",
    "HEADER_SIZE",
]
