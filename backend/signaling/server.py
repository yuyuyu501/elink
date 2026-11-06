"""信令服务端。

职责边界：只做「转发 + 鉴权 + 房间管理」，不解析 SDP 内容、不做媒体处理。
转发规则是「同房间内除发送者之外全部转发」，这样 1 对 1 与 1 对多共用一套逻辑。
"""

from __future__ import annotations

import socket
import socketserver
import ssl
import struct
import threading
from dataclasses import dataclass, field

from backend.errors.network import NetworkError
from backend.logging.logger import get_logger
from backend.security.auth import AuthError, Authenticator
from backend.signaling import messages as msg

logger = get_logger(__name__)

MAX_FRAME = 8 * 1024 * 1024


@dataclass
class Peer:
    """一条已连接的信令客户端。"""

    peer_id: str
    sock: socket.socket
    session_id: str = ""
    role: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock)

    def send(self, message: msg.Message) -> bool:
        payload = msg.encode(message).encode("utf-8")
        frame = struct.pack("!I", len(payload)) + payload
        with self.lock:
            try:
                self.sock.sendall(frame)
                return True
            except OSError:
                return False

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


class SignalingServer:
    """基于 ``socketserver.ThreadingTCPServer`` 的最小信令服务。"""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8443,
        *,
        authenticator: Authenticator | None = None,
        tls_context: ssl.SSLContext | None = None,
        require_join_auth: bool = True,
    ) -> None:
        self.host = host
        self.port = port
        self.authenticator = authenticator
        self.tls_context = tls_context
        self.require_join_auth = require_join_auth

        self._peers: dict[str, Peer] = {}
        self._lock = threading.RLock()
        self._server: socketserver.ThreadingTCPServer | None = None

    # ---- 生命周期 ----
    def start(self) -> tuple[str, int]:
        handler = _make_handler(self)
        server = socketserver.ThreadingTCPServer((self.host, self.port), handler)
        server.daemon_threads = True
        if self.tls_context is not None:
            server.socket = self.tls_context.wrap_socket(server.socket, server_side=True)
        self._server = server
        thread = threading.Thread(target=server.serve_forever, name="signaling-server", daemon=True)
        thread.start()
        address = server.server_address
        logger.info("信令服务已启动: %s:%d (TLS=%s)", address[0], address[1], bool(self.tls_context))
        return address[0], address[1]

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        with self._lock:
            for peer in list(self._peers.values()):
                peer.close()
            self._peers.clear()
        logger.info("信令服务已停止")

    # ---- 房间与转发 ----
    def register(self, peer: Peer) -> None:
        with self._lock:
            self._peers[peer.peer_id] = peer

    def unregister(self, peer: Peer) -> None:
        with self._lock:
            self._peers.pop(peer.peer_id, None)

    def peers_in(self, session_id: str) -> list[Peer]:
        with self._lock:
            return [peer for peer in self._peers.values() if peer.session_id == session_id]

    def relay(self, sender: Peer, message: msg.Message, *, include_sender: bool = False) -> int:
        if not sender.session_id:
            sender.send(msg.ErrorMessage(code="not_joined", message="请先 Join"))
            return 0

        sent = 0
        for peer in self.peers_in(sender.session_id):
            if peer is sender and not include_sender:
                continue
            if peer.send(message):
                sent += 1
        logger.debug("转发 %s 到 %d 个对端", message.type, sent)
        return sent

    # ---- 消息处理 ----
    def handle(self, peer: Peer, message: msg.Message) -> None:
        if isinstance(message, msg.Hello):
            peer.role = message.role
            peer.send(msg.Hello(client_id=peer.peer_id, role="server"))
            return

        if isinstance(message, msg.Join):
            if self.require_join_auth and self.authenticator is not None:
                try:
                    claims = self.authenticator.authenticate(message.token)
                except AuthError as exc:
                    peer.send(msg.ErrorMessage(code="auth_failed", message=str(exc), fatal=True))
                    return
                logger.info("对端 %s 认证通过: %s", peer.peer_id, claims.subject)

            peer.session_id = message.session_id or "default"
            self.register(peer)
            peer.send(msg.Join(session_id=peer.session_id))
            for other in self.peers_in(peer.session_id):
                if other is not peer:
                    other.send(msg.Hello(client_id=peer.peer_id, role=peer.role))
            return

        if isinstance(message, msg.Leave):
            self.unregister(peer)
            peer.session_id = ""
            return

        if isinstance(message, msg.Ping):
            peer.send(msg.Pong(nonce=message.nonce))
            return

        if isinstance(message, (msg.Offer, msg.Answer, msg.IceCandidate, msg.CapabilityOffer, msg.CapabilityAnswer)):
            self.relay(peer, message)
            return

        if isinstance(message, msg.Bye):
            self.relay(peer, message)
            self.unregister(peer)
            return

        peer.send(msg.ErrorMessage(code="unsupported", message=f"未处理的消息类型: {message.type}"))


def _make_handler(server: SignalingServer):
    class Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:  # pragma: no cover - 网络回调
            sock: socket.socket = self.request
            peer = Peer(peer_id=f"{self.client_address[0]}:{self.client_address[1]}", sock=sock)
            server.register(peer)
            logger.debug("信令对端接入: %s", peer.peer_id)
            try:
                while True:
                    header = _recv_exact(sock, 4)
                    if header is None:
                        break
                    (length,) = struct.unpack("!I", header)
                    if length == 0 or length > MAX_FRAME:
                        raise NetworkError("非法帧长度", detail=str(length))
                    payload = _recv_exact(sock, length)
                    if payload is None:
                        break
                    try:
                        message = msg.decode(payload)
                    except msg.SignalingError as exc:
                        peer.send(msg.ErrorMessage(code="bad_message", message=str(exc)))
                        continue
                    server.handle(peer, message)
            except (OSError, NetworkError) as exc:
                logger.debug("信令对端断开 %s: %s", peer.peer_id, exc)
            finally:
                server.unregister(peer)
                try:
                    sock.close()
                except OSError:
                    pass

    return Handler


def _recv_exact(sock: socket.socket, size: int) -> bytes | None:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sock.recv(size - len(chunks))
        if not chunk:
            return None
        chunks += chunk
    return bytes(chunks)


def create_authenticator(secret: str, *, allow_anonymous: bool = False) -> Authenticator:
    """便捷构造：直接用共享密钥做令牌校验。"""
    from backend.security.auth import TokenSigner

    return Authenticator(TokenSigner(secret), allow_anonymous=allow_anonymous)


__all__ = ["SignalingServer", "Peer", "create_authenticator"]
