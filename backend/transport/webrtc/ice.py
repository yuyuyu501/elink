"""WebRTC ICE 适配层。

**只做适配，不重复实现候选逻辑**（候选收集/配对/检查都在 ``nat/ice.py``）。
这里负责两件 WebRTC 特有的事：

1. 把本地候选写进 SDP 的 ``m=`` 段（``a=candidate:``），把对端候选从 SDP 读出来；
2. 在 ICE 完成/失败时把结果映射成传输层状态。

注意 ufrag/pwd 属于 SDP 会话级参数，必须两端一致；BUNDLE 场景下所有媒体段
共用一组凭据（这里取第一个有值的段）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.logging.logger import get_logger
from backend.nat.candidate import Candidate, CandidateType
from backend.nat.ice import IceAgent, IceConfig, IceState
from backend.signaling.sdp import SdpMedia, SdpSession

logger = get_logger(__name__)


@dataclass
class IceTransport:
    """把 ICE agent 接到 SDP 上。"""

    agent: IceAgent = field(default_factory=IceAgent)
    state: str = IceState.NEW

    # ---- SDP <-> 候选 ----
    def attach_local_candidates(self, session: SdpSession) -> None:
        """把已收集的本地候选写进每个媒体段。"""
        candidates = list(self.agent.local)
        if not candidates:
            logger.debug("没有本地候选可写入 SDP")
            return
        for media in session.media:
            media.remove_attribute("candidate")
            for candidate in candidates:
                media.add_candidate(candidate)

    def set_remote_from_sdp(self, session: SdpSession) -> list[Candidate]:
        """从对端 SDP 收集候选（含会话级候选）。"""
        candidates: list[Candidate] = []
        for value in session.attributes:
            if value.startswith("candidate:"):
                try:
                    candidates.append(Candidate.from_sdp(value[len("candidate:") :]))
                except Exception:
                    logger.debug("忽略无法解析的会话级候选: %s", value)

        for media in session.media:
            candidates.extend(media.candidates)

        # 去重（同一候选可能既在会话级又在媒体级出现）
        unique: dict[tuple[str, int], Candidate] = {}
        for candidate in candidates:
            unique.setdefault(candidate.endpoint, candidate)
        resolved = list(unique.values())
        logger.debug("解析到对端候选 %d 个", len(resolved))
        return resolved

    # ---- 启停 ----
    def gather(self, stun_servers: list[tuple[str, int]] | None = None) -> None:
        if stun_servers:
            self.agent.config.stun_servers = stun_servers
        self.agent.gather()
        self.state = self.agent.state

    def start_checks(self, remote: SdpSession) -> Candidate | None:
        candidates = self.set_remote_from_sdp(remote)
        if not candidates:
            logger.warning("对端 SDP 中没有候选，ICE 无法开始")
            self.state = IceState.FAILED
            return None
        self.agent.set_remote_candidates(candidates)
        pair = self.agent.check()
        self.state = self.agent.state
        return pair.remote if pair else None

    def stop(self) -> None:
        self.agent.close()
        self.state = IceState.CLOSED

    # ---- 状态 ----
    @property
    def chosen_pair(self):
        return self.agent.nominated

    @property
    def ready(self) -> bool:
        return self.agent.state == IceState.CONNECTED

    def describe(self) -> str:
        return self.agent.describe()


def build_ice_config(
    stun_servers: list[tuple[str, int]] | None = None,
    turns: list[dict] | None = None,
    check_timeout_s: float = 0.5,
) -> IceConfig:
    return IceConfig(
        stun_servers=stun_servers or [],
        turns=turns or [],
        check_timeout_s=check_timeout_s,
    )


def host_candidate_media(media: SdpMedia, candidate: Candidate) -> None:
    """给单个媒体段加一条 host 候选（供只支持单段的老对端使用）。"""
    if candidate.type is not CandidateType.HOST:
        logger.debug("非 host 候选写入媒体段: %s", candidate.describe() if hasattr(candidate, "describe") else candidate)
    media.add_candidate(candidate)
