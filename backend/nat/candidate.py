"""ICE 候选地址。

候选的四要素：类型（host/srflx/prflx/relay）、传输（UDP/TCP）、
基地址（base）与优先级。优先级算法按 RFC 8445：

    priority = (2^24) * type_preference + (2^8) * local_preference + (256 - component)

类型优先级决定「先试直连，再试打洞，最后才走中继」——中继最贵也最慢，
必须排在最后。这一条顺序错了，通话会一直走 TURN，带宽费用会莫名其妙升高。
"""

from __future__ import annotations

import enum
import ipaddress
from dataclasses import dataclass, field, replace

from backend.errors.base import ElinkError, ErrorCode


class NatError(ElinkError):
    code = ErrorCode.NETWORK


class CandidateType(str, enum.Enum):
    HOST = "host"
    SRFLX = "srflx"
    PRFLX = "prflx"
    RELAY = "relay"

    @property
    def preference(self) -> int:
        """RFC 8445 的建议值。"""
        return {
            CandidateType.HOST: 126,
            CandidateType.PRFLX: 110,
            CandidateType.SRFLX: 100,
            CandidateType.RELAY: 0,
        }[self]


class Transport(str, enum.Enum):
    UDP = "udp"
    TCP = "tcp"


@dataclass(frozen=True)
class Candidate:
    """一个候选地址。"""

    address: str
    port: int
    type: CandidateType = CandidateType.HOST
    transport: Transport = Transport.UDP
    foundation: str = "1"
    component: int = 1
    local_preference: int = 65535
    base: str | None = None
    related_address: str | None = None
    related_port: int = 0

    def __post_init__(self) -> None:
        if not 0 < self.port < 65536:
            raise NatError(f"非法端口: {self.port}")
        try:
            ipaddress.ip_address(self.address)
        except ValueError as exc:
            raise NatError(f"非法地址: {self.address}") from exc

    @property
    def priority(self) -> int:
        return (
            (1 << 24) * self.type.preference
            + (1 << 8) * self.local_preference
            + (256 - self.component)
        )

    @property
    def endpoint(self) -> tuple[str, int]:
        return (self.address, self.port)

    @property
    def is_relay(self) -> bool:
        return self.type is CandidateType.RELAY

    def to_sdp(self) -> str:
        """SDP ``a=candidate:`` 的属性值（不含前缀）。"""
        parts = [
            self.foundation,
            str(self.component),
            self.transport.value,
            str(self.priority),
            self.address,
            str(self.port),
            "typ",
            self.type.value,
        ]
        if self.related_address:
            parts += ["raddr", self.related_address, "rport", str(self.related_port)]
        if self.base and self.base != self.address:
            parts += ["base", self.base]
        return " ".join(parts)

    @classmethod
    def from_sdp(cls, value: str) -> "Candidate":
        tokens = value.strip().split()
        if len(tokens) < 8:
            raise NatError(f"候选行字段不足: {value!r}")

        foundation, component, transport, _priority, address, port = tokens[:6]
        if tokens[6] != "typ":
            raise NatError("候选行缺少 typ 关键字")

        try:
            candidate_type = CandidateType(tokens[7])
        except ValueError as exc:
            raise NatError(f"未知候选类型: {tokens[7]}") from exc

        extras = dict(zip(tokens[8::2], tokens[9::2]))
        return cls(
            address=address,
            port=int(port),
            type=candidate_type,
            transport=Transport(transport.lower()),
            foundation=foundation,
            component=int(component),
            related_address=extras.get("raddr"),
            related_port=int(extras.get("rport", 0)),
            base=extras.get("base"),
        )

    def with_local_preference(self, value: int) -> "Candidate":
        return replace(self, local_preference=max(0, min(65535, value)))


@dataclass
class CandidatePair:
    """一对候选（本地 + 远端），存在优先级与状态。"""

    local: Candidate
    remote: Candidate
    state: str = "frozen"  # frozen / waiting / in-progress / succeeded / failed
    nominated: bool = False
    rtt_ms: float | None = None

    @property
    def priority(self) -> int:
        # RFC 8445: 取两端优先级中较小的那个，再按较高者打破平局
        return (
            min(self.local.priority, self.remote.priority) << 32
            + max(self.local.priority, self.remote.priority)
        )

    @property
    def key(self) -> tuple[tuple[str, int], tuple[str, int]]:
        return (self.local.endpoint, self.remote.endpoint)

    def describe(self) -> str:
        return (
            f"{self.local.type.value}/{self.remote.type.value} "
            f"{self.local.address}:{self.local.port} -> "
            f"{self.remote.address}:{self.remote.port} [{self.state}]"
        )


@dataclass
class CandidateSet:
    """本地候选集合。"""

    candidates: list[Candidate] = field(default_factory=list)

    def add(self, candidate: Candidate) -> None:
        if all(existing.endpoint != candidate.endpoint for existing in self.candidates):
            self.candidates.append(candidate)

    def by_type(self, candidate_type: CandidateType) -> list[Candidate]:
        return [item for item in self.candidates if item.type is candidate_type]

    def sorted(self) -> list[Candidate]:
        return sorted(self.candidates, key=lambda item: -item.priority)

    def __len__(self) -> int:
        return len(self.candidates)

    def __iter__(self):
        return iter(self.sorted())
