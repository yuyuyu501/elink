"""RTP 打包 / 解包（RFC 3550 + RFC 6184 的 H.264 载荷）。

为什么自己实现：WebRTC 的媒体轨道要按 RTP 语义送包（时间戳 = 90kHz 采样时钟、
序号连续、marker 标记 AU 结尾），这些语义正是丢包统计与音视频同步的依据。
用现成库会把这些「契约」藏起来，出问题时无法定位。

分段策略（RFC 6184）：

- 小 NAL：单 NAL 包（类型即原始 NAL 类型）；
- 大 NAL：FU-A 分片，首片带 S 位、末片带 E 位，原始类型放在 FU header 里。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from backend.errors.codec import BitstreamError
from backend.media.packet import Packet, split_annexb
from backend.media.stream import CodecId
from backend.media.timebase import Timebase

RTP_VERSION = 2
#: 单包载荷上限：1500 MTU - 20 IP - 8 UDP - 12 RTP
MAX_PAYLOAD = 1200

NAL_FU_A = 28
NAL_STAP_A = 24
START_CODE4 = b"\x00\x00\x00\x01"


@dataclass
class RtpPacket:
    payload_type: int
    sequence: int
    timestamp: int
    ssrc: int
    payload: bytes
    marker: bool = False
    version: int = RTP_VERSION
    padding: bool = False
    extension: bool = False
    csrc: tuple[int, ...] = ()

    def pack(self) -> bytes:
        first = (
            (self.version << 6)
            | ((1 if self.padding else 0) << 5)
            | ((1 if self.extension else 0) << 4)
            | (len(self.csrc) & 0x0F)
        )
        second = ((1 if self.marker else 0) << 7) | (self.payload_type & 0x7F)
        header = struct.pack(
            "!BBHII",
            first,
            second,
            self.sequence & 0xFFFF,
            self.timestamp & 0xFFFFFFFF,
            self.ssrc & 0xFFFFFFFF,
        )
        for identifier in self.csrc:
            header += struct.pack("!I", identifier & 0xFFFFFFFF)
        return header + bytes(self.payload)

    @classmethod
    def unpack(cls, data: bytes) -> "RtpPacket":
        if len(data) < 12:
            raise BitstreamError("RTP 包长度不足", detail=str(len(data)))

        first, second, sequence, timestamp, ssrc = struct.unpack("!BBHII", data[:12])
        version = first >> 6
        if version != RTP_VERSION:
            raise BitstreamError(f"不支持的 RTP 版本: {version}")

        padding = bool(first & 0x20)
        extension = bool(first & 0x10)
        csrc_count = first & 0x0F
        offset = 12 + csrc_count * 4
        if offset > len(data):
            raise BitstreamError("RTP CSRC 越界")

        csrc = tuple(
            struct.unpack("!I", data[12 + index * 4 : 16 + index * 4])[0]
            for index in range(csrc_count)
        )

        if extension and offset + 4 <= len(data):
            extension_length = struct.unpack("!H", data[offset + 2 : offset + 4])[0]
            offset += 4 + extension_length * 4

        payload = data[offset:]
        if padding and payload:
            padding_length = payload[-1]
            if 0 < padding_length <= len(payload):
                payload = payload[:-padding_length]

        return cls(
            payload_type=second & 0x7F,
            sequence=sequence,
            timestamp=timestamp,
            ssrc=ssrc,
            payload=payload,
            marker=bool(second & 0x80),
            version=version,
            padding=padding,
            extension=extension,
            csrc=csrc,
        )

    @property
    def size(self) -> int:
        return len(self.pack())


class H264RtpPacketizer:
    """把一个 ``Packet``（一个 AU）切成若干 RTP 包。"""

    def __init__(
        self,
        payload_type: int = 96,
        ssrc: int = 0,
        max_payload: int = MAX_PAYLOAD,
    ) -> None:
        self.payload_type = payload_type
        self.ssrc = ssrc
        self.max_payload = max(2, max_payload)
        self._sequence = 0

    @property
    def next_sequence(self) -> int:
        return self._sequence

    def packetize(self, packet: Packet) -> list[RtpPacket]:
        timestamp = packet.rescaled_pts(Timebase.mpegts()) or 0
        nals = split_annexb(bytes(packet.data))
        if not nals:
            raise BitstreamError("Packet 中没有可打包的 NAL")

        output: list[RtpPacket] = []
        for nal_index, nal in enumerate(nals):
            is_last_nal = nal_index == len(nals) - 1
            if len(nal) <= self.max_payload:
                output.append(
                    self._build(nal, timestamp, marker=is_last_nal)
                )
                continue
            output.extend(self._fragment(nal, timestamp, marker=is_last_nal))
        return output

    def _build(self, payload: bytes, timestamp: int, marker: bool) -> RtpPacket:
        packet = RtpPacket(
            payload_type=self.payload_type,
            sequence=self._sequence & 0xFFFF,
            timestamp=timestamp,
            ssrc=self.ssrc,
            payload=payload,
            marker=marker,
        )
        self._sequence = (self._sequence + 1) & 0xFFFF
        return packet

    def _fragment(self, nal: bytes, timestamp: int, marker: bool) -> list[RtpPacket]:
        nal_type = nal[0] & 0x1F
        nri = nal[0] & 0x60
        header = bytes([nri | NAL_FU_A])
        body = nal[1:]

        chunk_size = self.max_payload - 2
        chunks = [body[index : index + chunk_size] for index in range(0, len(body), chunk_size)]

        fragments: list[RtpPacket] = []
        for index, chunk in enumerate(chunks):
            start = 0x80 if index == 0 else 0x00
            end = 0x40 if index == len(chunks) - 1 else 0x00
            fu_header = bytes([start | end | nal_type])
            is_last = index == len(chunks) - 1
            fragments.append(
                self._build(header + fu_header + chunk, timestamp, marker and is_last)
            )
        return fragments


class H264RtpDepacketizer:
    """把 RTP 包还原成 ``Packet``（以 marker 为 AU 边界）。"""

    def __init__(self, payload_type: int = 96) -> None:
        self.payload_type = payload_type
        self._buffer = bytearray()
        self._fragment: bytearray | None = None
        self._fragment_header = b""
        self._timestamp = 0

    def push(self, rtp: RtpPacket) -> Packet | None:
        if rtp.payload_type != self.payload_type:
            raise BitstreamError(
                "载荷类型不匹配", detail=f"{rtp.payload_type} != {self.payload_type}"
            )

        self._timestamp = rtp.timestamp
        payload = bytes(rtp.payload)
        if not payload:
            return None

        nal_type = payload[0] & 0x1F
        if nal_type == NAL_FU_A:
            if len(payload) < 2:
                raise BitstreamError("FU-A 载荷过短")
            fu_header = payload[1]
            start = bool(fu_header & 0x80)
            end = bool(fu_header & 0x40)
            original_type = fu_header & 0x1F

            if start:
                self._fragment_header = bytes([(payload[0] & 0xE0) | original_type])
                self._fragment = bytearray(payload[2:])
            elif self._fragment is not None:
                self._fragment += payload[2:]
            else:
                raise BitstreamError("FU-A 分片乱序：收到续片但没有首片")

            if end:
                if self._fragment is not None:
                    self._buffer += START_CODE4 + self._fragment_header + bytes(self._fragment)
                self._fragment = None
        elif nal_type == NAL_STAP_A:
            offset = 1
            while offset + 2 <= len(payload):
                size = struct.unpack("!H", payload[offset : offset + 2])[0]
                offset += 2
                nal = payload[offset : offset + size]
                offset += size
                self._buffer += START_CODE4 + nal
        else:
            self._buffer += START_CODE4 + payload

        if not rtp.marker:
            return None

        if not self._buffer:
            return None

        data = bytes(self._buffer)
        self._buffer.clear()
        return Packet(
            data=data,
            codec=CodecId.H264,
            timebase=Timebase.mpegts(),
            pts=rtp.timestamp,
            dts=rtp.timestamp,
        )

    def reset(self) -> None:
        self._buffer.clear()
        self._fragment = None


def packetize_video(codec, data, limit=1200):
    """Packetize HEVC without DONL, or AV1 with one OBU element per packet."""
    if codec == "hevc":
        for nal in split_annexb(data):
            if len(nal) < 2:
                raise ValueError("Invalid HEVC NAL")
            if len(nal) <= limit:
                yield nal
                continue
            header = bytes(((nal[0] & 0x81) | 98, nal[1]))
            body = nal[2:]
            step = limit - 3
            for offset in range(0, len(body), step):
                flags = (0x80 if offset == 0 else 0) | (0x40 if offset + step >= len(body) else 0)
                yield header + bytes((flags | ((nal[0] >> 1) & 63),)) + body[offset:offset + step]
    else:
        from backend.media.video.codec.framing import sized_av1_obus
        for kind, body, raw in sized_av1_obus(data):
            if kind in (2, 8, 15):
                continue
            header_size = 2 if raw[0] & 4 else 1
            obu = bytes((raw[0] & ~2,)) + raw[1:header_size] + body
            step = limit - 1
            for offset in range(0, len(obu), step):
                flags = ((0x80 if offset else 0) | (0x40 if offset + step < len(obu) else 0)
                         | (8 if kind == 1 and not offset else 0))
                yield bytes((0x10 | flags,)) + obu[offset:offset + step]


def depacketize_video(codec, data):
    """Convert complete jitter-buffer access units with length-prefixed RTP payloads."""
    from backend.media.video.codec.framing import MAX_UNIT

    def leb(data, offset):
        value = 0
        for shift in range(0, 56, 7):
            if offset >= len(data):
                break
            byte = data[offset]
            offset += 1
            value |= (byte & 127) << shift
            if not byte & 128:
                return value, offset
        raise ValueError("Invalid AV1 OBU length")

    out = bytearray()
    fragment = None
    offset = 0
    if len(data) > MAX_UNIT:
        raise ValueError("RTP access unit too large")
    while offset < len(data):
        if len(data) - offset < 2:
            raise ValueError("Missing RTP payload length")
        size = int.from_bytes(data[offset:offset + 2], "big")
        offset += 2
        if not size or offset + size > len(data):
            raise ValueError("Invalid RTP payload length")
        payload = data[offset:offset + size]
        offset += size
        if codec == "hevc":
            if len(payload) < 2:
                raise ValueError("Truncated HEVC payload")
            kind = (payload[0] >> 1) & 63
            if kind == 49 and len(payload) > 3:
                flags = payload[2]
                start, end = bool(flags & 0x80), bool(flags & 0x40)
                if start == (fragment is not None) or (start and end):
                    raise ValueError("Invalid HEVC fragment")
                if start:
                    fragment = bytearray((payload[0] & 0x81 | ((flags & 63) << 1), payload[1]))
                fragment.extend(payload[3:])
                if end:
                    out.extend(b"\x00\x00\x00\x01" + fragment)
                    fragment = None
            elif kind < 48 and fragment is None:
                out.extend(b"\x00\x00\x00\x01" + payload)
            else:
                raise ValueError("Unsupported HEVC RTP payload")
        else:
            flags, pos = payload[0], 1
            count = flags >> 4 & 3
            index = 0
            while pos < len(payload):
                index += 1
                if index == count:
                    end = len(payload)
                else:
                    size, pos = leb(payload, pos)
                    end = pos + size
                if not pos < end <= len(payload):
                    raise ValueError("Invalid AV1 RTP element")
                if bool(index == 1 and flags & 0x80) != (fragment is not None):
                    raise ValueError("Invalid AV1 RTP fragment")
                if fragment is None:
                    fragment = bytearray()
                fragment.extend(payload[pos:end])
                pos = end
                if pos == len(payload) and flags & 0x40:
                    continue
                header_size = 2 if fragment[0] & 4 else 1
                if len(fragment) < header_size or fragment[0] & 0x83:
                    raise ValueError("Invalid AV1 OBU header")
                out.append(fragment[0] | 2)
                out.extend(fragment[1:header_size])
                size = len(fragment) - header_size
                while size >= 128:
                    out.append(size & 127 | 128)
                    size >>= 7
                out.append(size)
                out.extend(fragment[header_size:])
                fragment = None
            if not index or count and index != count:
                raise ValueError("Invalid AV1 RTP element count")
    if fragment is not None or not out:
        raise ValueError("Incomplete RTP access unit")
    return bytes(out)


@dataclass
class RtpSessionState:
    """对端 RTP 会话的轻量状态（序号/时间戳连续性统计）。"""

    received: int = 0
    expected_sequence: int | None = None
    lost: int = 0
    jitter: float = 0.0
    last_transit: float = 0.0
    clock_rate: int = 90_000
    _lock: object = field(default=None, repr=False)

    def observe(self, sequence: int, timestamp: int) -> None:
        self.received += 1
        if self.expected_sequence is None:
            self.expected_sequence = (sequence + 1) & 0xFFFF
            return

        if sequence == self.expected_sequence:
            self.expected_sequence = (sequence + 1) & 0xFFFF
        elif ((sequence - self.expected_sequence) & 0xFFFF) < 0x8000:
            self.lost += ((sequence - self.expected_sequence) & 0xFFFF)
            self.expected_sequence = (sequence + 1) & 0xFFFF
        else:
            self.received -= 1  # 迟到包，不计入

        transit = float(timestamp)
        if self.last_transit:
            delta = abs(transit - self.last_transit)
            self.jitter += (delta - self.jitter) / 16.0
        self.last_transit = transit

    @property
    def loss_rate(self) -> float:
        total = self.received + self.lost
        return self.lost / total if total else 0.0
