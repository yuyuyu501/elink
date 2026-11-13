"""Persistent desktop session negotiation; media runs only for an active request."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import replace

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtNetwork import QHostAddress, QTcpServer, QTcpSocket

from .service import HostSettings, SessionService, ViewerSettings
from .error_report import remote_details, save_error
from backend.media.video.codec.probe import (
    checked_capabilities, codec_options, compatible_choices,
)

SESSION_PORT = 49321
PROTOCOL = "elink-session-v1"
TIMEOUT_S = 30.0
MAX_MESSAGE = 16384
PICTURE_KEYS = ("width", "height", "fps", "bitrate_kbps")


def output_picture(picture):
    result = {key: picture[key] for key in PICTURE_KEYS}
    if "upscale" in picture:
        result.update(width=picture["upscale"]["width"], height=picture["upscale"]["height"])
    return result


def stream_picture(picture, viewer):
    result = picture_settings(picture)
    if viewer.sr_enabled:
        result["upscale"] = {
            "width": result["width"], "height": result["height"],
            "percent": viewer.sr_percent, "mode": viewer.sr_mode,
            "sharpen": viewer.sr_sharpen, "sharpness": viewer.sr_sharpness,
        }
        for axis in ("width", "height"):
            result[axis] = max(2, int(result[axis] * viewer.sr_percent / 100) & ~1)
    return picture_settings(result)


def picture_settings(values: dict) -> dict:
    """Accept only supported numeric picture parameters."""
    if not isinstance(values, dict):
        raise ValueError("画面参数格式错误")
    result = {}
    for name, low, high, cast in (
        ("width", 320, 7680, int), ("height", 240, 4320, int),
        ("fps", 5, 120, float), ("bitrate_kbps", 300, 50000, int),
    ):
        value = values.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"画面参数无效：{name}")
        if name in {"width", "height"} and "upscale" in values:
            low = 2
        if not low <= value <= high:
            raise ValueError(f"画面参数超出范围：{name}")
        result[name] = cast(value)
    if result["width"] % 2 or result["height"] % 2:
        raise ValueError("分辨率必须为偶数")
    if "upscale" in values:
        sr = values["upscale"]
        if not isinstance(sr, dict) or set(sr) != {"width", "height", "percent", "mode", "sharpen", "sharpness"}:
            raise ValueError("超分参数格式错误")
        for key, low, high in (("width", 320, 7680), ("height", 240, 4320),
                               ("percent", 33, 100), ("sharpness", 0, 100)):
            if type(sr[key]) is not int or not low <= sr[key] <= high:
                raise ValueError(f"超分参数无效：{key}")
        if sr["mode"] not in {"fsr", "rtx"} or type(sr["sharpen"]) is not bool:
            raise ValueError("超分算法或锐化参数无效")
        for axis in ("width", "height"):
            if sr[axis] % 2 or result[axis] > sr[axis]:
                raise ValueError("超分尺寸无效")
        result["upscale"] = dict(sr)
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
    capabilities_changed = Signal()
    codec_selection_required = Signal()

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
        self._session_id = ""
        self._stop_deadline = 0.0
        self._ping_token = None
        self._closing = False
        self._codec_enabled = False
        self._upscale_enabled = False
        self._peer_caps = None
        self._selection = None
        self._prior_selection = None
        self._preferences = codec_options({})
        self._applied_options = codec_options({})
        self._choices = []
        self._revision = 0
        self._offer_sent = False
        self._validating = None
        self._rollback = False
        self._host_attempted = False
        self.fault_id = ""
        self._fault_source = "local"
        self.changed.connect(self._update_error_context)
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        service.host_prepared.connect(self._host_prepared)
        service.frame_ready.connect(self._frame)
        service.state_changed.connect(self._service_state)
        if hasattr(service, "refresh_codecs"):
            service.capabilities_changed.connect(self._capabilities_ready)
            service.codec_validated.connect(self._codec_validated)
            service.refresh_codecs(host.ffmpeg_path)

    @property
    def codec_capabilities(self):
        local = getattr(self.service, "codec_capabilities", None)
        return self._peer_caps, local

    def _capabilities_ready(self):
        self.capabilities_changed.emit()
        if not self._codec_enabled:
            return
        local = getattr(self.service, "codec_capabilities", None)
        if local is None:
            return
        if self.role == "host" and self.phase == "negotiating" and not self._offer_sent:
            self._offer_sent = True
            self._send("capabilities", capabilities=local, upscale=True)
        elif self.role == "viewer" and self.phase == "negotiating" and self._peer_caps is not None:
            self._begin_choices()

    def refresh_codecs(self):
        if self.role != "viewer" or self.phase != "active" or not self._codec_enabled:
            return
        self._preferences = dict(self._applied_options)
        self._peer_caps = None
        self.service.codec_capabilities = None
        self._host_attempted = False
        self.phase = "negotiating"
        self._started = time.monotonic()
        self._send("refresh_codecs")
        self._stop_then(lambda: self.service.refresh_codecs(self._viewer.ffmpeg_path, refresh=True))
        self.capabilities_changed.emit()
        self.changed.emit()

    def _begin_choices(self):
        try:
            self._choices = compatible_choices(self._peer_caps, self.service.codec_capabilities,
                                               self._preferences)
        except ValueError as exc:
            if self.service.running:
                if self._previous is not None:
                    self._picture, self._previous = self._previous, None
                self.phase = "active"
                self.error.emit(str(exc))
                self.changed.emit()
            else:
                self.phase = "selection"
                self._send("selection_pending")
                self.error.emit(str(exc))
                self.changed.emit()
                self.codec_selection_required.emit()
            return
        self._try_choice()

    def _try_choice(self):
        if not self._choices:
            if not self._host_attempted and self.service.running:
                if self._previous is not None:
                    self._picture, self._previous = self._previous, None
                self._selection, self._prior_selection = self._prior_selection, None
                self.phase = "active"
                self.changed.emit()
                return
            self._send("restore")
            self.phase = "updating"
            self._started = time.monotonic()
            return
        self._selection = self._choices.pop(0)
        self._revision += 1
        self.phase = "validating"
        self._started = time.monotonic()
        self._validating = "decoder"
        self.service.validate_codec(self._revision, "decoder", self._selection,
                                    self._picture, self._viewer.ffmpeg_path)
        self.changed.emit()

    def _codec_validated(self, revision, reason):
        if revision != self._revision or self.phase != "validating":
            return
        kind, self._validating = self._validating, None
        if reason:
            if self.role == "viewer":
                if self._preferences["decoder"] == "auto":
                    self._choices = [item for item in self._choices if item["decoder"] != self._selection["decoder"]]
                else:
                    self._choices = []
                if not self._choices:
                    self.error.emit(f"解码器验证失败：{reason}")
                self._try_choice()
            else:
                self.phase = "active" if self.service.running else "negotiating"
                self._send("candidate_failed", message=f"编码器验证失败：{reason}", backend="encoder",
                           revision=self._revision)
            return
        if self.role == "viewer":
            self.phase = "updating"
            self._host_attempted = True
            self._send("select", picture=self._picture, selection=self._selection,
                       preference=self._preferences, capabilities=self.service.codec_capabilities,
                       revision=self._revision)
        elif self.service.running:
            self.phase = "updating"
            self._send("stop_media")
        else:
            self._start_host()
        self.changed.emit()

    def _restore_codecs(self):
        if self._previous is None:
            self._fail("所选编解码组合未能启动，请选择其他可用选项")
            return
        self._picture, self._previous = self._previous, None
        self._selection, self._prior_selection = self._prior_selection, None
        self._rollback = True
        self._send("rollback", message="编解码设置失败，恢复此前参数")
        self.phase = "updating"
        self._started = time.monotonic()
        self._send("stop_media")

    @property
    def busy(self) -> bool:
        return self.phase != "idle" or self.service.running

    def start(self, port: int = SESSION_PORT) -> bool:
        if self.server.isListening():
            return True
        if not self.server.listen(QHostAddress.SpecialAddress.AnyIPv4, port):
            detail = f"连接监听失败：{self.server.errorString()}"
            self._report(detail)
            self.error.emit(detail)
            return False
        self.changed.emit()
        return True

    def close(self) -> None:
        self.server.close()
        self._closing = True
        if hasattr(self.service, "cancel_codec_validation"):
            self.service.cancel_codec_validation(include_probe=True)
        self.disconnect()
        if self.phase == "idle":
            self._timer.stop()

    def _attach(self, sock: QTcpSocket, role: str) -> None:
        self.socket, self.role = sock, role
        self._clear_failure()
        self.service.failure_id = ""
        self.service.failure_message = ""
        self._buffer.clear()
        self._last_rx = self._started = time.monotonic()
        self.phase = "connecting"
        self._session_id = uuid.uuid4().hex if role == "viewer" else ""
        self._stop_deadline = 0.0
        self._ping_token = None
        self._codec_enabled = False
        self._upscale_enabled = False
        self._peer_caps = None
        self._selection = self._prior_selection = None
        self._revision = 0
        self._offer_sent = self._rollback = self._host_attempted = False
        sock.readyRead.connect(lambda: self._read() if sock is self.socket else None)
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
        self._clear_failure()
        try:
            picture = saved_picture(viewer)
            port = int(peer["session_port"])
            if not 0 < port <= 65535:
                raise ValueError("连接端口无效")
        except (KeyError, ValueError, TypeError) as exc:
            self._report(exc)
            self.error.emit(str(exc))
            return
        self._peer, self._viewer = dict(peer), replace(viewer)
        self._picture, self._previous, self._control = stream_picture(picture, viewer), None, control
        if hasattr(self.service, "refresh_codecs"):
            self.service.refresh_codecs(viewer.ffmpeg_path)
        try:
            self._preferences = codec_options(viewer.codec_preferences.get(str(peer["id"]), {}))
        except (ValueError, AttributeError):
            self._preferences = codec_options({})
        sock = QTcpSocket(self)
        self._attach(sock, "viewer")
        sock.connected.connect(lambda: self._send(
            "request", target=peer["id"], control=control, picture=picture,
            codec_selection=hasattr(self.service, "refresh_codecs"),
            upscale=True, upscale_picture=self._picture)
            if sock is self.socket and self.phase == "connecting" else None)
        sock.connectToHost(peer["ip"], port)

    @staticmethod
    def _encode(kind: str, **values) -> bytes:
        return (json.dumps({"proto": PROTOCOL, "kind": kind, **values}) + "\n").encode()

    def _send(self, kind: str, **values) -> None:
        if self.socket is not None:
            if (kind in {"error", "media_failed", "candidate_failed", "rollback"}
                    and self.fault_id and self.fault_id == getattr(self.service, "failure_id", "")):
                values.setdefault("diagnostic", remote_details(getattr(self.service, "failure_details", {})))
                values.setdefault("fault_id", self.fault_id)
                values.setdefault("source", self.identity)
            if self._codec_enabled:
                values.setdefault("revision", self._revision)
            self.socket.write(self._encode(kind, id=self.identity, session=self._session_id, **values))

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
            self._report(exc)
            self._fail(str(exc))

    def _message(self, message: dict) -> None:
        kind = message.get("kind")
        if self._session_id and message.get("session", self._session_id) != self._session_id:
            return
        if (self._codec_enabled and kind in {"ready", "candidate_failed", "stop_media", "stopped", "active", "media_failed"}
                and message.get("revision", self._revision) != self._revision):
            return
        if self.phase == "stopping":
            if kind == "bye":
                self._send("bye_ack")
                self._complete_disconnect()
            elif kind == "bye_ack":
                self._complete_disconnect()
            return
        if kind == "error":
            self._remote_failure(message)
            self._fail(str(message.get("message", "连接失败")), notify=False)
        elif kind == "ping":
            self._send("pong", token=message.get("token"))
        elif kind == "pong":
            if message.get("token") == self._ping_token and self._ping_token is not None:
                stats = getattr(self.service, "stats", None)
                if stats is not None:
                    stats.extra["rtt_ms"] = (time.monotonic() - self._ping_token) * 1000
                    stats.extra["rtt_sampled_at"] = time.monotonic()
                self._ping_token = None
        elif kind == "bye":
            self._pending_action = None
            self.phase = "stopping"
            self._started = time.monotonic()
            self._send("bye_ack")
            self._complete_disconnect()
        elif self.role == "host" and kind == "request" and self.phase == "connecting":
            if message.get("target") != self.identity or not message.get("id"):
                raise ValueError("目标设备不匹配")
            self._upscale_enabled = message.get("upscale") is True
            self._picture = picture_settings(message.get("upscale_picture", message["picture"])
                                             if self._upscale_enabled else message["picture"])
            if not isinstance(message.get("control"), bool):
                raise ValueError("会话模式无效")
            self._control = message["control"]
            self._session_id = str(message.get("session", ""))
            self._previous = None
            self._prior_selection = None
            self._codec_enabled = message.get("codec_selection") is True and hasattr(self.service, "refresh_codecs")
            if self._codec_enabled:
                self.phase = "negotiating"
                self.service.refresh_codecs(self.host.ffmpeg_path)
                self._capabilities_ready()
            else:
                self._start_host()
        elif kind == "capabilities" and self.role == "viewer" and self.phase in {"connecting", "negotiating"}:
            self._codec_enabled = True
            self._upscale_enabled = message.get("upscale") is True
            if not self._upscale_enabled:
                self._picture = output_picture(self._picture)
                self._viewer = replace(self._viewer, sr_enabled=False)
            self._peer_caps = checked_capabilities(message["capabilities"])
            self.phase = "negotiating"
            self._capabilities_ready()
        elif kind == "select" and self.role == "host" and self._codec_enabled and self.phase in {
            "negotiating", "active", "validating", "selection"
        }:
            revision = message.get("revision")
            if type(revision) is not int or revision <= self._revision:
                return
            picture = picture_settings(message["picture"])
            selection = codec_options(message["selection"])
            preference = codec_options(message["preference"])
            peer_caps = checked_capabilities(message["capabilities"])
            choices = compatible_choices(self.service.codec_capabilities, peer_caps, preference)
            if selection not in choices:
                raise ValueError("所选编解码器未通过双方能力验证")
            if self.phase == "active" and self._previous is None:
                self._previous = dict(self._picture)
                self._prior_selection = dict(self._selection)
            self._rollback = False
            self._peer_caps = peer_caps
            self._picture, self._selection, self._preferences = picture, selection, preference
            self._revision = revision
            self._validating = "encoder"
            self.phase = "validating"
            self._started = time.monotonic()
            self.service.validate_codec(revision, "encoder", selection, picture, self.host.ffmpeg_path)
        elif kind == "candidate_failed" and self.role == "viewer" and self._codec_enabled:
            self._remote_failure(message)
            reason = str(message.get("message", "编解码器不可用"))
            axis = message.get("backend", "encoder")
            if axis not in {"encoder", "decoder", "pair"}:
                raise ValueError("无效编解码失败类型")
            if axis == "pair":
                pass
            elif self._preferences[axis] == "auto":
                self._choices = [item for item in self._choices if item[axis] != self._selection[axis]]
            else:
                self._choices = []
            if not self._choices:
                self.error.emit(reason)
            self._stop_then(self._try_choice) if self.service.running else self._try_choice()
        elif (kind == "restore" and self.role == "host" and self._codec_enabled
              and self.phase in {"negotiating", "active", "selection"}):
            revision = message.get("revision")
            if type(revision) is not int or revision < self._revision:
                return
            self._revision = revision
            self._restore_codecs()
        elif kind == "selection_pending" and self.role == "host" and self._codec_enabled and self.phase == "negotiating":
            self.phase = "selection"
        elif kind == "refresh_codecs" and self.role == "host" and self._codec_enabled and self.phase == "active":
            self._previous = dict(self._picture)
            self._prior_selection = dict(self._selection)
            self._offer_sent = False
            self.service.codec_capabilities = None
            self.phase = "negotiating"
            self._started = time.monotonic()
            self._stop_then(lambda: self.service.refresh_codecs(self.host.ffmpeg_path, refresh=True))
        elif self.role == "viewer" and kind == "ready" and self.phase in {"connecting", "updating"}:
            if message.get("id") != self._peer["id"]:
                raise ValueError("设备标识不匹配")
            self._picture = picture_settings(message["picture"])
            self._upscale_enabled = message.get("upscale") is True
            if self._codec_enabled:
                self._selection = codec_options(message["selection"])
                if self._selection not in compatible_choices(self._peer_caps, self.service.codec_capabilities, self._selection):
                    raise ValueError("对端返回了无效编解码组合")
                self._rollback = message.get("rollback") is True
            self._srt_port, self._control_port = int(message["srt_port"]), int(message["control_port"])
            if not all(0 < port <= 65535 for port in (self._srt_port, self._control_port)):
                raise ValueError("会话端口无效")
            self.phase = "receiving"
            self._started = time.monotonic()
            self._update_error_context()
            sr = self._picture.get("upscale")
            self._viewer = replace(
                self._viewer, transport="srt", srt_mode="caller",
                srt_url=f"srt://{self._peer['ip']}:{self._srt_port}",
                control_host=self._peer["ip"], control_port=self._control_port,
                control_enabled=self._control,
                **{f"stream_{key}": self._picture[key] for key in PICTURE_KEYS},
                output_width=sr["width"] if sr else self._picture["width"],
                output_height=sr["height"] if sr else self._picture["height"],
                sr_enabled=sr is not None,
                **({"sr_percent": sr["percent"], "sr_mode": sr["mode"],
                    "sr_sharpen": sr["sharpen"], "sr_sharpness": sr["sharpness"]} if sr else {}),
                **({"stream_codec": self._selection["codec"], "decoder": self._selection["decoder"]}
                   if self._selection else {"decoder": "h264"}),
            )
            self.service.start_viewer(self._viewer)
            self.connected.emit(self._viewer)
            self.changed.emit()
        elif self.role == "host" and kind == "active" and self.phase == "preparing":
            self.phase = "active"
            self._previous = None
            self._prior_selection = None
            self._clear_failure()
            self.changed.emit()
        elif self.role == "host" and kind == "configure" and self.phase == "active":
            self._clear_failure()
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
            self._remote_failure(message)
            if self._codec_enabled and not self._rollback:
                self.phase = "negotiating"
                self._stop_then(lambda: self._send(
                    "candidate_failed", message=str(message.get("message", "画面接收失败")),
                    backend="pair"))
            else:
                self._failed_media(str(message.get("message", "画面接收失败")))
        elif self.role == "host" and kind == "mode" and self.phase == "active":
            if not isinstance(message.get("control"), bool):
                raise ValueError("会话模式无效")
            self._control = message["control"]
            self.service.set_host_control(self._control)
            self._send("mode", control=self._control)
        elif self.role == "viewer" and kind == "mode":
            if not isinstance(message.get("control"), bool):
                raise ValueError("会话模式无效")
            self._control = message["control"]
            self.mode_applied.emit(self._control)
        elif kind in {"rejected", "rollback"} and self.role == "viewer":
            self._remote_failure(message)
            self._report(str(message.get("message", "画面设置失败")))
            self.error.emit(str(message.get("message", "画面设置失败")))
            self._clear_failure()
            if kind == "rejected":
                self.phase = "active"
                self.changed.emit()
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
            self.host, **{key: self._picture[key] for key in PICTURE_KEYS}, transport="srt", srt_mode="listener",
            srt_url=f"srt://0.0.0.0:{self._srt_port}",
            control_port=self._control_port, allow_inject=self._control,
            clipboard_enabled=self._control, gamepad_enabled=self._control,
            **({"codec": self._selection["codec"], "encoder": self._selection["encoder"]}
               if self._selection else {}),
        )
        self._update_error_context()
        try:
            self.service.start_host(settings)
        except Exception as exc:
            self._report(exc)
            self._failed_media(str(exc))
        self.changed.emit()

    def _host_prepared(self) -> None:
        if self.role == "host" and self.phase == "preparing":
            actual = getattr(self.service, "_host_live", None)
            if actual is not None:
                self._picture = {**self._picture, "width": actual.width, "height": actual.height}
            self._send("ready", srt_port=self._srt_port, control_port=self._control_port,
                       picture=self._picture, upscale=True, **({
                           "selection": self._selection, "revision": self._revision, "rollback": self._rollback
                       } if self._codec_enabled else {}))

    def _frame(self, _frame) -> None:
        if self.role == "viewer" and self.phase == "receiving":
            self.phase = "active"
            self._previous = None
            self._prior_selection = None
            if self._codec_enabled and not self._rollback:
                self._applied_options = dict(self._preferences)
                self._viewer.codec_preferences[str(self._peer["id"])] = dict(self._preferences)
            self._clear_failure()
            self._send("active")
            self._viewer = replace(
                self._viewer, **{f"stream_{key}": self._picture[key] for key in PICTURE_KEYS}
            )
            self.picture_applied.emit(output_picture(self._picture))
            self.changed.emit()

    def configure(self, picture: dict, *, upscale=None) -> None:
        if self.role != "viewer" or self.phase not in {"active", "selection"}:
            return
        self._clear_failure()
        try:
            options = codec_options(picture) if "codec" in picture else self._applied_options
            settings = upscale if upscale is not None else self._viewer
            if settings.sr_enabled and not self._upscale_enabled:
                raise ValueError("对端版本不支持超分压缩比")
            picture = stream_picture(picture, settings)
        except ValueError as exc:
            self._report(exc)
            self.error.emit(str(exc))
            self._clear_failure()
            return
        if self._codec_enabled:
            self._previous = dict(self._picture)
            self._prior_selection = dict(self._selection) if self._selection else None
            self._picture, self._preferences = picture, options
            self._rollback = False
            self._host_attempted = False
            self._begin_choices()
        else:
            self._send("configure", picture=picture)
            self.phase = "updating"
            self._started = time.monotonic()
            self.changed.emit()

    def set_control(self, enabled: bool) -> None:
        if self.role == "viewer" and self.phase == "active":
            self._send("mode", control=enabled)

    def _stop_then(self, action) -> None:
        self._pending_action = action
        self.service.stop()

    def _service_state(self, state: str, detail: str) -> None:
        self._update_error_context()
        if state == "error" and self.socket is not None:
            self._failed_media(detail)

    def _failed_media(self, detail: str) -> None:
        if self.phase in {"stopping", "idle"}:
            return
        detail = getattr(self.service, "failure_message", "") or detail
        self._report(detail)
        if self._codec_enabled and not self._rollback:
            if self.role == "host" and self.phase == "preparing":
                self.phase = "negotiating"
                self._stop_then(lambda: self._send(
                    "candidate_failed", message=detail, backend="encoder", revision=self._revision))
                return
            if self.role == "viewer" and self.phase == "receiving":
                self.phase = "updating"
                self._stop_then(lambda: self._send("media_failed", message=detail,
                                                  fault_id=self.fault_id, source=self.identity))
                return
        if self.role == "host" and self._previous is not None:
            self._picture, self._previous = self._previous, None
            self.phase = "updating"
            self._started = time.monotonic()
            self._send("rollback", message=f"画面设置失败，恢复此前参数：{detail}",
                       fault_id=self.fault_id, source=self.identity)
            self._send("stop_media")
        elif self.role == "viewer" and self.phase == "receiving":
            self.phase = "updating"
            self._stop_then(lambda: self._send("media_failed", message=detail,
                                              fault_id=self.fault_id, source=self.identity))
        else:
            self._fail(detail)

    def _tick(self) -> None:
        if self._pending_action is not None and not self.service.running:
            action, self._pending_action = self._pending_action, None
            action()
        now = time.monotonic()
        if self.phase == "stopping":
            if self.socket is not None and now >= self._stop_deadline:
                self._complete_disconnect()
            if self.socket is None and not self.service.running:
                self.phase, self.role, self._session_id = "idle", "", ""
                self._picture = {}
                self._buffer.clear()
                self.changed.emit()
                self.finished.emit()
                if self._closing:
                    self._timer.stop()
            return
        if self.socket is not None:
            # Queued idle notifications may belong to the pipeline being replaced.
            if self.phase in {"active", "preparing", "receiving"} and not self.service.running:
                self._failed_media("画面传输已结束")
            elif now - self._last_rx > TIMEOUT_S:
                self._fail("对端连接超时")
            elif self.phase not in {"active", "selection"} and now - self._started > (
                120 if self.phase == "negotiating" and getattr(self.service, "codec_capabilities", None) is None
                else TIMEOUT_S
            ):
                if self._codec_enabled and self.phase in {"preparing", "receiving"}:
                    self._failed_media("桌面会话启动超时")
                else:
                    self._fail("桌面会话启动超时")
            elif int(now) != int(now - 0.1) and (self._ping_token is None or now - self._ping_token > 3):
                self._ping_token = now
                self._send("ping", token=now)

    def disconnect(self) -> None:
        if self.phase == "stopping":
            return
        self._pending_action = None
        if hasattr(self.service, "cancel_codec_validation"):
            self.service.cancel_codec_validation()
        focus = getattr(self.service, "set_input_focus", None)
        if focus is not None:
            focus(False)
        if self.phase == "idle" and self.socket is None:
            self.service.stop()
            return
        self.phase = "stopping"
        self._stop_deadline = time.monotonic() + 1.0
        expect_stop = getattr(self.service, "expect_stop", None)
        if expect_stop is not None:
            expect_stop()
        self._send("bye")
        if self.socket is None or not self._session_id:
            self._complete_disconnect()
        self.changed.emit()

    def _complete_disconnect(self) -> None:
        expect_stop = getattr(self.service, "expect_stop", None)
        if expect_stop is not None:
            expect_stop()
        sock, self.socket = self.socket, None
        if sock is not None:
            sock.disconnectFromHost()
            if sock.state() == QTcpSocket.SocketState.UnconnectedState:
                sock.deleteLater()
            else:
                sock.disconnected.connect(sock.deleteLater)
        if self.phase != "idle":
            self.phase = "stopping"
        self.service.stop()
        self.changed.emit()

    def _disconnected(self, sock: QTcpSocket) -> None:
        if sock is self.socket:
            if self.phase == "stopping":
                self._complete_disconnect()
                return
            self._report("对端会话连接意外断开")
            self.disconnect()

    def _socket_error(self, sock: QTcpSocket) -> None:
        if sock is self.socket:
            if self.phase == "stopping":
                self._complete_disconnect()
                return
            self._fail(f"连接失败：{sock.errorString()}", notify=False)

    def _fail(self, detail: str, *, notify: bool = True) -> None:
        self._report(detail)
        if notify:
            self._send("error", message=detail, fault_id=self.fault_id, source=self.identity)
        self.error.emit(detail)
        self.disconnect()

    def _update_error_context(self) -> None:
        self.service.error_context = {
            "role": self.role, "phase": self.phase, "identity": self.identity,
            "peer": self.socket.peerAddress().toString() if self.socket else self._peer.get("ip", ""),
            "picture": dict(self._picture),
            "codec_selection": self._selection, "codec_preference": self._preferences,
            "revision": self._revision,
        }

    def _clear_failure(self) -> None:
        self.fault_id = ""
        self._fault_source = "local"

    def _remote_failure(self, message: dict) -> None:
        fault_id = message.get("fault_id")
        if isinstance(fault_id, str) and 0 < len(fault_id) <= 64:
            self.fault_id = fault_id
        self._fault_source = f"remote:{str(message.get('source', 'peer'))[:128]}"
        diagnostic = message.get("diagnostic")
        if isinstance(diagnostic, dict):
            self._update_error_context()
            self.fault_id = save_error(
                str(message.get("message", "远端会话错误")), fault_id=self.fault_id,
                source=self._fault_source, context=self.service.error_context, remote=diagnostic)

    def _report(self, error: BaseException | str) -> None:
        self._update_error_context()
        fault_id = self.fault_id or getattr(self.service, "failure_id", "")
        self.fault_id = save_error(error, fault_id=fault_id, source=self._fault_source,
                                   context=self.service.error_context)
