"""同局域网设备发现（UDP 广播）。

从 ``E:\\elink`` 的「设备页自动发现」借来的交互逻辑：不需要账号或中心服务器，
同一网段里运行 elink 的机器每隔几秒广播一次身份；设备页把在线设备列出来，
点击即可把 SRT / 控制端口填进观看模式。

实现边界（刻意保持简单）：

- 只在同一广播域生效，不做 Tailscale/公网穿透，不扫描网段；
- 协议是纯 JSON、无鉴权、无加密；发现只用于「找到地址」，
  真正连接仍走自己的 SRT + TCP 控制通道；
- 端口 49320（避开 ``E:\\elink`` 的 49201），可多实例共享监听；
- 广播失败或防火墙拦截时只影响发现，不影响手动输入地址。
"""

from __future__ import annotations

import hashlib
import json
import platform
import socket
import time
import uuid
from typing import Any

from PySide6.QtCore import QByteArray, QObject, QTimer, Signal
from PySide6.QtNetwork import QAbstractSocket, QHostAddress, QUdpSocket

from . import __version__

DISCOVERY_PORT = 49320
PROTOCOL = "elink-discovery-v1"
PEER_TTL_S = 15.0
QUERY_INTERVAL_S = 4.0
ANNOUNCE_INTERVAL_S = 4.0
MAX_DATAGRAM = 2048


class DiscoveryService(QObject):
    """UDP 发现服务：广播自己的身份，收集同网段的其它 elink。"""

    peers_changed = Signal(list)
    status_changed = Signal(str)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        port: int = DISCOVERY_PORT,
        instance_id: str | None = None,
    ) -> None:
        super().__init__(parent)
        self._port = port
        self._id = instance_id or _machine_id()
        self._name = socket.gethostname() or "elink"
        self._platform = f"{platform.system()} {platform.release()}".strip()
        self._hosting = False
        self._srt_port = 0
        self._control_port = 7000
        self._running = False
        self._peers: dict[str, dict[str, Any]] = {}
        self._last_query = 0.0
        self._last_announce = 0.0
        self._last_fingerprint: tuple[tuple[str, str, bool], ...] = ()

        self._socket = QUdpSocket(self)
        self._socket.readyRead.connect(self._read_datagrams)
        self._timer = QTimer(self)
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self._tick)

    # ---- 生命周期 ----
    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> bool:
        if self._running:
            return True
        flags = QAbstractSocket.BindFlag.ShareAddress | QAbstractSocket.BindFlag.ReuseAddressHint
        if not self._socket.bind(QHostAddress("0.0.0.0"), self._port, flags):
            message = f"设备发现不可用：UDP {self._port} 无法监听（{self._socket.errorString()}）"
            self.status_changed.emit(message)
            return False
        self._running = True
        self._last_query = 0.0
        self._last_announce = 0.0
        self._timer.start()
        self.status_changed.emit(f"设备发现已开启 · UDP {self._port}")
        self.refresh()
        return True

    def stop(self) -> None:
        if not self._running:
            return
        self._timer.stop()
        self._socket.close()
        self._running = False
        self._peers.clear()
        self._publish(force=True)
        self.status_changed.emit("设备发现已关闭")

    def set_hosting(self, hosting: bool, srt_port: int = 0, control_port: int = 7000) -> None:
        """主机模式启动/停止时更新广播内容。"""
        hosting = bool(hosting)
        srt_port = int(srt_port) if 0 <= int(srt_port or 0) <= 65535 else 0
        control_port = int(control_port) if 0 <= int(control_port or 0) <= 65535 else 7000
        changed = (hosting, srt_port, control_port) != (self._hosting, self._srt_port, self._control_port)
        self._hosting, self._srt_port, self._control_port = hosting, srt_port, control_port
        if changed and self._running:
            self._announce()

    def refresh(self) -> None:
        """立即发一次查询 + 自己的广播（设备页刷新按钮）。"""
        if not self._running:
            return
        self._send_query()
        self._announce()
        self._publish(force=True)

    def peers(self) -> list[dict[str, Any]]:
        return self._snapshot()

    # ---- 定时器 ----
    def _tick(self) -> None:
        now = time.monotonic()
        self._prune(now)
        if now - self._last_query >= QUERY_INTERVAL_S:
            self._send_query()
        if now - self._last_announce >= ANNOUNCE_INTERVAL_S:
            self._announce()
        self._publish()

    def _prune(self, now: float) -> None:
        expired = [key for key, peer in self._peers.items() if now - peer["_seen"] > PEER_TTL_S]
        for key in expired:
            self._peers.pop(key, None)

    # ---- 收发 ----
    def _read_datagrams(self) -> None:
        while self._socket.hasPendingDatagrams():
            size = self._socket.pendingDatagramSize()
            if size <= 0 or size > MAX_DATAGRAM:
                self._socket.readDatagram(size)
                continue
            data, sender, _port = self._socket.readDatagram(size)
            try:
                message = json.loads(bytes(data).decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
            ip = sender.toString()
            if not _valid_ip(ip) or not _valid_message(message):
                continue
            identifier = message["id"]
            if identifier == self._id:
                continue
            self._remember(message, ip)
            if message.get("kind") == "query":
                self._announce(sender)
            self._publish()

    def _send_query(self) -> None:
        self._last_query = time.monotonic()
        payload = self._payload("query", nonce=uuid.uuid4().hex[:12])
        self._broadcast(payload)

    def _announce(self, target: QHostAddress | None = None) -> None:
        self._last_announce = time.monotonic()
        payload = self._payload("announce")
        if target is None:
            self._broadcast(payload)
            return
        data = QByteArray(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        self._socket.writeDatagram(data, target, self._port)

    def _broadcast(self, payload: dict[str, Any]) -> None:
        data = QByteArray(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        self._socket.writeDatagram(data, QHostAddress(QHostAddress.SpecialAddress.Broadcast), self._port)

    def _payload(self, kind: str, **extra: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "proto": PROTOCOL,
            "kind": kind,
            "id": self._id,
            "name": self._name,
            "platform": self._platform,
            "version": __version__,
            "hosting": self._hosting,
            "srt_port": self._srt_port if self._hosting else 0,
            "control_port": self._control_port,
        }
        payload.update(extra)
        return payload

    # ---- 状态 ----
    def _remember(self, message: dict[str, Any], ip: str) -> None:
        identifier = message["id"]
        self._peers[identifier] = {
            "id": identifier,
            "name": message["name"],
            "platform": message.get("platform", ""),
            "version": message.get("version", ""),
            "hosting": bool(message.get("hosting")),
            "srt_port": int(message.get("srt_port") or 0),
            "control_port": int(message.get("control_port") or 7000),
            "ip": ip,
            "_seen": time.monotonic(),
        }

    def _snapshot(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        peers = []
        for peer in self._peers.values():
            if now - peer["_seen"] > PEER_TTL_S:
                continue
            item = {key: value for key, value in peer.items() if not key.startswith("_")}
            peers.append(item)
        peers.sort(key=lambda item: (item.get("name", "").casefold(), item.get("ip", "")))
        return peers

    def _publish(self, *, force: bool = False) -> None:
        peers = self._snapshot()
        fingerprint = tuple((peer["id"], peer["ip"], bool(peer["hosting"])) for peer in peers)
        if force or fingerprint != self._last_fingerprint:
            self._last_fingerprint = fingerprint
            self.peers_changed.emit(peers)


def _machine_id() -> str:
    raw = f"elink-lite:{socket.gethostname()}:{uuid.getnode()}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def _valid_ip(value: str) -> bool:
    return bool(value) and value not in {"0.0.0.0", "255.255.255.255"}


def _valid_message(message: Any) -> bool:
    if not isinstance(message, dict) or message.get("proto") != PROTOCOL:
        return False
    identifier = message.get("id")
    name = message.get("name")
    if not isinstance(identifier, str) or len(identifier) not in range(8, 65):
        return False
    if not all(character in "0123456789abcdef" for character in identifier):
        return False
    if not isinstance(name, str) or not 0 < len(name) <= 80:
        return False
    for key, limit in (("platform", 40), ("version", 24)):
        value = message.get(key, "")
        if not isinstance(value, str) or len(value) > limit:
            return False
    for key in ("srt_port", "control_port"):
        value = message.get(key, 0)
        if type(value) is not int or not 0 <= value <= 65535:
            return False
    if not isinstance(message.get("hosting"), bool):
        return False
    return message.get("kind") in {"query", "announce"}


__all__ = ["DiscoveryService", "DISCOVERY_PORT"]
