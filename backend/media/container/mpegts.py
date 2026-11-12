"""MPEG-TS 封装与解封装。

只实现首个里程碑需要的子集：单节目、单路视频、无 PSI 描述符。
- 封装：``Packet`` -> PES -> 188 字节 TS 包（含 PAT/PMT/PCR）
- 解封装：TS 包 -> PES 重组 -> ``Packet``

TS 层的时间语义固定为 90kHz（``Timebase.mpegts()``），因此 packet 若来自别的
时间基，会在这里显式 rescale，而不是隐式假设。
"""

from __future__ import annotations

import struct
from typing import BinaryIO

from backend.config.schema import MuxConfig
from backend.errors.base import ElinkError, ErrorCode
from backend.logging.logger import get_logger
from backend.media.container.demuxer import Demuxer
from backend.media.container.muxer import Muxer
from backend.media.packet import Packet, is_keyframe
from backend.media.stream import CodecId, MediaType, StreamInfo
from backend.media.timebase import Timebase

logger = get_logger(__name__)

TS_PACKET_SIZE = 188
SYNC_BYTE = 0x47
NULL_PID = 0x1FFF

PAT_PID = 0x0000
VIDEO_STREAM_ID = 0xE0

STREAM_TYPE_H264 = 0x1B
STREAM_TYPE_HEVC = 0x24
STREAM_TYPE_AV1 = 0x06

_STREAM_TYPE_OF = {
    CodecId.H264: STREAM_TYPE_H264,
    CodecId.H265: STREAM_TYPE_HEVC,
    CodecId.AV1: STREAM_TYPE_AV1,
}
_CODEC_OF_STREAM_TYPE = {
    STREAM_TYPE_H264: CodecId.H264,
    STREAM_TYPE_HEVC: CodecId.H265,
    STREAM_TYPE_AV1: CodecId.AV1,
}

TS_TIMEBASE = Timebase.mpegts()


class MpegTsError(ElinkError):
    code = ErrorCode.INVALID_STATE


def _build_crc_table() -> list[int]:
    table = []
    for index in range(256):
        crc = index << 24
        for _ in range(8):
            if crc & 0x80000000:
                crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
            else:
                crc = (crc << 1) & 0xFFFFFFFF
        table.append(crc)
    return table


_CRC_TABLE = _build_crc_table()


def crc32_mpeg(data: bytes) -> int:
    """MPEG-2 系统层 CRC32（多项式 0x04C11DB7，初值全 1，不取反）。"""
    crc = 0xFFFFFFFF
    for byte in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC_TABLE[((crc >> 24) ^ byte) & 0xFF]
    return crc


def _build_section(table_id: int, body: bytes) -> bytes:
    section_length = len(body) + 4
    head = bytes(
        [table_id, 0xB0 | ((section_length >> 8) & 0x0F), section_length & 0xFF]
    )
    data = head + body
    return data + struct.pack(">I", crc32_mpeg(data))


def build_pat(
    transport_stream_id: int = 1, program_number: int = 1, pmt_pid: int = 0x1000
) -> bytes:
    body = (
        struct.pack(">H", transport_stream_id)
        + b"\xC1\x00\x00"
        + struct.pack(">HH", program_number, 0xE000 | pmt_pid)
    )
    return _build_section(0x00, body)


def build_pmt(
    program_number: int, pcr_pid: int, elementary: list[tuple[int, int]], descriptors: bytes = b""
) -> bytes:
    body = (
        struct.pack(">H", program_number)
        + b"\xC1\x00\x00"
        + struct.pack(">H", 0xE000 | pcr_pid)
        + struct.pack(">H", 0xF000)
    )
    for stream_type, pid in elementary:
        body += (bytes([stream_type]) + struct.pack(">H", 0xE000 | pid)
                 + struct.pack(">H", 0xF000 | len(descriptors)) + descriptors)
    return _build_section(0x02, body)


def _encode_timestamp(value: int, prefix: int) -> bytes:
    v = value & 0x1FFFFFFFF
    return bytes(
        [
            (prefix << 4) | (((v >> 30) & 0x07) << 1) | 0x01,
            (v >> 22) & 0xFF,
            (((v >> 15) & 0x7F) << 1) | 0x01,
            (v >> 7) & 0xFF,
            ((v & 0x7F) << 1) | 0x01,
        ]
    )


def _decode_timestamp(buf: bytes) -> int:
    return (
        (((buf[0] >> 1) & 0x07) << 30)
        | (buf[1] << 22)
        | (((buf[2] >> 1) & 0x7F) << 15)
        | (buf[3] << 7)
        | ((buf[4] >> 1) & 0x7F)
    )


def _encode_pcr(base: int) -> bytes:
    value = ((base & 0x1FFFFFFFF) << 15) | (0x3F << 9)
    return value.to_bytes(6, "big")


class MpegTsMuxer(Muxer):
    """MPEG-TS 封装器。"""

    format = "mpegts"

    def __init__(self, sink: BinaryIO, config: MuxConfig | None = None) -> None:
        super().__init__(sink)
        self.config = config or MuxConfig()
        self._pmt_pid = self.config.pmt_pid
        self._es_pid = 0x0100
        self._stream_type = STREAM_TYPE_H264
        self._continuity: dict[int, int] = {}
        self._bytes_written = 0
        self._last_psi_pts: int | None = None
        self._last_pcr: int | None = None
        self._pcr_interval = TS_TIMEBASE.ticks(self.config.pcr_interval_ms / 1000.0)

    @property
    def bytes_written(self) -> int:
        return self._bytes_written

    def open(self, streams: list[StreamInfo]) -> None:
        video = next((s for s in streams if s.media_type is MediaType.VIDEO), None)
        if video is None:
            raise MpegTsError("MPEG-TS 封装需要至少一路视频流")
        stream_type = _STREAM_TYPE_OF.get(video.codec)
        if stream_type is None:
            raise MpegTsError(
                f"MPEG-TS 暂不支持该编码: {video.codec.value}", detail=video.codec.value
            )

        self.streams = [video]
        self._stream_type = stream_type
        self._write_psi()
        self._opened = True

    def write(self, packet: Packet) -> int:
        if not self._opened:
            raise MpegTsError("封装器尚未打开")

        pts = packet.rescaled_pts(TS_TIMEBASE) or 0
        dts = packet.rescaled_dts(TS_TIMEBASE)
        if dts is None or dts > pts:
            dts = pts

        if self._last_psi_pts is None or pts - self._last_psi_pts >= TS_TIMEBASE.ticks(0.5):
            self._write_psi()
            self._last_psi_pts = pts

        pcr = None
        if self._last_pcr is None or dts - self._last_pcr >= self._pcr_interval:
            pcr = dts
            self._last_pcr = dts

        pes = self._build_pes(packet.data, pts, dts)
        before = self._bytes_written
        self._packetize(
            self._es_pid,
            pes,
            pusi=True,
            pcr=pcr,
            random_access=packet.keyframe,
        )
        return self._bytes_written - before

    def _write_psi(self) -> None:
        pat = build_pat(
            self.config.transport_stream_id,
            self.config.program_number,
            self._pmt_pid,
        )
        pmt = build_pmt(
            self.config.program_number, self._es_pid, [(self._stream_type, self._es_pid)],
            self._descriptors(),
        )
        self._packetize(PAT_PID, b"\x00" + pat, pusi=True)
        self._packetize(self._pmt_pid, b"\x00" + pmt, pusi=True)

    def _descriptors(self) -> bytes:
        stream = self.streams[0]
        payload = b"ELNK" + struct.pack(">HHf", stream.width, stream.height, stream.framerate)
        return (b"\x05\x04AV01" if stream.codec is CodecId.AV1 else b"") + bytes([0x80, len(payload)]) + payload

    def _build_pes(self, payload: bytes, pts: int, dts: int) -> bytes:
        if dts != pts:
            flags = 0xC0
            header = _encode_timestamp(pts, 0b0011) + _encode_timestamp(dts, 0b0001)
        else:
            flags = 0x80
            header = _encode_timestamp(pts, 0b0010)

        header_len = len(header)
        payload_len = 3 + header_len + len(payload)
        length_field = 0 if payload_len > 0xFFFF else payload_len
        return (
            b"\x00\x00\x01"
            + bytes([VIDEO_STREAM_ID])
            + struct.pack(">H", length_field)
            + bytes([0x80, flags, header_len])
            + header
            + payload
        )

    def _packetize(
        self,
        pid: int,
        payload: bytes,
        *,
        pusi: bool,
        pcr: int | None = None,
        random_access: bool = False,
    ) -> None:
        offset = 0
        total = len(payload)
        first = True
        while offset < total or first:
            remaining = total - offset
            af_flags = 0
            af_opts = b""
            if first:
                if pcr is not None:
                    af_flags |= 0x10
                    af_opts += _encode_pcr(pcr)
                if random_access:
                    af_flags |= 0x40
            need_af = bool(af_opts) or bool(af_flags) or remaining < 184

            cc = self._continuity.get(pid, 0)
            self._continuity[pid] = (cc + 1) & 0x0F

            header = bytes(
                [
                    SYNC_BYTE,
                    (0x40 if (pusi and first) else 0x00) | ((pid >> 8) & 0x1F),
                    pid & 0xFF,
                ]
            )

            if need_af:
                min_af = 1 + len(af_opts)
                if remaining + 1 + min_af <= 184:
                    af_len = 183 - remaining
                    take = remaining
                else:
                    af_len = min_af
                    take = 183 - af_len
                adaptation = (
                    bytes([af_len, af_flags])
                    + af_opts
                    + b"\xFF" * (af_len - 1 - len(af_opts))
                )
                packet = header + bytes([0x30 | cc]) + adaptation + payload[offset : offset + take]
            else:
                take = min(184, remaining)
                packet = header + bytes([0x10 | cc]) + payload[offset : offset + take]

            self._emit(packet)
            offset += take
            first = False

    def _emit(self, packet: bytes) -> None:
        view = memoryview(packet)
        # 管道/套接字上的 write 可能只写入一部分，必须写完剩余的字节，
        # 否则 TS 流会被截断成损坏码流。
        while view:
            written = self.sink.write(view)
            if not written:
                raise MpegTsError("写入字节流失败（sink 返回 0）")
            view = view[written:]
        self._bytes_written += len(packet)


class MpegTsDemuxer(Demuxer):
    """MPEG-TS 解封装器（单节目，按 PMT 取第一条可识别视频流）。"""

    format = "mpegts"

    #: 一次从 source 读取的字节数
    READ_SIZE = TS_PACKET_SIZE * 256

    def __init__(self, source: BinaryIO) -> None:
        super().__init__(source)
        self._buffer = bytearray()
        self._pmt_pid: int | None = None
        self._elementary: dict[int, int] = {}
        self._pes_buffers: dict[int, bytearray] = {}
        self._pes_headers: dict[int, tuple[int | None, int | None]] = {}
        self._queue: list[Packet] = []
        self._eof = False
        self._video_pid: int | None = None
        self._random_access: dict[int, bool] = {}

    def open(self) -> None:
        # TS 不需要预读头部，PAT/PMT 会随流出现；这里只重置状态。
        self._opened = True

    @property
    def video_pid(self) -> int | None:
        return self._video_pid

    def read(self) -> Packet | None:
        while not self._queue:
            if self._eof:
                self._flush_all_pes()
                if not self._queue:
                    return None
                break
            chunk = self.source.read(self.READ_SIZE)
            if not chunk:
                self._eof = True
                continue
            self._buffer += chunk
            self._parse_buffer()
        return self._queue.pop(0)

    # ---- TS 解析 ----
    def _parse_buffer(self) -> None:
        buf = self._buffer
        index = 0
        size = len(buf)
        while size - index >= TS_PACKET_SIZE:
            if buf[index] != SYNC_BYTE:
                nxt = buf.find(SYNC_BYTE, index + 1)
                if nxt < 0:
                    index = size
                    break
                index = nxt
                continue
            self._parse_packet(bytes(buf[index : index + TS_PACKET_SIZE]))
            index += TS_PACKET_SIZE
        del buf[:index]

    def _parse_packet(self, packet: bytes) -> None:
        pid = ((packet[1] & 0x1F) << 8) | packet[2]
        if pid == NULL_PID:
            return
        pusi = bool(packet[1] & 0x40)
        afc = (packet[3] >> 4) & 0x03
        if afc == 0:
            return

        offset = 4
        if afc & 0x02:
            offset = 5 + packet[4]
            if pusi and packet[4] > 0:
                random_access = bool(packet[5] & 0x40)
            else:
                random_access = False
        else:
            random_access = False
        if offset >= TS_PACKET_SIZE:
            return
        payload = packet[offset:]

        if pid == PAT_PID:
            self._parse_pat(payload)
        elif self._pmt_pid is not None and pid == self._pmt_pid:
            self._parse_pmt(payload)
        elif pid in self._elementary:
            self._handle_pes(pid, payload, pusi)
            if pusi:
                self._random_access[pid] = random_access

    def _parse_pat(self, payload: bytes) -> None:
        if len(payload) < 9 or payload[0] != 0x00:
            return
        section = payload[1:]
        if section[0] != 0x00:
            return
        section_length = ((section[1] & 0x0F) << 8) | section[2]
        end = min(len(section), 3 + section_length - 4)
        body_start = 8
        for pos in range(body_start, end - 3, 4):
            program = (section[pos] << 8) | section[pos + 1]
            pid = ((section[pos + 2] & 0x1F) << 8) | section[pos + 3]
            if program != 0:
                self._pmt_pid = pid
                return

    def _parse_pmt(self, payload: bytes) -> None:
        if len(payload) < 13 or payload[0] != 0x00:
            return
        section = payload[1:]
        if section[0] != 0x02:
            return
        section_length = ((section[1] & 0x0F) << 8) | section[2]
        end = min(len(section), 3 + section_length - 4)

        pos = 12 + (((section[10] & 0x0F) << 8) | section[11])
        while pos + 5 <= end:
            stream_type = section[pos]
            pid = ((section[pos + 1] & 0x1F) << 8) | section[pos + 2]
            es_info_len = ((section[pos + 3] & 0x0F) << 8) | section[pos + 4]
            info = section[pos + 5:pos + 5 + es_info_len]
            width = height = 0
            fps = 0.0
            av1 = False
            at = 0
            while at + 2 <= len(info):
                tag, size = info[at:at + 2]
                value = info[at + 2:at + 2 + size]
                if len(value) != size:
                    raise MpegTsError("无效流描述")
                if tag == 5 and value == b"AV01":
                    av1 = True
                if tag == 0x80 and len(value) == 12 and value[:4] == b"ELNK":
                    width, height, fps = struct.unpack(">HHf", value[4:])
                at += size + 2
            pos += 5 + es_info_len

            codec = _CODEC_OF_STREAM_TYPE.get(stream_type)
            if codec is None:
                continue
            if stream_type == STREAM_TYPE_AV1 and not av1:
                continue
            if pid in self._elementary:
                continue

            self._elementary[pid] = stream_type
            if self._video_pid is None:
                self._video_pid = pid
                self._streams.append(
                    StreamInfo(
                        stream_id=len(self._streams),
                        media_type=MediaType.VIDEO,
                        codec=codec,
                        timebase=TS_TIMEBASE,
                        width=width, height=height, framerate=fps,
                    )
                )
                logger.debug(
                    "解封装发现视频流: pid=0x%04X codec=%s", pid, codec.value
                )

    def _handle_pes(self, pid: int, payload: bytes, pusi: bool) -> None:
        if pusi:
            self._flush_pes(pid)
            header, body = self._split_pes_header(payload)
            self._pes_headers[pid] = header
            self._pes_buffers[pid] = bytearray(body)
        else:
            buffer = self._pes_buffers.get(pid)
            if buffer is None:
                return
            buffer += payload
            if len(buffer) > 32 * 1024 * 1024:
                raise MpegTsError("视频包过大")

    def _split_pes_header(self, payload: bytes) -> tuple[tuple[int | None, int | None], bytes]:
        if len(payload) < 9 or payload[:3] != b"\x00\x00\x01":
            return (None, None), payload

        flags = payload[7]
        header_len = payload[8]
        pts: int | None = None
        dts: int | None = None

        body_start = 9 + header_len
        timestamp_flags = (flags >> 6) & 0x03
        if timestamp_flags in (0b10, 0b11) and len(payload) >= 14:
            pts = _decode_timestamp(payload[9:14])
        if timestamp_flags == 0b11 and len(payload) >= 19:
            dts = _decode_timestamp(payload[14:19])

        return (pts, dts), payload[body_start:]

    def _flush_pes(self, pid: int) -> None:
        buffer = self._pes_buffers.pop(pid, None)
        if not buffer:
            self._pes_headers.pop(pid, None)
            return
        pts, dts = self._pes_headers.pop(pid, (None, None))
        codec = _CODEC_OF_STREAM_TYPE.get(self._elementary.get(pid, -1), CodecId.H264)
        data = bytes(buffer)
        self._queue.append(
            Packet(
                data=data,
                codec=codec,
                timebase=TS_TIMEBASE,
                pts=pts,
                dts=dts,
                stream_id=0,
                keyframe=self._random_access.pop(pid, False) or is_keyframe(data, codec=codec),
            )
        )

    def _flush_all_pes(self) -> None:
        for pid in list(self._pes_buffers):
            self._flush_pes(pid)
