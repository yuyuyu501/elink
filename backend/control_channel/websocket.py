"""WebSocket 控制通道（RFC 6455 最小实现）。

为什么自己实现：控制通道的 WebSocket 只需要「二进制帧 + ping/pong + 关闭」，
引入第三方库对这样一个窄用途来说不划算，而且我们要在浏览器端复用同一份协议帧。

实现范围：客户端握手、掩码帧（客户端必须掩码）、分片重组、ping/pong、关闭。
不支持：扩展（permessage-deflate）、多子协议协商——用不到，也不必承担复杂度。
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import struct
import threading

from backend.control_channel import protocol
from backend.control_channel.base import ChannelState, ControlChannel
from backend.errors.network import NetworkError, TransportClosedError
from backend.event.bus import EventBus
from backend.logging.logger import get_logger

logger = get_logger(__name__)

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONTINUATION = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


def _mask(payload: bytes, mask_key: bytes) -> bytes:
    return bytes(byte ^ mask_key[index % 4] for index, byte in enumerate(payload))


def encode_frame(payload: bytes, *, opcode: int = OP_BINARY, masked: bool = True) -> bytes:
    header = bytearray([0x80 | opcode])
    length = len(payload)

    mask_bit = 0x80 if masked else 0x00
    if length < 126:
        header.append(mask_bit | length)
    elif length < (1 << 16):
        header.append(mask_bit | 126)
        header += struct.pack("!H", length)
    else:
        header.append(mask_bit | 127)
        header += struct.pack("!Q", length)

    if masked:
        mask_key = os.urandom(4)
        header += mask_key
        return bytes(header) + _mask(payload, mask_key)
    return bytes(header) + payload


class FrameReader:
    """从 socket 读 WebSocket 帧（处理分片与掩码）。"""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock

    def _recv(self, size: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < size:
            chunk = self.sock.recv(size - len(chunks))
            if not chunk:
                raise TransportClosedError("WebSocket 连接已关闭")
            chunks += chunk
        return bytes(chunks)

    def read(self) -> tuple[bool, int, bytes] | None:
        """读一帧，返回 ``(fin, opcode, payload)``；收到关闭帧返回 None。"""
        first, second = self._recv(2)
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F

        if length == 126:
            length = struct.unpack("!H", self._recv(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv(8))[0]

        mask_key = self._recv(4) if masked else b""
        payload = self._recv(length) if length else b""
        if masked:
            payload = _mask(payload, mask_key)

        if opcode == OP_CLOSE:
            return None
        return fin, opcode, payload


class WebSocketControlChannel(ControlChannel):
    kind = "websocket"

    def __init__(
        self,
        host: str,
        port: int,
        path: str = "/control",
        *,
        tls_context: ssl.SSLContext | None = None,
        timeout_s: float = 10.0,
        bus: EventBus | None = None,
    ) -> None:
        super().__init__(bus)
        self.host = host
        self.port = port
        self.path = path
        self.tls_context = tls_context
        self.timeout_s = timeout_s
        self._socket: socket.socket | None = None
        self._reader_thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._send_lock = threading.Lock()

    def open(self) -> None:
        self.state = ChannelState.CONNECTING
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout_s)
            if self.tls_context is not None:
                sock = self.tls_context.wrap_socket(sock, server_hostname=self.host)
        except OSError as exc:
            self.state = ChannelState.FAILED
            raise NetworkError(
                f"WebSocket 连接失败 {self.host}:{self.port}", detail=str(exc)
            ) from exc

        self._socket = sock
        try:
            self._handshake()
        except (OSError, NetworkError):
            sock.close()
            self._socket = None
            self.state = ChannelState.FAILED
            raise

        sock.settimeout(None)
        self.state = ChannelState.OPEN
        self._stopping.clear()
        self._reader_thread = threading.Thread(
            target=self._read_loop, name="ws-control-rx", daemon=True
        )
        self._reader_thread.start()
        logger.info("WebSocket 控制通道已建立: ws://%s:%d%s", self.host, self.port, self.path)

    def _handshake(self) -> None:
        assert self._socket is not None
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Sec-WebSocket-Protocol: elink-control\r\n"
            "\r\n"
        )
        self._socket.sendall(request.encode())

        response = bytearray()
        while b"\r\n\r\n" not in response:
            chunk = self._socket.recv(1024)
            if not chunk:
                raise NetworkError("WebSocket 握手被拒绝")
            response += chunk
            if len(response) > 8192:
                raise NetworkError("WebSocket 握手响应异常")

        header_text = response.decode("latin-1")
        if "101" not in header_text.splitlines()[0]:
            raise NetworkError("WebSocket 握手失败", detail=header_text.splitlines()[0])

        expected = base64.b64encode(
            hashlib.sha1((key + GUID).encode()).digest()
        ).decode()
        if f"sec-websocket-accept: {expected}".lower() not in header_text.lower():
            raise NetworkError("WebSocket Accept 校验失败")

    def close(self) -> None:
        self._stopping.set()
        if self._socket is not None:
            try:
                self._socket.sendall(encode_frame(b"", opcode=OP_CLOSE))
            except OSError:
                pass
            self._socket.close()
            self._socket = None
        self.state = ChannelState.CLOSED

    def _write(self, data: bytes) -> None:
        if self._socket is None:
            raise TransportClosedError("WebSocket 未连接")
        with self._send_lock:
            try:
                self._socket.sendall(encode_frame(data, opcode=OP_BINARY))
            except OSError as exc:
                self.state = ChannelState.FAILED
                raise TransportClosedError("WebSocket 发送失败", detail=str(exc)) from exc

    def _read_loop(self) -> None:  # noqa: C901 - 分片与心跳分支就在这里，拆开反而难读
        assert self._socket is not None
        reader = FrameReader(self._socket)
        fragment = bytearray()
        # WS 的 message 边界不保证等于协议帧边界（也可能一个 message 里塞多帧），
        # 所以必须再过一遍 protocol.FrameDecoder，不能直接把 message 当 JSON 负载
        decoder = protocol.FrameDecoder()

        try:
            while not self._stopping.is_set():
                frame = reader.read()
                if frame is None:
                    break
                fin, opcode, payload = frame

                if opcode == OP_PING:
                    with self._send_lock:
                        self._socket.sendall(encode_frame(payload, opcode=OP_PONG))
                    continue
                if opcode == OP_PONG:
                    continue

                if opcode in (OP_BINARY, OP_TEXT):
                    fragment = bytearray(payload)
                elif opcode == OP_CONTINUATION:
                    fragment += payload
                else:
                    continue

                if fin and fragment:
                    for frame_type, data in decoder.push(bytes(fragment)):
                        if frame_type is protocol.FrameType.JSON:
                            self._dispatch(data)
                    fragment = bytearray()
        except (OSError, TransportClosedError) as exc:
            logger.debug("WebSocket 读循环结束: %s", exc)
        finally:
            self.state = ChannelState.CLOSED

def build_accept_key(client_key: str) -> str:
    """按 RFC 6455 计算 ``Sec-WebSocket-Accept``。"""
    return base64.b64encode(hashlib.sha1((client_key + GUID).encode()).digest()).decode()


class WebSocketServerChannel(ControlChannel):
    """服务端一侧的 WebSocket 控制通道（浏览器 / 移动端用）。

    与客户端通道的差别只有两点：握手方向相反、发出的帧**不能掩码**
    （浏览器发来的帧则一定掩码，由 :class:`FrameReader` 处理）。
    """

    kind = "websocket-server"

    def __init__(self, bus: EventBus | None = None) -> None:
        super().__init__(bus)
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._send_lock = threading.Lock()
        self.peer = ""

    def accept(self, sock: socket.socket, client_key: str, *, peer: str = "") -> None:
        """完成握手并把连接转入读循环。"""
        if not client_key:
            raise NetworkError("WebSocket 缺少 Sec-WebSocket-Key")
        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {build_accept_key(client_key)}\r\n"
            "Sec-WebSocket-Protocol: elink-control\r\n"
            "\r\n"
        )
        sock.sendall(response.encode("ascii"))
        sock.settimeout(None)

        self._socket = sock
        self.peer = peer
        self.state = ChannelState.OPEN
        self._stopping.clear()
        self._thread = threading.Thread(target=self._read_loop, name="ws-server-rx", daemon=True)
        self._thread.start()
        logger.info("WebSocket 控制通道已接入: %s", peer or "<unknown>")

    # 未握手时调用 open() 没有意义，这里显式拒绝，避免静默状态错乱
    def open(self) -> None:
        raise NetworkError("服务端通道请用 accept(sock, key) 建立连接")

    def close(self) -> None:
        self._stopping.set()
        if self._socket is not None:
            try:
                self._socket.sendall(encode_frame(b"", opcode=OP_CLOSE, masked=False))
            except OSError:
                pass
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None
        self.state = ChannelState.CLOSED

    def _write(self, data: bytes) -> None:
        if self._socket is None:
            raise TransportClosedError("WebSocket 服务端通道未连接")
        with self._send_lock:
            try:
                self._socket.sendall(encode_frame(data, opcode=OP_BINARY, masked=False))
            except OSError as exc:
                self.state = ChannelState.FAILED
                raise TransportClosedError("WebSocket 发送失败", detail=str(exc)) from exc

    def _read_loop(self) -> None:
        assert self._socket is not None
        reader = FrameReader(self._socket)
        fragment = bytearray()
        decoder = protocol.FrameDecoder()

        try:
            while not self._stopping.is_set():
                frame = reader.read()
                if frame is None:
                    break
                fin, opcode, payload = frame

                if opcode == OP_PING:
                    with self._send_lock:
                        self._socket.sendall(encode_frame(payload, opcode=OP_PONG, masked=False))
                    continue
                if opcode == OP_PONG:
                    continue

                if opcode in (OP_BINARY, OP_TEXT):
                    fragment = bytearray(payload)
                elif opcode == OP_CONTINUATION:
                    fragment += payload
                else:
                    continue

                if fin and fragment:
                    for frame_type, data in decoder.push(bytes(fragment)):
                        if frame_type is protocol.FrameType.JSON:
                            self._dispatch(data)
                    fragment = bytearray()
        except (OSError, TransportClosedError) as exc:
            logger.debug("WebSocket 服务端读循环结束: %s", exc)
        finally:
            self.state = ChannelState.CLOSED
            # 读循环结束意味着对端已断开：这里要真正关掉 socket，
            # 否则文件描述符会一直挂着（服务端长时间运行会泄漏）
            if not self._stopping.is_set():
                try:
                    self._socket.close()
                except OSError:
                    pass
