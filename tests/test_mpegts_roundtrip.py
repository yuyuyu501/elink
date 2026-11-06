"""契约回归测试：编码器输出 -> MPEG-TS 封装 -> 解封装 -> 解码。

不依赖 pytest，可直接运行：

    python tests/test_mpegts_roundtrip.py

覆盖两个曾真实出现过的缺陷：
1. Annex-B 解析在 EOF 漏交付最后一个 NAL（末帧损坏）；
2. 封装写管道时的短写（TS 被截断）。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config.schema import MuxConfig, VideoEncodeConfig  # noqa: E402
from backend.media.container.mpegts import MpegTsDemuxer, MpegTsMuxer  # noqa: E402
from backend.media.frame import Frame, PixelFormat  # noqa: E402
from backend.media.stream import CodecId, MediaType, StreamInfo  # noqa: E402
from backend.media.timebase import Timebase  # noqa: E402
from backend.media.video.codec.decoding.h264.h264 import H264Decoder  # noqa: E402
from backend.media.video.codec.encoding.h264.libx264 import Libx264Encoder  # noqa: E402

WIDTH, HEIGHT, FPS, FRAMES = 320, 240, 30, 30
TIMEBASE = Timebase.mpegts()


def _make_frame(index: int) -> Frame:
    row = bytearray(WIDTH * 4)
    for x in range(WIDTH):
        value = (x + index * 4) % 256
        row[x * 4 : x * 4 + 4] = bytes([value, (value * 2) % 256, (value * 3) % 256, 255])
    return Frame(
        data=bytes(row) * HEIGHT,
        timebase=TIMEBASE,
        pts=TIMEBASE.ticks(index / FPS),
        width=WIDTH,
        height=HEIGHT,
        pixel_format=PixelFormat.BGRA,
    )


def _stream() -> StreamInfo:
    return StreamInfo(
        media_type=MediaType.VIDEO,
        codec=CodecId.H264,
        timebase=TIMEBASE,
        width=WIDTH,
        height=HEIGHT,
        framerate=FPS,
        metadata={"pixel_format": "bgra"},
    )


def _encode() -> list:
    encoder = Libx264Encoder(_stream(), VideoEncodeConfig(bitrate_kbps=1000, gop=15))
    encoder.open()
    packets = []
    for index in range(FRAMES):
        packets.extend(encoder.encode(_make_frame(index)))
    packets.extend(encoder.flush())
    encoder.close()
    return packets


def test_encoder_emits_one_packet_per_frame() -> None:
    packets = _encode()
    assert len(packets) == FRAMES, f"包数 {len(packets)} != 帧数 {FRAMES}"
    assert packets[0].keyframe, "首包应为关键帧"
    assert all(packet.pts is not None for packet in packets)
    pts_values = [packet.pts for packet in packets]
    assert pts_values == sorted(pts_values), "PTS 必须单调递增"


def test_mpegts_roundtrip_is_lossless(tmp_path: Path | None = None) -> None:
    packets = _encode()
    out_dir = Path(tmp_path or Path("out/test"))
    out_dir.mkdir(parents=True, exist_ok=True)
    ts_path = out_dir / "roundtrip.ts"

    with ts_path.open("wb") as sink:
        muxer = MpegTsMuxer(sink, MuxConfig())
        muxer.open([_stream()])
        for packet in packets:
            muxer.write(packet)
        muxer.close()

    with ts_path.open("rb") as source:
        demuxer = MpegTsDemuxer(source)
        demuxer.open()
        read_back = []
        while True:
            packet = demuxer.read()
            if packet is None:
                break
            read_back.append(packet)
        demuxer.close()

    assert len(read_back) == len(packets), f"回读 {len(read_back)} != 写出 {len(packets)}"
    for index, (original, restored) in enumerate(zip(packets, read_back)):
        assert original.data == restored.data, f"第 {index} 个包负载不一致"
        assert original.pts == restored.pts, f"第 {index} 个包 PTS 不一致"


def test_decode_yields_all_frames() -> None:
    packets = _encode()
    decoder = H264Decoder(_stream())
    decoder.open()
    frames = []
    for packet in packets:
        frames.extend(decoder.decode(packet))
    frames.extend(decoder.flush())
    decoder.close()
    assert len(frames) == FRAMES, f"解出 {len(frames)} 帧 != {FRAMES}"
    assert frames[0].width == WIDTH and frames[0].height == HEIGHT


def test_ffmpeg_accepts_our_ts() -> None:
    """让 ffmpeg 独立解码我们封装的 TS，确保没有损坏帧。"""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        print("跳过：未安装 ffmpeg")
        return

    packets = _encode()
    out_dir = Path("out/test")
    out_dir.mkdir(parents=True, exist_ok=True)
    ts_path = out_dir / "verify.ts"
    with ts_path.open("wb") as sink:
        muxer = MpegTsMuxer(sink, MuxConfig())
        muxer.open([_stream()])
        for packet in packets:
            muxer.write(packet)
        muxer.close()

    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "warning", "-i", str(ts_path), "-f", "null", "-"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "corrupt" not in proc.stderr.lower(), proc.stderr


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
