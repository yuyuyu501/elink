"""通用 ICE（候选收集 / 配对 / 连通性检查）。

这是 WebRTC 与自有协议**共用**的候选管理；``transport/webrtc/ice.py`` 只是
把这套逻辑接到 WebRTC 的 SDP 上，不重复实现候选逻辑。

流程：

1. **收集**：host（本机网卡）+ srflx（STUN）+ relay（TURN）；
2. **配对**：本地候选 × 远端候选，按 RFC 8445 优先级排序；
3. **检查**：对每对发 STUN Binding 请求，最先成功的一对变为 nominated；
4. **完成**：有 nominated 对即 ``connected``；全部失败即 ``failed``。

检查是串行 + 有超时的：串行足够应付 4~8 个候选，并且避免一次性打爆 NAT。
"""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass, field

from backend.errors.network import NetworkError
from backend.logging.logger import get_logger
from backend.nat.candidate import (
    Candidate,
    CandidatePair,
    CandidateSet,
    CandidateType,
    Transport,
)
from backend.nat.stun import (
    CLASS_SUCCESS,
    StunClient,
    StunError,
    StunMessage,
    build_binding_request,
)

logger = get_logger(__name__)


class IceError(NetworkError):
    pass


class IceState(str):
    NEW = "new"
    GATHERING = "gathering"
    CHECKING = "checking"
    CONNECTED = "connected"
    FAILED = "failed"
    CLOSED = "closed"


@dataclass
class IceConfig:
    stun_servers: list[tuple[str, int]] = field(default_factory=list)
    turns: list[dict] = field(default_factory=list)
    check_timeout_s: float = 0.5
    max_pairs: int = 32


class IceAgent:
    """候选收集与连通性检查。"""

    def __init__(self, config: IceConfig | None = None) -> None:
        self.config = config or IceConfig()
        self.local = CandidateSet()
        self.remote = CandidateSet()
        self.pairs: list[CandidatePair] = []
        self.state: str = IceState.NEW
        self.nominated: CandidatePair | None = None

    # ---- 收集 ----
    def gather_host_candidates(self, ports: list[int] | None = None) -> list[Candidate]:
        """枚举本机 IPv4 地址作为 host 候选。"""
        self.state = IceState.GATHERING
        addresses = _local_addresses()
        candidates: list[Candidate] = []
        for index, address in enumerate(addresses):
            port = ports[index] if ports and index < len(ports) else 0
            candidate = Candidate(
                address=address,
                port=port or _ephemeral_port(),
                type=CandidateType.HOST,
                foundation=f"host{index}",
            )
            self.local.add(candidate)
            candidates.append(candidate)
        logger.debug("收集 host 候选: %s", [item.endpoint for item in candidates])
        return candidates

    def gather_server_reflexive(self) -> list[Candidate]:
        """用 STUN 探测 srflx 候选；没有 STUN 服务器时直接跳过。"""
        candidates: list[Candidate] = []
        client = StunClient()
        for server in self.config.stun_servers:
            try:
                address, mapped_port = client.binding(server)
            except (StunError, OSError) as exc:
                logger.debug("STUN %s 失败: %s", server, exc)
                continue
            candidate = Candidate(
                address=address,
                port=mapped_port,
                type=CandidateType.SRFLX,
                foundation="srflx0",
                base=address,
            )
            self.local.add(candidate)
            candidates.append(candidate)
        return candidates

    def gather_relay(self) -> list[Candidate]:
        """用 TURN 申请中继候选（最贵，放在最后）。"""
        from backend.nat.turn import discover_relay

        candidates: list[Candidate] = []
        for index, turn in enumerate(self.config.turns):
            relayed = discover_relay(
                turn["server"], turn.get("username", ""), turn.get("password", "")
            )
            if relayed is None:
                continue
            candidate = Candidate(
                address=relayed[0],
                port=relayed[1],
                type=CandidateType.RELAY,
                foundation=f"relay{index}",
            )
            self.local.add(candidate)
            candidates.append(candidate)
        return candidates

    def gather(self) -> CandidateSet:
        self.gather_host_candidates()
        self.gather_server_reflexive()
        self.gather_relay()
        self.state = IceState.CHECKING
        return self.local

    # ---- 配对与检查 ----
    def set_remote_candidates(self, candidates: list[Candidate]) -> list[CandidatePair]:
        for candidate in candidates:
            self.remote.add(candidate)

        pairs = [
            CandidatePair(local=local, remote=remote)
            for local in self.local
            for remote in self.remote
            if local.transport is remote.transport
        ]
        pairs.sort(key=lambda pair: -pair.priority)
        self.pairs = pairs[: self.config.max_pairs]
        logger.debug("生成候选对 %d 个（截断前 %d）", len(self.pairs), len(pairs))
        return self.pairs

    def check(self) -> CandidatePair | None:
        """串行检查候选对，返回第一个成功的对（并标为 nominated）。"""
        if not self.pairs:
            self.state = IceState.FAILED
            raise IceError("没有可检查的候选对（远端候选为空？）")

        for pair in self.pairs:
            pair.state = "in-progress"
            began = time.perf_counter()
            if self._check_pair(pair):
                pair.state = "succeeded"
                pair.nominated = True
                pair.rtt_ms = (time.perf_counter() - began) * 1000
                self.nominated = pair
                self.state = IceState.CONNECTED
                logger.info(
                    "ICE 连通: %s (RTT %.1fms)", pair.describe(), pair.rtt_ms
                )
                return pair
            pair.state = "failed"

        self.state = IceState.FAILED
        logger.warning("ICE 全部候选对检查失败（%d 个）", len(self.pairs))
        return None

    def _check_pair(self, pair: CandidatePair) -> bool:
        """对一对候选发 STUN Binding 请求。"""
        request = build_binding_request()
        payload = request.pack()

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind(("0.0.0.0", pair.local.port))
        except OSError:
            sock.bind(("0.0.0.0", 0))
        try:
            sock.settimeout(self.config.check_timeout_s)
            sock.sendto(payload, pair.remote.endpoint)
            began = time.perf_counter()
            while time.perf_counter() - began < self.config.check_timeout_s:
                try:
                    data, _addr = sock.recvfrom(2048)
                except socket.timeout:
                    break
                try:
                    response = StunMessage.unpack(data)
                except StunError:
                    continue
                if (
                    response.transaction_id == request.transaction_id
                    and response.message_class == CLASS_SUCCESS
                ):
                    return True
            return False
        except OSError as exc:
            logger.debug("候选对检查失败 %s: %s", pair.describe(), exc)
            return False
        finally:
            sock.close()

    # ---- 状态 ----
    def close(self) -> None:
        self.state = IceState.CLOSED
        self.pairs.clear()
        self.nominated = None

    def describe(self) -> str:
        nominated = self.nominated.describe() if self.nominated else "无"
        return (
            f"ICE[{self.state}] 本地={len(self.local)} 远端={len(self.remote)} "
            f"候选对={len(self.pairs)} 选中={nominated}"
        )


def _local_addresses() -> list[str]:
    """枚举本机 IPv4 地址（排除回环）。"""
    addresses: set[str] = set()
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            addresses.add(info[4][0])
    except OSError:
        pass

    # 用一个 UDP "连接" 拿到默认出口地址（不会真的发包）
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        addresses.add(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()

    return sorted(address for address in addresses if not address.startswith("127."))


def _ephemeral_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


__all__ = ["IceAgent", "IceConfig", "IceState", "IceError", "Transport"]
