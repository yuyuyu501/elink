"""Persistent desktop session negotiation; media runs only for an active request."""

from __future__ import annotations

import json
import time
from dataclasses import replace

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtNetwork import QHostAddress, QTcpServer, QTcpSocket

from .service import HostSettings, SessionService, ViewerSettings

SESSION_PORT = 49321
PROTOCOL = "elink-session-v1"
TIMEOUT_S = 30.0
MAX_MESSAGE = 8192


def picture_settings(values: dict) -> dict:
    """Accept only supported numeric picture parameters."""
    if not isinstance(values, dict):
        raise ValueError("画面参数格式错误")
    result = {}
    for name, low, high, cast in (
        ("width", 320, 7680, int), ("height", 240, 4320, int),
        ("fps", 5, 60, float), ("bitrate_kbps", 300, 20000, int),
    ):
        value = values.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"画面参数无效：{name}")
        if not low <= value <= high:
            raise ValueError(f"画面参数超出范围：{name}")
        result[name] = cast(value)
    if result["width"] % 2 or result["height"] % 2:
        raise ValueError("分辨率必须为偶数")
    return result


def saved_picture(settings: ViewerSettings) -> dict:
    return picture_settings({
        "width": settings.stream_width, "height": settings.stream_height,
        "fps": settings.stream_fps, "bitrate_kbps": settings.stream_bitrate_kbps,
    })


class DesktopConnection(QObject):
    changed = Signal()
    error = Signal(str)
    connected = Signal(object)
    picture_applied = Signal(dict)
    mode_applied = Signal(bool)
    finished = Signal()

    def __init__(self, service: SessionService, identity: str, host: HostSettings,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.service, self.identity, self.host = service, identity, host
        self.server = QTcpServer(self)
        self.server.newConnection.connect(self._accept)
        self.socket: QTcpSocket | None = None
        self.role = ""
        self.phase = "idle"
        self._buffer = bytearray()
        self._last_rx = self._started = time.monotonic()
        self._picture: dict = {}
        self._previous: dict | None = None
        self._control = True
        self._peer: dict = {}
        self._viewer = ViewerSettings()
        self._srt_port = self._control_port = 0
        self._pending_action = None
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        service.host_prepared.connect(self._host_prepared)
        service.frame_ready.connect(self._frame)
        service.state_changed.connect(self._service_state)

    @property
    def busy(self) -> bool:
        return self.phase != "idle" or self.service.running

    def start(self, port: int = SESSION_PORT) -> bool:
        if self.server.isListening():
            return True
        if not self.server.listen(QHostAddress.SpecialAddress.AnyIPv4, port):
            self.error.emit(f"连接监听失败：{self.server.errorString()}")
            return False
        self.changed.emit()
        return True

    def close(self) -> None:
        self.server.close()
        self.disconnect()
        self._timer.stop()

    def _attach(self, sock: QTcpSocket, role: str) -> None:
        self.socket, self.role = sock, role
        self._buffer.clear()
        self._last_rx = self._started = time.monotonic()
        self.phase = "connecting"
        sock.readyRead.connect(self._read)
        sock.disconnected.connect(lambda: self._disconnected(sock))
        sock.errorOccurred.connect(lambda _error: self._socket_error(sock))
        self.changed.emit()

    def _accept(self) -> None:
        while self.server.hasPendingConnections():
            sock = self.server.nextPendingConnection()
            if self.busy:
                sock.write(self._encode("error", message="设备正在使用中"))
                sock.disconnected.connect(sock.deleteLater)
                sock.disconnectFromHost()
                continue
            self._attach(sock, "host")

    def connect_peer(self, peer: dict, viewer: ViewerSettings, control: bool) -> None:
        if self.busy:
            self.error.emit("已有会话正在连接或运行")
            return
        try:
            picture = saved_picture(viewer)
            port = int(peer["session_port"])
            if not 0 < port <= 65535:
                raise ValueError("连接端口无效")
        except (KeyError, ValueError, TypeError) as exc:
            self.error.emit(str(exc))
            return
        self._peer, self._viewer = dict(peer), replace(viewer)
        self._picture, self._previous, self._control = picture, None, control
        sock = QTcpSocket(self)
        self._attach(sock, "viewer")
        sock.connected.connect(lambda: self._send(
            "request", target=peer["id"], control=control, picture=picture))
        sock.connectToHost(peer["ip"], port)

    @staticmethod
    def _encode(kind: str, **values) -> bytes:
        return (json.dumps({"proto": PROTOCOL, "kind": kind, **values}) + "\n").encode()

    def _send(self, kind: str, **values) -> None:
        if self.socket is not None:
            self.socket.write(self._encode(kind, id=self.identity, **values))

    def _read(self) -> None:
        sock = self.socket
        if sock is None:
            return
        self._buffer.extend(bytes(sock.readAll()))
        try:
            while b"\n" in self._buffer:
                raw, _, tail = self._buffer.partition(b"\n")
                self._buffer = bytearray(tail)
                if len(raw) > MAX_MESSAGE:
                    raise ValueError("会话消息过长")
                message = json.loads(raw)
                if not isinstance(message, dict) or message.get("proto") != PROTOCOL:
                    raise ValueError("会话协议不匹配")
                self._last_rx = time.monotonic()
                self._message(message)
                if self.socket is not sock:
                    return
            if len(self._buffer) > MAX_MESSAGE:
                raise ValueError("会话消息过长")
        except (ValueError, TypeError, KeyError) as exc:
            self._fail(str(exc))

    def _message(self, message: dict) -> None:
        kind = message.get("kind")
        if kind == "error":
            self._fail(str(message.get("message", "连接失败")), notify=False)
        elif kind == "ping":
            self._send("pong")
        elif kind == "pong":
            pass
        elif kind == "bye":
            self.disconnect()
        elif self.role == "host" and kind == "request" and self.phase == "connecting":
            if message.get("target") != self.identity or not message.get("id"):
                raise ValueError("目标设备不匹配")
            self._picture = picture_settings(message["picture"])
            if not isinstance(message.get("control"), bool):
                raise ValueError("会话模式无效")
            self._control = message["control"]
            self._previous = None
            self._start_host()
        elif self.role == "viewer" and kind == "ready" and self.phase in {"connecting", "updating"}:
            if message.get("id") != self._peer["id"]:
                raise ValueError("设备标识不匹配")
            self._picture = picture_settings(message["picture"])
            self._srt_port, self._control_port = int(message["srt_port"]), int(message["control_port"])
            if not all(0 < port <= 65535 for port in (self._srt_port, self._control_port)):
                raise ValueError("会话端口无效")
            self.phase = "receiving"
            self._started = time.monotonic()
            self._viewer = replace(
                self._viewer, transport="srt", srt_mode="caller",
                srt_url=f"srt://{self._peer['ip']}:{self._srt_port}",
                control_host=self._peer["ip"], control_port=self._control_port,
                control_enabled=self._control,
            )
            self.service.start_viewer(self._viewer)
            self.connected.emit(self._viewer)
            self.changed.emit()
        elif self.role == "host" and kind == "active" and self.phase == "preparing":
            self.phase = "active"
            self._previous = None
            self.changed.emit()
        elif self.role == "host" and kind == "configure" and self.phase == "active":
            try:
                picture = picture_settings(message["picture"])
            except ValueError as exc:
                self._send("rejected", message=str(exc))
                return
            self._previous, self._picture = self._picture, picture
            self.phase = "updating"
            self._started = time.monotonic()
            self._send("stop_media")
        elif self.role == "viewer" and kind == "stop_media" and self.phase in {"active", "receiving", "updating"}:
            self.phase = "updating"
            self._started = time.monotonic()
            self._stop_then(lambda: self._send("stopped"))
            self.changed.emit()
        elif self.role == "host" and kind == "stopped" and self.phase == "updating":
            self._stop_then(self._start_host)
        elif self.role == "host" and kind == "media_failed" and self.phase == "preparing":
            self._failed_media(str(message.get("message", "画面接收失败")))
        elif self.role == "host" and kind == "mode" and self.phase == "active":
            if not isinstance(message.get("control"), bool):
                raise ValueError("会话模式无效")
            self._control = message["control"]
            self.service.set_host_control(self._control)
            self._send("mode", control=self._control)
        elif self.role == "viewer" and kind == "mode" and self.phase == "active":
            if not isinstance(message.get("control"), bool):
                raise ValueError("会话模式无效")
            self._control = message["control"]
            self.mode_applied.emit(self._control)
        elif kind in {"rejected", "rollback"} and self.role == "viewer":
            self.error.emit(str(message.get("message", "画面设置失败")))
        else:
            raise ValueError("会话消息与当前状态不匹配")

    def _start_host(self) -> None:
        self.phase = "preparing"
        self._started = time.monotonic()
        # Session requests and input use separate TCP ports; SRT uses UDP.
        self._control_port = self.server.serverPort() + 1
        self._srt_port = self.server.serverPort() + 2
        if self._srt_port > 65535:
            self._fail("会话端口超出范围")
            return
        settings = replace(
            self.host, **self._picture, transport="srt", srt_mode="listener",
            srt_url=f"srt://0.0.0.0:{self._srt_port}",
            control_port=self._control_port, allow_inject=self._control,
            clipboard_enabled=self._control, gamepad_enabled=self._control,
        )
        try:
            self.service.start_host(settings)
        except Exception as exc:
            self._failed_media(str(exc))
        self.changed.emit()

    def _host_prepared(self) -> None:
        if self.role == "host" and self.phase == "preparing":
            self._send("ready", srt_port=self._srt_port, control_port=self._control_port,
                       picture=self._picture)

    def _frame(self, _frame) -> None:
        if self.role == "viewer" and self.phase == "receiving":
            self.phase = "active"
            self._previous = None
            self._send("active")
            self._viewer = replace(
                self._viewer, **{f"stream_{key}": value for key, value in self._picture.items()}
            )
            self.picture_applied.emit(dict(self._picture))
            self.changed.emit()

    def configure(self, picture: dict) -> None:
        if self.role != "viewer" or self.phase != "active":
            return
        try:
            picture = picture_settings(picture)
        except ValueError as exc:
            self.error.emit(str(exc))
            return
        self._send("configure", picture=picture)

    def set_control(self, enabled: bool) -> None:
        if self.role == "viewer" and self.phase == "active":
            self._send("mode", control=enabled)

    def _stop_then(self, action) -> None:
        self._pending_action = action
        self.service.stop()

    def _service_state(self, state: str, detail: str) -> None:
        if state == "error" and self.socket is not None:
            self._failed_media(detail)

    def _failed_media(self, detail: str) -> None:
        if self.role == "host" and self._previous is not None:
            self._picture, self._previous = self._previous, None
            self.phase = "updating"
            self._started = time.monotonic()
            self._send("rollback", message=f"画面设置失败，恢复此前参数：{detail}")
            self._send("stop_media")
        elif self.role == "viewer" and self.phase == "receiving":
            self.phase = "updating"
            self._stop_then(lambda: self._send("media_failed", message=detail))
        else:
            self._fail(detail)

    def _tick(self) -> None:
        if self._pending_action is not None and not self.service.running:
            action, self._pending_action = self._pending_action, None
            action()
        now = time.monotonic()
        if self.socket is not None:
            # Queued idle notifications may belong to the pipeline being replaced.
            if self.phase in {"active", "preparing", "receiving"} and not self.service.running:
                self._failed_media("画面传输已结束")
            elif now - self._last_rx > TIMEOUT_S:
                self._fail("对端连接超时")
            elif self.phase != "active" and now - self._started > TIMEOUT_S:
                self._fail("桌面会话启动超时")
            elif int(now) != int(now - 0.1):
                self._send("ping")
        elif self.phase == "stopping" and not self.service.running:
            self.phase = "idle"
            self.role = ""
            self.changed.emit()
            self.finished.emit()

    def disconnect(self) -> None:
        self._pending_action = None
        sock, self.socket = self.socket, None
        if sock is not None:
            sock.write(self._encode("bye"))
            sock.disconnectFromHost()
            sock.deleteLater()
        if self.phase != "idle":
            self.phase = "stopping"
        self.service.stop()
        self.changed.emit()

    def _disconnected(self, sock: QTcpSocket) -> None:
        if sock is self.socket:
            self.disconnect()

    def _socket_error(self, sock: QTcpSocket) -> None:
        if sock is self.socket:
            self._fail(f"连接失败：{sock.errorString()}", notify=False)

    def _fail(self, detail: str, *, notify: bool = True) -> None:
        if notify:
            self._send("error", message=detail)
        self.error.emit(detail)
        self.disconnect()
