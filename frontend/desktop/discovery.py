"""Device discovery over network interface broadcasts."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import platform
import socket
import time
import uuid
from typing import Any

from PySide6.QtCore import QByteArray, QObject, QTimer, Signal
from PySide6.QtNetwork import QAbstractSocket, QHostAddress, QUdpSocket

from . import __version__
from .network_candidates import network_candidates
from .network_interfaces import network_interfaces

DISCOVERY_PORT = 49320
PROTOCOL = "elink-discovery-v1"
PEER_TTL_S = 15.0
QUERY_INTERVAL_S = 4.0
ANNOUNCE_INTERVAL_S = 4.0
MAX_DATAGRAM = 2048


class DiscoveryService(QObject):
    """Discover Elink peers and merge their live network paths."""

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
        self._last_fingerprint = ()
        self._interfaces: list[dict] = []
        self._bindings: dict[str, tuple[QUdpSocket, dict]] = {}
        self._candidates: list[dict] = []
        self._last_targeted_queries: dict[tuple[str, int], float] = {}
        self._status = ""

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
        self.refresh()
        return True

    def stop(self) -> None:
        if not self._running:
            return
        self._timer.stop()
        for bound, _interface in self._bindings.values():
            bound.close()
            bound.deleteLater()
        self._bindings.clear()
        self._interfaces.clear()
        self._candidates.clear()
        self._last_targeted_queries.clear()
        self._status = ""
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
        """立即发一次查询与广播；定时器持续维护设备列表。"""
        if not self._running:
            return
        self._update_interfaces()
        self._send_query()
        self._announce()
        self._publish(force=True)

    def peers(self) -> list[dict[str, Any]]:
        return self._snapshot()

    # ---- 定时器 ----
    def _tick(self) -> None:
        now = time.monotonic()
        self._update_interfaces()
        self._prune(now)
        if now - self._last_query >= QUERY_INTERVAL_S:
            self._send_query()
        if now - self._last_announce >= ANNOUNCE_INTERVAL_S:
            self._announce()
        self._publish()

    def _prune(self, now: float) -> None:
        for identifier, paths in list(self._peers.items()):
            for ip, peer in list(paths.items()):
                if now - peer["_seen"] > PEER_TTL_S:
                    del paths[ip]
            if not paths:
                del self._peers[identifier]

    def _update_interfaces(self) -> None:
        self._interfaces = network_interfaces()
        active = {}
        for interface in self._interfaces:
            if not interface["up"] or interface["loopback"]:
                continue
            for entry in interface["addresses"]:
                address = ipaddress.ip_address(entry["ip"])
                if address.version == 4 and not address.is_unspecified:
                    active[str(address)] = {**interface, **entry}
        for ip in self._bindings.keys() - active.keys():
            bound, _interface = self._bindings.pop(ip)
            bound.close()
            bound.deleteLater()
        for ip, interface in active.items():
            if ip in self._bindings:
                self._bindings[ip] = (self._bindings[ip][0], interface)
                continue
            bound = QUdpSocket(self)
            flags = QAbstractSocket.BindFlag.ShareAddress | QAbstractSocket.BindFlag.ReuseAddressHint
            if bound.bind(QHostAddress(ip), self._port, flags):
                bound.readyRead.connect(lambda bound=bound: self._read_datagrams(bound))
                self._bindings[ip] = (bound, interface)
            else:
                self.status_changed.emit(f"网卡 {interface['name']} 发现不可用：{bound.errorString()}")
                bound.deleteLater()
        self._candidates = network_candidates(self._interfaces)
        targets = {(item["ip"], item["interface_index"]) for item in self._candidates}
        self._last_targeted_queries = {
            target: sent for target, sent in self._last_targeted_queries.items()
            if target in targets
        }
        self._publish_status()

    def _publish_status(self) -> None:
        message = f"自动发现 · {len(self._bindings)} 个网络地址"
        if not self._bindings:
            message += " · 等待网络连接"
        if message != self._status:
            self._status = message
            self.status_changed.emit(message)

    # ---- 收发 ----
    def _read_datagrams(self, receiver: QUdpSocket | None = None) -> None:
        receiver = receiver or self._socket
        while receiver.hasPendingDatagrams():
            size = receiver.pendingDatagramSize()
            if size <= 0 or size > MAX_DATAGRAM:
                receiver.readDatagram(size)
                continue
            data, sender, _port = receiver.readDatagram(size)
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
            interface = self._bindings.get(receiver.localAddress().toString())
            source = interface[1]["name"] if interface else self._source_for(ip)
            if message.get("kind") == "query":
                self._announce(sender, receiver)
            else:
                self._remember(message, ip, source)
            self._publish()

    def _send_query(self) -> None:
        self._last_query = time.monotonic()
        payload = self._payload("query", nonce=uuid.uuid4().hex[:12])
        self._broadcast(payload)
        data = QByteArray(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        now = time.monotonic()
        bindings = {
            interface["index"]: bound for bound, interface in self._bindings.values()
        }
        for candidate in self._candidates:
            target = (candidate["ip"], candidate["interface_index"])
            bound = bindings.get(candidate["interface_index"])
            if bound is None or now - self._last_targeted_queries.get(target, 0) < QUERY_INTERVAL_S:
                continue
            bound.writeDatagram(data, QHostAddress(candidate["ip"]), self._port)
            self._last_targeted_queries[target] = now

    def _announce(self, target: QHostAddress | None = None, sender: QUdpSocket | None = None) -> None:
        self._last_announce = time.monotonic()
        payload = self._payload("announce")
        if target is None:
            self._broadcast(payload)
            return
        data = QByteArray(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        (sender or self._socket).writeDatagram(data, target, self._port)

    def _broadcast(self, payload: dict[str, Any]) -> None:
        data = QByteArray(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        for bound, interface in self._bindings.values():
            # /31 and /32 tunnels have no usable subnet broadcast.
            if interface["can_broadcast"] and interface["broadcast"] and 0 <= interface["prefix"] < 31:
                bound.writeDatagram(data, QHostAddress(interface["broadcast"]), self._port)

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
    def _source_for(self, ip: str) -> str:
        address = ipaddress.ip_address(ip)
        for local_ip, (_bound, interface) in self._bindings.items():
            if address in ipaddress.ip_network(f"{local_ip}/{interface['prefix']}", strict=False):
                return interface["name"]
        return ""

    def _remember(self, message: dict[str, Any], ip: str, interface: str = "") -> None:
        identifier = message["id"]
        self._peers.setdefault(identifier, {})[ip] = {
            "id": identifier,
            "name": message["name"],
            "platform": message.get("platform", ""),
            "version": message.get("version", ""),
            "hosting": bool(message.get("hosting")),
            "srt_port": int(message.get("srt_port") or 0),
            "control_port": int(message.get("control_port") or 7000),
            "ip": ip,
            "interface": interface,
            "_seen": time.monotonic(),
        }

    def _snapshot(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        peers = []
        for paths in self._peers.values():
            live = sorted(
                (peer for peer in paths.values() if now - peer["_seen"] <= PEER_TTL_S),
                key=lambda peer: peer["ip"],
            )
            if not live:
                continue
            item = {key: value for key, value in live[0].items() if not key.startswith("_")}
            item["addresses"] = [
                {"ip": peer["ip"], "interface": peer["interface"]} for peer in live
            ]
            peers.append(item)
        peers.sort(key=lambda item: (item.get("name", "").casefold(), item.get("ip", "")))
        return peers

    def _publish(self, *, force: bool = False) -> None:
        peers = self._snapshot()
        fingerprint = tuple(tuple(sorted(peer.items())) for peer in peers)
        if force or fingerprint != self._last_fingerprint:
            self._last_fingerprint = fingerprint
            self.peers_changed.emit(peers)


def _machine_id() -> str:
    raw = f"elink-lite:{socket.gethostname()}:{uuid.getnode()}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def _valid_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
        return address.version == 4 and not (
            address.is_unspecified or address.is_loopback or address.is_multicast
            or value == "255.255.255.255"
        )
    except ValueError:
        return False


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
