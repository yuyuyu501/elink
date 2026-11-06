"""PeerConnection（消费 SDP 对象，不改写 SDP 文本）。

边界（重点）：

- **本模块绝不拼接或解析 SDP 字符串**，一切交给 ``signaling/sdp.py``；
- 这里只维护连接状态机、轨道集合、DataChannel 与 ICE 适配；
- DTLS/SRTP 的加解密由底层栈负责，本层只交换指纹与凭据。

当前里程碑的实际用途：媒体仍走 ``transport/srt.py``（已验证），本类负责
「协商 + 轨道编排 + 状态」这部分逻辑；接入真正的 WebRTC 栈时替换
``_transport`` 即可，上层接口不变。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from backend.errors.network import NetworkError
from backend.logging.logger import get_logger
from backend.media.packet import Packet
from backend.signaling.sdp import SdpSession, build_answer, build_offer
from backend.transport.webrtc.data_channel import DataChannel, DataChannelConfig
from backend.transport.webrtc.ice import IceTransport
from backend.transport.webrtc.media_track import MediaTrack

logger = get_logger(__name__)


class ConnectionState(str):
    NEW = "new"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    FAILED = "failed"
    CLOSED = "closed"


@dataclass
class PeerConnectionConfig:
    video_payload_type: int = 96
    audio_payload_type: int = 111
    ssrc: int = 0x12345678
    stun_servers: list[tuple[str, int]] = field(default_factory=list)
    data_channel: DataChannelConfig = field(default_factory=DataChannelConfig)


class PeerConnection:
    """一个对端连接。"""

    def __init__(
        self,
        config: PeerConnectionConfig | None = None,
        *,
        local_fingerprint: str = "",
        ice_ufrag: str = "",
        ice_pwd: str = "",
    ) -> None:
        self.config = config or PeerConnectionConfig()
        self.state = ConnectionState.NEW
        self.local_fingerprint = local_fingerprint
        self.ice_ufrag = ice_ufrag
        self.ice_pwd = ice_pwd

        self.ice = IceTransport()
        self.tracks: dict[str, MediaTrack] = {}
        self.data_channel: DataChannel | None = None
        self.remote_sdp: SdpSession | None = None
        self.local_sdp: SdpSession | None = None
        self._created_at = time.time()

    # ---- 协商 ----
    def create_offer(self) -> SdpSession:
        """生成 offer（只调用 ``sdp.py``，不碰字符串）。"""
        self.state = ConnectionState.CONNECTING
        self.ice.gather(self.config.stun_servers)

        from backend.security.dtls_srtp import DtlsSrtpParameters

        dtls = DtlsSrtpParameters(
            fingerprint=self.local_fingerprint or "sha-256 " + "0" * 95,
            setup="actpass",
            ice_ufrag=self.ice_ufrag,
            ice_pwd=self.ice_pwd,
        )
        session = build_offer(
            video_codecs={str(self.config.video_payload_type): "H264/90000"},
            audio_codecs={str(self.config.audio_payload_type): "opus/48000/2"},
            dtls=dtls,
            candidates=list(self.ice.agent.local),
        )
        self.local_sdp = session
        logger.info("已生成 offer: %s", session.describe())
        return session

    def set_remote_description(self, session: SdpSession) -> None:
        """记录对端 SDP 并启动连通性检查。"""
        self.remote_sdp = session
        pair = self.ice.start_checks(session)
        if pair is None:
            self.state = ConnectionState.FAILED
            raise NetworkError("ICE 未能建立连通", detail=self.ice.describe())
        self.state = ConnectionState.CONNECTED
        logger.info("PeerConnection 已连通: %s", self.ice.describe())

    def create_answer(self) -> SdpSession:
        if self.remote_sdp is None:
            raise NetworkError("收到 offer 之前不能生成 answer")
        from backend.security.dtls_srtp import DtlsSrtpParameters

        dtls = DtlsSrtpParameters(
            fingerprint=self.local_fingerprint or "sha-256 " + "0" * 95,
            setup="active",
            ice_ufrag=self.ice_ufrag,
            ice_pwd=self.ice_pwd,
        )
        session = build_answer(self.remote_sdp, dtls=dtls)
        self.ice.attach_local_candidates(session)
        self.local_sdp = session
        self.state = ConnectionState.CONNECTED
        return session

    # ---- 轨道 ----
    def add_track(self, mid: str = "0", codec=None) -> MediaTrack:
        from backend.media.stream import CodecId

        track = MediaTrack(
            payload_type=self.config.video_payload_type,
            ssrc=self.config.ssrc,
            codec=codec or CodecId.H264,
            mid=mid,
        )
        self.tracks[mid] = track
        logger.debug("新增轨道: %s", track.describe())
        return track

    def send_packet(self, packet: Packet, mid: str = "0") -> list[bytes]:
        track = self.tracks.get(mid)
        if track is None:
            raise NetworkError(f"轨道不存在: {mid}")
        return [rtp.pack() for rtp in track.send_packet(packet)]

    def receive_rtp(self, data: bytes, mid: str = "0") -> Packet | None:
        track = self.tracks.get(mid)
        if track is None:
            return None
        return track.receive_rtp(data)

    # ---- DataChannel ----
    def create_data_channel(self, config: DataChannelConfig | None = None) -> DataChannel:
        self.data_channel = DataChannel(config or self.config.data_channel)
        logger.debug("创建 DataChannel: %s", self.data_channel.describe())
        return self.data_channel

    # ---- 生命周期 ----
    def close(self) -> None:
        self.ice.stop()
        for track in self.tracks.values():
            track.close()
        self.tracks.clear()
        if self.data_channel is not None:
            self.data_channel.close()
            self.data_channel = None
        self.state = ConnectionState.CLOSED
        logger.info("PeerConnection 已关闭（存活 %.1fs）", time.time() - self._created_at)

    def describe(self) -> str:
        return (
            f"PeerConnection[{self.state}] tracks={len(self.tracks)} "
            f"ice={self.ice.state} uptime={time.time() - self._created_at:.1f}s"
        )
