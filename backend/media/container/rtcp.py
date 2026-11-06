"""RTCP 反馈（RFC 3550 的 SR / RR / SDES / BYE 最小子集）。

用到的是「接收侧汇报」：接收端周期性发 RR，把丢包率与累计丢包数告诉发送端，
``qos/congestion_control.py`` 据此降码率。当前不实现 XR / NACK / PLI——
它们属于重传与关键帧请求，等 QoS 层需要时再加。

报文格式提醒：RTCP 包必须按复合包（compound）发送，长度字段是「以 32 位字
为单位减一」，写错的表现是对端直接丢弃整个复合包。
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass, field

from backend.errors.codec import BitstreamError

RTCP_SR = 200
RTCP_RR = 201
RTCP_SDES = 202
RTCP_BYE = 203
RTCP_APP = 204

#: 建议的发送间隔下限（RFC 3550 的 5 秒 / 带宽 5% 的折中）
MIN_INTERVAL_S = 0.5


@dataclass
class ReportBlock:
    """RR 里的一个接收报告块。"""

    ssrc: int
    fraction_lost: int = 0  # 0~255，即 0~100% 的定点表示
    cumulative_lost: int = 0  # 24 位有符号
    highest_sequence: int = 0
    jitter: int = 0
    last_sr: int = 0
    delay_since_sr: int = 0

    def pack(self) -> bytes:
        return struct.pack(
            "!IIIIIIII",
            self.ssrc & 0xFFFFFFFF,
            ((self.fraction_lost & 0xFF) << 24) | (self.cumulative_lost & 0xFFFFFF),
            self.highest_sequence & 0xFFFFFFFF,
            self.jitter & 0xFFFFFFFF,
            self.last_sr & 0xFFFFFFFF,
            self.delay_since_sr & 0xFFFFFFFF,
            # 后两个字段是 RFC 的「扩展最高序号/扩展丢包」，这里填 0
            0,
            0,
        )

    @classmethod
    def unpack(cls, data: bytes) -> "ReportBlock":
        if len(data) < 24:
            raise BitstreamError("ReportBlock 长度不足")
        ssrc, fraction_cumulative, highest, jitter, last_sr, delay = struct.unpack(
            "!IIIIII", data[:24]
        )
        return cls(
            ssrc=ssrc,
            fraction_lost=(fraction_cumulative >> 24) & 0xFF,
            cumulative_lost=fraction_cumulative & 0xFFFFFF,
            highest_sequence=highest,
            jitter=jitter,
            last_sr=last_sr,
            delay_since_sr=delay,
        )


@dataclass
class RtcpPacket:
    """一条 RTCP 报文（复合包中的一段）。"""

    packet_type: int
    payload: bytes
    report_count: int = 0
    version: int = 2

    def pack(self) -> bytes:
        length = len(self.payload) // 4
        header = bytes(
            [
                (self.version << 6) | (self.report_count & 0x1F),
                self.packet_type & 0xFF,
            ]
        ) + struct.pack("!H", length & 0xFFFF)
        return header + self.payload

    @classmethod
    def unpack(cls, data: bytes) -> tuple["RtcpPacket", int]:
        """解析一条报文，返回 ``(报文, 消耗字节数)``。"""
        if len(data) < 4:
            raise BitstreamError("RTCP 报文长度不足")
        first, packet_type, length = struct.unpack("!BBH", data[:4])
        total = (length + 1) * 4
        if total > len(data):
            raise BitstreamError("RTCP 报文被截断", detail=f"{total} > {len(data)}")
        return (
            cls(
                packet_type=packet_type,
                payload=data[4:total],
                report_count=first & 0x1F,
                version=first >> 6,
            ),
            total,
        )


def build_receiver_report(
    reporter_ssrc: int, blocks: list[ReportBlock]
) -> list[RtcpPacket]:
    """构造 RR + SDES，返回可直接拼接发送的报文列表。"""
    body = struct.pack("!I", reporter_ssrc & 0xFFFFFFFF)
    for block in blocks:
        body += block.pack()

    rr = RtcpPacket(packet_type=RTCP_RR, payload=body, report_count=len(blocks))
    sdes = _build_sdes(reporter_ssrc, "elink")
    return [rr, sdes]


def _build_sdes(ssrc: int, cname: str) -> RtcpPacket:
    encoded = cname.encode("utf-8")[:255]
    chunk = struct.pack("!I", ssrc & 0xFFFFFFFF)
    chunk += bytes([1, len(encoded)]) + encoded + b"\x00"  # CNAME 项
    # 补齐到 32 位字边界
    if len(chunk) % 4:
        chunk += b"\x00" * (4 - len(chunk) % 4)
    return RtcpPacket(packet_type=RTCP_SDES, payload=chunk, report_count=1)


def build_bye(ssrc: int, reason: str = "") -> RtcpPacket:
    encoded = reason.encode("utf-8")[:255]
    payload = struct.pack("!I", ssrc & 0xFFFFFFFF)
    if encoded:
        payload += bytes([len(encoded)]) + encoded
        if len(payload) % 4:
            payload += b"\x00" * (4 - len(payload) % 4)
    return RtcpPacket(packet_type=RTCP_BYE, payload=payload, report_count=1)


def parse_compound(data: bytes) -> list[RtcpPacket]:
    """解析复合包（一条 UDP 报文里可能有多条 RTCP）。"""
    packets: list[RtcpPacket] = []
    offset = 0
    while offset < len(data):
        packet, consumed = RtcpPacket.unpack(data[offset:])
        packets.append(packet)
        offset += consumed
    return packets


@dataclass
class RtcpReporter:
    """按固定间隔生成接收报告（含丢包率）。"""

    local_ssrc: int
    interval_s: float = MIN_INTERVAL_S
    blocks: list[ReportBlock] = field(default_factory=list)
    _last_sent: float = field(default=0.0, repr=False)

    def update(self, block: ReportBlock) -> None:
        for index, existing in enumerate(self.blocks):
            if existing.ssrc == block.ssrc:
                self.blocks[index] = block
                return
        self.blocks.append(block)

    def due(self, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        return current - self._last_sent >= self.interval_s

    def build(self, now: float | None = None) -> bytes:
        current = time.monotonic() if now is None else now
        self._last_sent = current
        return b"".join(packet.pack() for packet in build_receiver_report(self.local_ssrc, self.blocks))
