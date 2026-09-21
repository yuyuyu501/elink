from __future__ import annotations

import ipaddress
import json
import re
import shutil
import socket
from dataclasses import dataclass, field
from pathlib import Path

from .models import Endpoint, ValidationError
from .processes import run_native


@dataclass(frozen=True)
class Peer:
    name: str
    address: str
    os: str
    online: bool
    dns_name: str = ""
    addresses: tuple[str, ...] = ()


@dataclass
class TailnetStatus:
    installed: bool = False
    state: str = "NotInstalled"
    addresses: list[str] = field(default_factory=list)
    peers: list[Peer] = field(default_factory=list)
    health: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return self.installed and self.state == "Running" and bool(self.addresses)


@dataclass(frozen=True)
class RouteProbe:
    kind: str = "unknown"
    latency_ms: float | None = None
    detail: str = ""


def parse_tailnet(value: dict) -> TailnetStatus:
    if not isinstance(value, dict):
        raise ValidationError("Tailscale 返回了无效状态。")
    status = TailnetStatus(installed=True, state=str(value.get("BackendState", "Unknown")),
                           addresses=list(value.get("TailscaleIPs") or []),
                           health=[str(item) for item in (value.get("Health") or [])])
    raw_peers = value.get("Peer") or {}
    if not isinstance(raw_peers, dict):
        raise ValidationError("Tailscale 设备列表无效。")
    for raw in raw_peers.values():
        if not isinstance(raw, dict) or not raw.get("TailscaleIPs"):
            continue
        addresses = raw["TailscaleIPs"]
        address = next((a for a in addresses if ":" not in a), addresses[0])
        Endpoint.parse(address)
        status.peers.append(Peer(str(raw.get("HostName") or raw.get("DNSName") or address),
                                 address, str(raw.get("OS", "")), raw.get("Online") is True,
                                 str(raw.get("DNSName") or "").rstrip("."), tuple(addresses)))
    status.peers.sort(key=lambda peer: (not peer.online, peer.name.casefold()))
    return status


def parse_ping(output: str) -> RouteProbe:
    # This CLI has no JSON output. Only actual pong lines establish a route;
    # the status JSON's Relay field merely identifies a preferred DERP region.
    matches = re.findall(r"^pong from .+? via (.+?) in ([0-9.]+)ms\s*$", output, re.MULTILINE)
    if not matches:
        return RouteProbe(detail=output.strip()[-500:] or "没有收到探测响应。")
    path, milliseconds = matches[-1]
    if path.startswith("DERP("):
        kind = "relay"
    elif path.startswith("peer-relay("):
        kind = "peer-relay"
    else:
        try:
            endpoint = path.rsplit(":", 1)[0].strip("[]")
            ipaddress.ip_address(endpoint)
            kind = "direct"
        except ValueError:
            kind = "unknown"
    return RouteProbe(kind, float(milliseconds), path)


def tailscale_path(override: str = "") -> Path | None:
    candidates = [override, shutil.which("tailscale") or "", r"C:\Program Files\Tailscale\tailscale.exe"]
    return next((Path(p) for p in candidates if p and Path(p).is_file()), None)


def tailnet_status(executable: Path | None) -> TailnetStatus:
    if executable is None:
        return TailnetStatus()
    result = run_native(executable, ["status", "--json"], timeout=12)
    if result.returncode:
        return TailnetStatus(True, "Unavailable", health=[result.stderr.strip()[:600] or "Tailscale 服务不可用。"])
    return parse_tailnet(json.loads(result.stdout))


def tailnet_ping(executable: Path, address: str) -> RouteProbe:
    host = Endpoint.parse(address).hostname
    result = run_native(executable, ["ping", "--c", "5", "--timeout", "2s", host], timeout=18)
    return parse_ping(result.stdout + "\n" + result.stderr)


def netcheck(executable: Path) -> dict:
    result = run_native(executable, ["netcheck", "--format=json"], timeout=25)
    if result.returncode:
        raise ValidationError(result.stderr.strip()[-500:] or "网络诊断失败。")
    report = json.loads(result.stdout)
    keys = ("UDP", "IPv4", "IPv6", "MappingVariesByDestIP", "UPnP", "PMP", "PCP", "PreferredDERP")
    return {key: report.get(key) for key in keys}


def local_addresses() -> list[str]:
    addresses = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            address = info[4][0]
            if address not in addresses and not ipaddress.ip_address(address).is_loopback:
                addresses.append(address)
    except OSError:
        pass
    return addresses
