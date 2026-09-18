from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass


class ValidationError(ValueError):
    pass


@dataclass(frozen=True)
class Endpoint:
    hostname: str
    port: int = 49200

    @classmethod
    def parse(cls, value: str) -> "Endpoint":
        value = value.strip()
        if not value or len(value) > 300 or any(c.isspace() for c in value) or any(c in value for c in "%/\\?#\0"):
            raise ValidationError("请输入有效的 IP 地址或主机名。")
        port, host = 49200, value
        if value.startswith("["):
            match = re.fullmatch(r"\[([^\]]+)\](?::([0-9]+))?", value)
            if not match:
                raise ValidationError("IPv6 端口格式应为 [IPv6]:端口。")
            host = match[1]
            try:
                host = str(ipaddress.IPv6Address(host))
            except ValueError as exc:
                raise ValidationError("IPv6 地址无效。") from exc
            if match[2]:
                port = int(match[2])
        elif value.count(":") == 1:
            host, port_text = value.rsplit(":", 1)
            if not port_text.isascii() or not port_text.isdecimal():
                raise ValidationError("端口必须是数字。")
            port = int(port_text)
        elif ":" in value:
            try:
                host = str(ipaddress.IPv6Address(value))
            except ValueError as exc:
                raise ValidationError("IPv6 地址无效。") from exc
        if not 1024 <= port <= 65535:
            raise ValidationError("Elink 端口必须在 1024 到 65535 之间。")
        if ":" not in host:
            try:
                host = str(ipaddress.IPv4Address(host))
            except ValueError:
                host = host.rstrip(".").lower()
                if not host.isascii() or len(host) > 253 or not all(
                    re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                    for label in host.split(".")
                ):
                    raise ValidationError("地址只能包含 IP 或 DNS 主机名，不能包含 URL、路径或命令选项。")
        return cls(host, port)

    @property
    def authority(self) -> str:
        host = f"[{self.hostname}]" if ":" in self.hostname else self.hostname
        return host if self.port == 49200 else f"{host}:{self.port}"

    def url(self, port: int, path: str, scheme: str = "https") -> str:
        host = f"[{self.hostname}]" if ":" in self.hostname else self.hostname
        return f"{scheme}://{host}:{port}{path}"
