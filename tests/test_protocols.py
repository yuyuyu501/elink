"""协议层回归测试：RTP / FEC / 抖动缓冲 / SDP / 控制帧。

不依赖 pytest，可直接运行：

    python tests/test_protocols.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.control_channel import protocol  # noqa: E402
from backend.media.container.rtp import (  # noqa: E402
    H264RtpDepacketizer,
    H264RtpPacketizer,
)
from backend.media.packet import Packet  # noqa: E402
from backend.media.stream import CodecId  # noqa: E402
from backend.media.sync.jitter_buffer import JitterBuffer  # noqa: E402
from backend.media.timebase import Timebase  # noqa: E402
from backend.qos.fec import XorFecDecoder, XorFecEncoder  # noqa: E402
from backend.signaling.sdp import SdpSession, build_answer, build_offer, negotiate_codecs  # noqa: E402


def _nal(nal_type: int, size: int) -> bytes:
    """构造一段 NAL 负载（避免以 0x00 结尾：Annex-B 会把尾部零当作对齐字节剥离）。"""
    return bytes([0x60 | nal_type]) + (bytes(range(1, 256)) * (size // 255 + 1))[: size - 1]


def test_rtp_roundtrip_small_and_fragmented() -> None:
    """小 NAL 走单包、大 NAL 走 FU-A，两者都要能还原。"""
    timebase = Timebase.mpegts()
    small = b"\x00\x00\x00\x01" + _nal(5, 300)
    large = b"\x00\x00\x00\x01" + _nal(1, 5000)
    packet = Packet(data=small + large, codec=CodecId.H264, timebase=timebase, pts=90000)

    packetizer = H264RtpPacketizer(payload_type=96, ssrc=0x11223344, max_payload=1200)
    rtp_packets = packetizer.packetize(packet)
    assert len(rtp_packets) > 2, f"大 NAL 应被分片，实际只产生 {len(rtp_packets)} 包"
    assert rtp_packets[-1].marker, "最后一个 RTP 包必须带 marker"

    depacketizer = H264RtpDepacketizer(payload_type=96)
    restored = None
    for rtp in rtp_packets:
        restored = depacketizer.push(rtp)
    assert restored is not None, "没有还原出 AU"
    assert restored.data == packet.data, "RTP 还原的码流与原始不一致"
    assert restored.pts == 90000


def test_rtp_unpack_roundtrip() -> None:
    from backend.media.container.rtp import RtpPacket

    original = RtpPacket(
        payload_type=96, sequence=65535, timestamp=123456, ssrc=0xDEADBEEF, payload=b"hello"
    )
    parsed = RtpPacket.unpack(original.pack())
    assert parsed.sequence == 65535
    assert parsed.payload == b"hello"
    assert parsed.marker == original.marker


def test_fec_recovers_single_loss() -> None:
    encoder = XorFecEncoder(block_size=4)
    packets = [f"packet-{index}".encode() + bytes([index]) * 10 for index in range(4)]
    parity = None
    for sequence, payload in enumerate(packets):
        result = encoder.push(sequence, payload)
        if result is not None:
            parity = result
    assert parity is not None, "组满后应产出校验包"

    decoder = XorFecDecoder(block_size=4)
    for sequence, payload in enumerate(packets):
        if sequence == 2:  # 假设第 3 个包丢了
            continue
        decoder.push_data(sequence, payload)

    recovered = decoder.push_parity(parity)
    assert recovered is not None, "FEC 应能恢复单个丢包"
    assert recovered[0] == 2
    assert recovered[1] == packets[2], "恢复的数据与原数据不一致"


def test_jitter_buffer_orders_packets() -> None:
    buffer = JitterBuffer(capacity=16, min_delay_ms=0.0, max_delay_ms=0.0)
    for sequence in (2, 0, 3, 1):
        buffer.push(sequence, sequence * 3000, bytes([sequence]))

    order = []
    deadline = time.monotonic() + 2.0
    while len(order) < 4 and time.monotonic() < deadline:
        item = buffer.pop_ready()
        if item is None:
            time.sleep(0.01)
            continue
        order.append(item.sequence)
    assert order == [0, 1, 2, 3], f"抖动缓冲输出顺序错误: {order}"


def test_sdp_roundtrip_and_negotiation() -> None:
    from backend.security.dtls_srtp import DtlsSrtpParameters

    dtls = DtlsSrtpParameters(fingerprint="sha-256 " + ":".join(["AB"] * 32), setup="actpass")
    offer = build_offer(dtls=dtls)
    text = offer.to_string()
    assert text.startswith("v=0\r\n"), "SDP 必须以 v=0 开头"
    assert "m=video" in text and "m=audio" in text
    assert "a=ice-ufrag:" in text

    parsed = SdpSession.parse(text)
    assert len(parsed.media) == 2
    video = parsed.media_of("video")
    assert video is not None
    assert "H264" in "".join(video.rtpmap.values())

    answer = build_answer(parsed)
    common = negotiate_codecs(parsed, answer, "video")
    assert common == ["H264"], common

    assert parsed.dtls_parameters() is not None, "应能读出 DTLS 参数"


def test_control_frame_streaming() -> None:
    event = protocol.build_mouse_move(100, 200)
    data = protocol.encode_json(event)

    decoder = protocol.FrameDecoder()
    frames = []
    for index in range(0, len(data), 3):  # 故意切成 3 字节的小块，模拟 TCP 粘包
        frames.extend(decoder.push(data[index : index + 3]))
    assert len(frames) == 1, f"应恰好解析出 1 帧，实际 {len(frames)}"

    frame_type, payload = frames[0]
    assert frame_type is protocol.FrameType.JSON
    decoded = protocol.decode_json(payload)
    assert isinstance(decoded, protocol.InputEvent)
    assert decoded.x == 100 and decoded.y == 200


def test_control_frame_resync() -> None:
    """前导脏字节不应导致整条流失效。"""
    event = protocol.build_key_event(65, True)
    data = protocol.encode_json(event)
    decoder = protocol.FrameDecoder()
    frames = decoder.push(b"\x00\xff" + data)
    assert len(frames) == 1
    assert decoder.dropped == 2


def main() -> int:
    failures = 0
    for name, func in sorted(globals().items()):
        if not name.startswith("test_") or not callable(func):
            continue
        try:
            func()
        except AssertionError as exc:
            failures += 1
            print(f"[FAIL] {name}: {exc}")
        else:
            print(f"[ OK ] {name}")
    print(f"\n{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
