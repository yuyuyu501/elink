"""信令客户端。

只负责「与服务端交换 offer/answer/candidate」，不做 SDP 文本处理（那是
``sdp.py`` 的事），也不做重连决策（那是 ``session/reconnect.py`` 的事）。

帧格式：``4 字节大端长度 + JSON``。选长度前缀而不是 WebSocket，是因为信令
是自建服务，长连接 + 定长头最省心；WebSocket 留给浏览器客户端（见
``control_channel/websocket.py``）。
"""

from __future__ import annotations

import socket
import ssl
import struct
import threading
from collections.abc import Callable

from backend.errors.network import NetworkError, TransportClosedError, TransportTimeoutError
from backend.logging.logger import get_logger
from backend.signaling import messages as msg

logger = get_logger(__name__)

MAX_FRAME = 8 * 1024 * 1024


class SignalingClient:
    """线程安全的信令客户端（后台线程收包，回调在收包线程里触发）。"""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        tls_context: ssl.SSLContext | None = None,
        server_hostname: str | None = None,
        timeout_s: float = 10.0,
        on_message: Callable[[msg.Message], None] | None = None,
        on_disconnect: Callable[[str], None] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.tls_context = tls_context
        self.server_hostname = server_hostname or host
        self.timeout_s = timeout_s
        self.on_message = on_message
        self.on_disconnect = on_disconnect

        self._socket: socket.socket | None = None
        self._reader: threading.Thread | None = None
        self._send_lock = threading.Lock()
        self._closed = threading.Event()
        self._pending: dict[str, threading.Event] = {}
        self._received: dict[str, msg.Message] = {}

    # ---- 生命周期 ----
    def connect(self) -> None:
        try:
            raw = socket.create_connection((self.host, self.port), timeout=self.timeout_s)
        except OSError as exc:
            raise NetworkError(
                f"无法连接信令服务 {self.host}:{self.port}", detail=str(exc)
            ) from exc

        if self.tls_context is not None:
            raw = self.tls_context.wrap_socket(raw, server_hostname=self.server_hostname)

        raw.settimeout(None)
        self._socket = raw
        self._closed.clear()
        self._reader = threading.Thread(target=self._read_loop, name="signaling-client", daemon=True)
        self._reader.start()
        logger.info("信令已连接: %s:%d", self.host, self.port)

    def close(self, reason: str = "client closed") -> None:
        self._closed.set()
        if self._socket is not None:
            try:
                self._socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._socket.close()
            self._socket = None
        if self.on_disconnect is not None:
            self.on_disconnect(reason)

    @property
    def connected(self) -> bool:
        return self._socket is not None and not self._closed.is_set()

    # ---- 发送 ----
    def send(self, message: msg.Message) -> None:
        if self._socket is None:
            raise TransportClosedError("信令通道未连接")
        payload = msg.encode(message).encode("utf-8")
        if len(payload) > MAX_FRAME:
            raise NetworkError("信令消息过大", detail=f"{len(payload)} 字节")
        frame = struct.pack("!I", len(payload)) + payload
        with self._send_lock:
            try:
                self._socket.sendall(frame)
            except OSError as exc:
                raise TransportClosedError("信令发送失败", detail=str(exc)) from exc
        logger.debug("信令发出: %s", message.type)

    def request(self, message: msg.Message, expect: type[msg.Message], timeout_s: float = 10.0):
        """发一条消息并等指定类型的应答（同步语义，便于写线性流程）。"""
        event = threading.Event()
        self._pending[expect.__name__] = event
        self.send(message)
        if not event.wait(timeout_s):
            self._pending.pop(expect.__name__, None)
            raise TransportTimeoutError(f"等待 {expect.__name__} 超时")
        self._pending.pop(expect.__name__, None)
        return self._received.pop(expect.__name__, None)

    # ---- 接收 ----
    def _read_loop(self) -> None:
        assert self._socket is not None
        try:
            while not self._closed.is_set():
                header = self._recv_exact(4)
                if header is None:
                    break
                (length,) = struct.unpack("!I", header)
                if length == 0 or length > MAX_FRAME:
                    raise NetworkError("非法信令帧长度", detail=str(length))
                payload = self._recv_exact(length)
                if payload is None:
                    break
                try:
                    message = msg.decode(payload)
                except msg.SignalingError:
                    logger.exception("无法解析信令消息，已忽略")
                    continue
                self._dispatch(message)
        except (OSError, NetworkError) as exc:
            logger.debug("信令读循环结束: %s", exc)
        finally:
            reason = "remote closed" if not self._closed.is_set() else "client closed"
            self._closed.set()
            if self._socket is not None:
                self._socket.close()
                self._socket = None
            if self.on_disconnect is not None:
                self.on_disconnect(reason)

    def _dispatch(self, message: msg.Message) -> None:
        event = self._pending.get(message.type)
        if event is not None:
            self._received[message.type] = message
            event.set()
        if self.on_message is not None:
            self.on_message(message)

    def _recv_exact(self, size: int) -> bytes | None:
        assert self._socket is not None
        chunks = bytearray()
        while len(chunks) < size:
            chunk = self._socket.recv(size - len(chunks))
            if not chunk:
                return None if not chunks else bytes(chunks).ljust(size, b"\x00")
            chunks += chunk
        return bytes(chunks)
