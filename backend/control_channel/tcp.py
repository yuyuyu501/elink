"""TCP 控制通道。

TCP 是控制通道最直接的承载：可靠、保序、无需额外封装。代价是「没有应用层
心跳就会被中间设备静默断开」，所以这里内置 ping/pong 保活，并把超时视为断链。

发送侧用独立线程 + 无界队列（控制事件不丢）；一旦写失败即标记通道失败，
让上层走重连（``session/reconnect.py``）。
"""

from __future__ import annotations

import queue
import socket
import ssl
import threading
import time
from dataclasses import replace

from backend.control_channel import protocol
from backend.control_channel.base import ChannelState, ControlChannel
from backend.errors.network import TransportClosedError
from backend.event.bus import EventBus
from backend.logging.logger import get_logger

logger = get_logger(__name__)


class _ControlQueue(queue.Queue):
    """Only adjacent motion in the same capture epoch may be combined."""

    def _put(self, item):
        if (isinstance(item, protocol.InputEvent)
                and item.event_type is protocol.EventType.MOUSE_MOVE and self.queue):
            previous = self.queue[-1]
            if (isinstance(previous, protocol.InputEvent)
                    and previous.event_type is protocol.EventType.MOUSE_MOVE
                    and previous.extra == item.extra):
                if item.extra.get("coordinates") == "relative":
                    x, y = previous.x + item.x, previous.y + item.y
                    if -32767 <= x <= 32767 and -32767 <= y <= 32767:
                        self.queue[-1] = replace(item, x=x, y=y)
                        return
                elif item.extra.get("coordinates") == "normalized":
                    self.queue[-1] = item
                    return
        super()._put(item)


class TcpControlChannel(ControlChannel):
    kind = "tcp"

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 7000,
        *,
        server_side: bool = False,
        tls_context: ssl.SSLContext | None = None,
        timeout_s: float = 10.0,
        heartbeat_s: float = 5.0,
        bus: EventBus | None = None,
    ) -> None:
        super().__init__(bus)
        self.host = host
        self.port = port
        self.server_side = server_side
        self.tls_context = tls_context
        self.timeout_s = timeout_s
        self.heartbeat_s = heartbeat_s

        self._socket: socket.socket | None = None
        self._listener: socket.socket | None = None
        self._reader: threading.Thread | None = None
        self._writer: threading.Thread | None = None
        self._outbox = _ControlQueue()
        self._stopping = threading.Event()
        self._connected = threading.Event()

    # ---- 生命周期 ----
    def open(self) -> None:
        self.state = ChannelState.CONNECTING
        try:
            if self.server_side:
                self._open_server()
            else:
                self._socket = socket.create_connection((self.host, self.port), timeout=self.timeout_s)
                if self.tls_context is not None:
                    self._socket = self.tls_context.wrap_socket(
                        self._socket, server_hostname=self.host
                    )
        except OSError as exc:
            self.state = ChannelState.FAILED
            raise TransportClosedError(
                f"TCP 控制通道连接失败 {self.host}:{self.port}", detail=str(exc)
            ) from exc

        if self._stopping.is_set():
            self.close()
            raise TransportClosedError("控制通道已停止")
        self._socket.settimeout(None)
        self._socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.state = ChannelState.OPEN
        self._connected.set()
        self._reader = threading.Thread(target=self._read_loop, name="tcp-control-rx", daemon=True)
        self._writer = threading.Thread(target=self._write_loop, name="tcp-control-tx", daemon=True)
        self._reader.start()
        self._writer.start()
        logger.info("TCP 控制通道已建立: %s:%d", self.host, self.port)

    def _open_server(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener = listener
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((self.host, self.port))
            listener.listen(1)
            listener.settimeout(0.2)
            deadline = time.monotonic() + self.timeout_s
            while not self._stopping.is_set():
                try:
                    client, address = listener.accept()
                    break
                except socket.timeout:
                    if time.monotonic() >= deadline:
                        raise TransportClosedError("等待控制通道连接超时")
            else:
                raise TransportClosedError("控制通道已停止")
        finally:
            listener.close()
            self._listener = None

        self._socket = client
        if self.tls_context is not None:
            self._socket = self.tls_context.wrap_socket(client, server_side=True)
        logger.info("TCP 控制通道对端接入: %s", address)

    def close(self) -> None:
        self._stopping.set()
        self._outbox.put(None)
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
        if self._socket is not None:
            try:
                self._socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._socket.close()
            self._socket = None
        self.state = ChannelState.CLOSED
        self._connected.clear()

    def wait_connected(self, timeout_s: float | None = None) -> bool:
        return self._connected.wait(self.timeout_s if timeout_s is None else timeout_s)

    # ---- 收发 ----
    def send_event(self, event: protocol.InputEvent) -> None:
        if self.state != ChannelState.OPEN or self._socket is None:
            raise TransportClosedError("控制通道未连接")
        self._outbox.put(replace(event, extra=dict(event.extra)))

    def _write(self, data: bytes) -> None:
        if self._socket is None:
            raise TransportClosedError("控制通道未连接")
        # 入队而非直接写：写操作集中在一个线程里，避免多线程交错写出半帧
        self._outbox.put(data)

    def _write_loop(self) -> None:
        while True:
            data = self._outbox.get()
            if data is None:
                break
            if self._socket is None:
                break
            if isinstance(data, protocol.InputEvent):
                data = protocol.encode_json(data)
                with self._lock:
                    self.sent_bytes += len(data)
            try:
                self._socket.sendall(data)
            except OSError as exc:
                logger.debug("控制通道写失败: %s", exc)
                self.state = ChannelState.FAILED
                break

    def _read_loop(self) -> None:
        decoder = protocol.FrameDecoder()
        assert self._socket is not None
        sock = self._socket
        last_rx = time.monotonic()

        try:
            while not self._stopping.is_set():
                try:
                    chunk = sock.recv(8192)
                except OSError:
                    break
                if not chunk:
                    break
                last_rx = time.monotonic()

                for frame_type, payload in decoder.push(chunk):
                    if frame_type is protocol.FrameType.JSON:
                        self._dispatch(payload)
                    elif frame_type is protocol.FrameType.PING:
                        self._outbox.put(protocol.encode_frame(protocol.FrameType.PONG, payload))
                    # PONG 只用于刷新 last_rx，无需处理

                if self.heartbeat_s and time.monotonic() - last_rx > self.heartbeat_s * 3:
                    logger.warning("控制通道长时间无数据，判定断链")
                    break
        finally:
            self.state = ChannelState.CLOSED
            self._connected.clear()
            logger.debug("控制通道读循环结束")

    def send_ping(self) -> None:
        self._outbox.put(protocol.encode_frame(protocol.FrameType.PING, b"ka"))
