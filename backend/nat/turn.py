"""TURN（RFC 5766）最小客户端。

只在 ICE 直连与打洞都失败时才用中继，所以这里只实现必要动作：

1. ``Allocate``：申请中继地址（先收 401 挑战，再用长期凭证重发）；
2. ``CreatePermission``：允许某个对端 IP 通过中继；
3. ``Refresh``：续租（lifetime 到期前刷新）。

未实现 ChannelData（那只是省 4 字节负载头的优化）。所有请求都带
MESSAGE-INTEGRITY，且 nonce 随响应更新——少了这两点服务器会一直 401。
"""

from __future__ import annotations

import socket
import struct
import time

from backend.errors.network import NetworkError
from backend.logging.logger import get_logger
from backend.nat.stun import (
    ATTR_ERROR_CODE,
    ATTR_LIFETIME,
    ATTR_MESSAGE_INTEGRITY,
    ATTR_NONCE,
    ATTR_REALM,
    ATTR_REQUESTED_TRANSPORT,
    ATTR_USERNAME,
    ATTR_XOR_PEER_ADDRESS,
    ATTR_XOR_RELAYED_ADDRESS,
    CLASS_ERROR,
    CLASS_SUCCESS,
    METHOD_ALLOCATE,
    METHOD_CREATE_PERMISSION,
    METHOD_REFRESH,
    StunError,
    StunMessage,
    long_term_key,
    new_transaction_id,
    parse_address,
    xor_address,
)

logger = get_logger(__name__)


class TurnError(NetworkError):
    pass


class TurnClient:
    """一对 UDP 套接字与 TURN 服务器之间的会话。"""

    def __init__(
        self,
        server: tuple[str, int],
        username: str = "",
        password: str = "",
        realm: str = "",
        timeout_s: float = 1.0,
        retries: int = 3,
    ) -> None:
        self.server = server
        self.username = username
        self.password = password
        self.realm = realm
        self.timeout_s = timeout_s
        self.retries = retries
        self.nonce: bytes | None = None
        self.relayed: tuple[str, int] | None = None
        self.lifetime_s = 600
        self._socket: socket.socket | None = None

    # ---- 生命周期 ----
    def open(self) -> tuple[str, int]:
        """申请中继地址，返回 ``(地址, 端口)``。"""
        if self._socket is None:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._socket.bind(("", 0))

        response = self._authenticated_request(METHOD_ALLOCATE, self._allocate_attributes())
        relayed = response.get(ATTR_XOR_RELAYED_ADDRESS)
        if relayed is None:
            raise TurnError("Allocate 响应缺少 XOR-RELAYED-ADDRESS")

        self.relayed = parse_address(relayed, response.transaction_id, xored=True)
        lifetime = response.get(ATTR_LIFETIME)
        if lifetime:
            self.lifetime_s = struct.unpack("!I", lifetime[:4])[0]
        logger.info(
            "TURN 中继就绪: %s:%d (lifetime=%ds)", self.relayed[0], self.relayed[1], self.lifetime_s
        )
        return self.relayed

    def close(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    # ---- 动作 ----
    def create_permission(self, peer_ip: str) -> None:
        self._authenticated_request(
            METHOD_CREATE_PERMISSION,
            [(ATTR_XOR_PEER_ADDRESS, xor_address(peer_ip, 0, new_transaction_id()))],
        )

    def refresh(self) -> None:
        self._authenticated_request(METHOD_REFRESH, [(ATTR_LIFETIME, struct.pack("!I", self.lifetime_s))])

    # ---- 内部 ----
    def _allocate_attributes(self) -> list[tuple[int, bytes]]:
        # REQUESTED-TRANSPORT: UDP = 17
        return [(ATTR_REQUESTED_TRANSPORT, bytes([17, 0, 0, 0]))]

    def _build(
        self,
        method: int,
        attributes: list[tuple[int, bytes]],
        *,
        with_auth: bool,
    ) -> StunMessage:
        message = StunMessage(
            method=method, message_class=0x000, transaction_id=new_transaction_id()
        )
        if with_auth:
            message.add(ATTR_USERNAME, self.username.encode("utf-8"))
            if self.realm:
                message.add(ATTR_REALM, self.realm.encode("utf-8"))
            if self.nonce:
                message.add(ATTR_NONCE, self.nonce)
        for key, value in attributes:
            message.add(key, value)
        return message

    def _send(self, message: StunMessage, *, with_auth: bool) -> StunMessage:
        assert self._socket is not None
        key = long_term_key(self.username, self.realm, self.password) if with_auth else None
        payload = message.pack(integrity_key=key)

        for attempt in range(self.retries):
            self._socket.settimeout(self.timeout_s * (2**attempt))
            self._socket.sendto(payload, self.server)
            began = time.perf_counter()
            while True:
                remaining = self.timeout_s * (2**attempt) - (time.perf_counter() - began)
                if remaining <= 0:
                    break
                self._socket.settimeout(remaining)
                try:
                    data, _addr = self._socket.recvfrom(4096)
                except socket.timeout:
                    break
                try:
                    response = StunMessage.unpack(data)
                except StunError:
                    continue
                if response.transaction_id != message.transaction_id:
                    continue
                # 401/438：服务器要求认证或 nonce 过期，更新后重发
                if response.message_class == CLASS_ERROR:
                    error = response.error()
                    code = error[0] if error else 0
                    if code in (401, 438):
                        realm = response.get(ATTR_REALM)
                        nonce = response.get(ATTR_NONCE)
                        if realm:
                            self.realm = realm.decode("utf-8", "replace")
                        if nonce:
                            self.nonce = nonce
                        raise _AuthRequired()
                    raise TurnError(f"TURN 错误响应: {error}")
                return response
        raise TurnError(f"TURN 请求超时: method=0x{message.method:X}")

    def _authenticated_request(
        self, method: int, attributes: list[tuple[int, bytes]]
    ) -> StunMessage:
        try:
            return self._send(self._build(method, attributes, with_auth=False), with_auth=False)
        except _AuthRequired:
            pass
        except TurnError:
            raise

        for _ in range(2):
            try:
                response = self._send(self._build(method, attributes, with_auth=True), with_auth=True)
            except _AuthRequired:
                continue
            if response.message_class != CLASS_SUCCESS:
                raise TurnError("TURN 响应类型异常")
            return response
        raise TurnError("TURN 认证失败（检查 username/password）")


class _AuthRequired(Exception):
    """内部信号：需要带长期凭证重发。"""


def discover_relay(
    server: tuple[str, int], username: str, password: str, timeout_s: float = 1.0
) -> tuple[str, int] | None:
    """申请一次中继地址；失败返回 None（调用方回退到「无中继」）。"""
    client = TurnClient(server, username, password, timeout_s=timeout_s)
    try:
        client.open()
        return client.relayed
    except (TurnError, StunError, OSError) as exc:
        logger.debug("TURN 中继申请失败: %s", exc)
        return None
    finally:
        client.close()


__all__ = ["TurnClient", "TurnError", "discover_relay", "ATTR_MESSAGE_INTEGRITY"]
