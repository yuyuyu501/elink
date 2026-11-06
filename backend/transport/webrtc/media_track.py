"""WebRTC 媒体轨道。

一条轨道 = 「一个 SSRC + 一种编码 + 一组 RTP 序号」，负责：

- 发送：``Packet`` -> RTP 包（分片、marker、时间戳）；
- 接收：RTP 包 -> ``Packet``（重组 FU-A、按 marker 切 AU）；
- 统计：序号连续性、抖动（喂给 QoS 与 RTCP）。

传输细节（SRTP 加密、DTLS 握手）由底层栈负责，本层不感知。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from backend.logging.logger import get_logger
from backend.media.container.rtp import (
    H264RtpDepacketizer,
    H264RtpPacketizer,
    RtpPacket,
    RtpSessionState,
)
from backend.media.packet import Packet
from backend.media.stream import CodecId

logger = get_logger(__name__)


@dataclass
class TrackStats:
    packets_sent: int = 0
    bytes_sent: int = 0
    packets_received: int = 0
    packets_lost: int = 0
    frames_sent: int = 0
    frames_received: int = 0
    last_activity: float = field(default_factory=time.monotonic)

    def summary(self) -> str:
        return (
            f"发送 {self.frames_sent} 帧/{self.packets_sent} 包 "
            f"接收 {self.frames_received} 帧/{self.packets_received} 包 "
            f"丢 {self.packets_lost}"
        )


class MediaTrack:
    """一路媒体轨道（当前支持 H.264）。"""

    def __init__(
        self,
        *,
        payload_type: int = 96,
        ssrc: int = 0,
        codec: CodecId = CodecId.H264,
        mid: str = "0",
        max_payload: int = 1200,
    ) -> None:
        if codec is not CodecId.H264:
            logger.warning("轨道 %s 只实现了 H.264 的 RTP 打包，当前按 H.264 处理", codec.value)
        self.payload_type = payload_type
        self.ssrc = ssrc
        self.codec = codec
        self.mid = mid

        self.packetizer = H264RtpPacketizer(
            payload_type=payload_type, ssrc=ssrc, max_payload=max_payload
        )
        self.depacketizer = H264RtpDepacketizer(payload_type=payload_type)
        self.session = RtpSessionState()
        self.stats = TrackStats()

    # ---- 发送 ----
    def send_packet(self, packet: Packet) -> list[RtpPacket]:
        rtp_packets = self.packetizer.packetize(packet)
        self.stats.frames_sent += 1
        self.stats.packets_sent += len(rtp_packets)
        self.stats.bytes_sent += sum(item.size for item in rtp_packets)
        self.stats.last_activity = time.monotonic()
        return rtp_packets

    # ---- 接收 ----
    def receive_rtp(self, data: bytes) -> Packet | None:
        rtp = RtpPacket.unpack(data)
        self.session.observe(rtp.sequence, rtp.timestamp)
        self.stats.packets_received += 1
        self.stats.packets_lost = self.session.lost
        self.stats.last_activity = time.monotonic()

        packet = self.depacketizer.push(rtp)
        if packet is not None:
            self.stats.frames_received += 1
        return packet

    # ---- 统计 ----
    @property
    def loss_rate(self) -> float:
        return self.session.loss_rate

    @property
    def jitter_ms(self) -> float:
        """RTP 抖动的毫秒表示（时钟 90kHz）。"""
        return self.session.jitter / 90.0

    def describe(self) -> str:
        return f"track[{self.mid}] ssrc=0x{self.ssrc:08X} pt={self.payload_type} {self.stats.summary()}"

    def close(self) -> None:
        self.depacketizer.reset()
