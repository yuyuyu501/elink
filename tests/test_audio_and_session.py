"""音频链路、控制通道、会话协商的回归测试。

不依赖 pytest，可直接运行：

    python tests/test_audio_and_session.py
"""

from __future__ import annotations

import array
import math
import shutil
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.control_channel import protocol  # noqa: E402
from backend.control_channel.tcp import TcpControlChannel  # noqa: E402
from backend.media.audio.codec.decoding.aac import AacDecoder  # noqa: E402
from backend.media.audio.codec.encoding.aac import AacEncoder  # noqa: E402
from backend.media.audio.filter.gain import GainFilter  # noqa: E402
from backend.media.audio.filter.resampler import Resampler  # noqa: E402
from backend.media.audio.filter.volume import VolumeFilter  # noqa: E402
from backend.media.audio.processing.agc import AutomaticGainControl  # noqa: E402
from backend.media.audio.processing.noise_suppression import NoiseSuppressor  # noqa: E402
from backend.media.device.display import list_displays  # noqa: E402
from backend.media.device.enumerate import DeviceKind, enumerate_devices  # noqa: E402
from backend.media.frame import Frame  # noqa: E402
from backend.media.stream import CodecId, MediaType, StreamInfo  # noqa: E402
from backend.media.timebase import Timebase  # noqa: E402
from backend.pipeline.audio_pipeline import AudioPipeline  # noqa: E402
from backend.registry.codec_registry import build_default_registry  # noqa: E402
from backend.session.manager import Capabilities, SessionManager  # noqa: E402
from backend.session.state import SessionState, SessionStateMachine  # noqa: E402

SAMPLE_RATE = 48_000
CHANNELS = 1
FRAME_SAMPLES = 960  # 20ms


def _tone_frame(index: int) -> Frame:
    samples = array.array("h")
    for n in range(FRAME_SAMPLES):
        value = int(4000 * math.sin(2 * math.pi * 440 * (index * FRAME_SAMPLES + n) / SAMPLE_RATE))
        samples.append(value)
    return Frame(
        data=samples.tobytes(),
        timebase=Timebase.microseconds(),
        pts=Timebase.microseconds().ticks(index * FRAME_SAMPLES / SAMPLE_RATE),
        media_type=MediaType.AUDIO,
        sample_rate=SAMPLE_RATE,
        channels=CHANNELS,
        sample_format="s16",
        samples=FRAME_SAMPLES,
    )


def _audio_stream(codec: CodecId) -> StreamInfo:
    return StreamInfo(
        media_type=MediaType.AUDIO,
        codec=codec,
        timebase=Timebase.microseconds(),
        sample_rate=SAMPLE_RATE,
        channels=CHANNELS,
    )


def test_audio_filters() -> None:
    frame = _tone_frame(0)

    half = VolumeFilter(volume=0.5).process(frame)
    original_peak = max(abs(value) for value in array.array("h", frame.data))
    half_peak = max(abs(value) for value in array.array("h", half.data))
    assert half_peak < original_peak, "音量 0.5 应降低峰值"

    quieter = GainFilter(gain_db=-6.0).process(frame)
    quieter_peak = max(abs(value) for value in array.array("h", quieter.data))
    assert quieter_peak < original_peak, "-6dB 应降低电平"

    resampled = Resampler(target_rate=16_000).process(frame)
    assert resampled.sample_rate == 16_000
    assert resampled.samples == int(FRAME_SAMPLES * 16_000 / SAMPLE_RATE)

    suppressed = NoiseSuppressor().process(frame)
    assert len(suppressed.data) == len(frame.data)

    amplified = AutomaticGainControl().process(frame)
    assert len(amplified.data) == len(frame.data)


def test_aac_roundtrip() -> None:
    if not shutil.which("ffmpeg"):
        print("跳过：未安装 ffmpeg")
        return

    stream = _audio_stream(CodecId.AAC)
    encoder = AacEncoder(stream, bitrate_kbps=64)
    encoder.open()
    packets = []
    for index in range(20):
        packets.extend(encoder.encode(_tone_frame(index)))
    packets.extend(encoder.flush())
    encoder.close()

    assert packets, "AAC 编码没有产出任何包"
    assert all(packet.pts is not None for packet in packets)
    pts_values = [packet.pts for packet in packets]
    assert pts_values == sorted(pts_values), "音频 PTS 必须单调递增"

    decoder = AacDecoder(stream)
    decoder.open()
    frames = []
    for packet in packets:
        frames.extend(decoder.decode(packet))
    frames.extend(decoder.flush())
    decoder.close()

    assert len(frames) >= len(packets) - 2, f"解码帧数异常: {len(frames)} vs {len(packets)}"
    assert frames[0].sample_rate == SAMPLE_RATE
    assert frames[0].sample_format == "s16"


def test_audio_pipeline_with_fake_capture() -> None:
    if not shutil.which("ffmpeg"):
        print("跳过：未安装 ffmpeg")
        return

    class FakeCapture:
        def __init__(self) -> None:
            self.index = 0
            self.closed = False

        def open(self) -> None:
            pass

        def read(self) -> Frame | None:
            if self.index >= 15:
                return None  # 结束信号
            frame = _tone_frame(self.index)
            self.index += 1
            time.sleep(0.002)
            return frame

        def close(self) -> None:
            self.closed = True

    capture = FakeCapture()
    encoder = AacEncoder(_audio_stream(CodecId.AAC), bitrate_kbps=64)
    outputs: list = []

    pipeline = AudioPipeline(capture, encoder, sink=lambda packet: outputs.append(packet) or len(packet.data))
    stats = pipeline.run()

    assert stats.frames_captured == 15, f"采集帧数 {stats.frames_captured}"
    assert outputs, "管线没有输出任何音频包"
    assert capture.closed, "管线结束后应关闭采集"


def test_session_negotiation() -> None:
    registry = build_default_registry()
    manager = SessionManager(registry=registry, session_id="test-session")

    remote = Capabilities(codecs=("h264", "hevc"), max_width=1280, max_height=720, max_fps=30)
    negotiated = manager.negotiate(remote, preferred_size=(1920, 1080), preferred_fps=60)

    assert negotiated.width <= 1280 and negotiated.height <= 720
    assert negotiated.fps <= 30
    assert negotiated.encoder in registry.encoder_names(negotiated.codec.value)

    manager.open()
    manager.streaming()
    assert manager.state.state is SessionState.STREAMING
    manager.on_disconnected("网络抖动")
    assert manager.state.state is SessionState.RECONNECTING
    manager.close()
    assert manager.state.state is SessionState.CLOSED


def test_state_machine_rejects_invalid_transition() -> None:
    machine = SessionStateMachine()
    machine.transition(SessionState.CONNECTING)
    try:
        machine.transition(SessionState.STREAMING)  # 必须先 negotiating
    except Exception as exc:
        assert "非法会话状态迁移" in str(exc)
    else:
        raise AssertionError("非法迁移没有被拒绝")


def test_control_channel_over_tcp() -> None:
    port = 17823
    received: list = []
    server = TcpControlChannel("127.0.0.1", port, server_side=True, timeout_s=5)
    accept_thread = threading.Thread(target=server.open, daemon=True)
    accept_thread.start()
    time.sleep(0.3)

    client = TcpControlChannel("127.0.0.1", port, timeout_s=5)
    client.open()
    accept_thread.join(timeout=5)

    server.on_message(lambda message: received.append(message))
    client.send_event(protocol.build_mouse_move(320, 240))
    client.send_command(protocol.ControlCommand(name="clipboard_sync", args={"size": 12}))

    deadline = time.monotonic() + 3
    while len(received) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)

    client.close()
    server.close()

    assert len(received) == 2, f"控制通道收到 {len(received)} 条消息"
    assert isinstance(received[0], protocol.InputEvent)
    assert received[0].x == 320 and received[0].y == 240
    assert isinstance(received[1], protocol.ControlCommand)
    assert received[1].name == "clipboard_sync"


def test_device_enumeration() -> None:
    displays = list_displays()
    assert displays, "至少应枚举出一台显示器"
    assert displays[0].width > 0 and displays[0].height > 0

    devices = enumerate_devices(DeviceKind.DISPLAY)
    assert devices, "设备枚举应包含显示器"
    assert devices[0].kind is DeviceKind.DISPLAY


def test_websocket_channel_roundtrip() -> None:
    """WS 通道必须正确拆出协议帧。

    回归点：早期实现把 WebSocket message 直接当成 JSON 负载，实际内容是
    「8 字节协议帧头 + JSON」，结果一收到消息就解析失败并把连接打死。
    """
    import socket
    import threading

    from backend.control_channel.websocket import (
        WebSocketControlChannel,
        WebSocketServerChannel,
    )

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    received: list = []
    holder: dict = {}

    def serve() -> None:
        conn, _address = listener.accept()
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                return
            data += chunk

        key = ""
        for line in data.decode("latin-1").split("\r\n"):
            if line.lower().startswith("sec-websocket-key:"):
                key = line.split(":", 1)[1].strip()

        channel = WebSocketServerChannel()
        channel.on_message(lambda message: received.append(message))
        channel.accept(conn, key, peer="test")
        holder["channel"] = channel

    threading.Thread(target=serve, daemon=True).start()

    client = WebSocketControlChannel("127.0.0.1", port, timeout_s=5)
    client.open()
    client.send_command(protocol.ControlCommand(name="hello", args={"n": 1}))

    deadline = time.monotonic() + 3
    while not received and time.monotonic() < deadline:
        time.sleep(0.02)

    client.close()
    if holder.get("channel") is not None:
        holder["channel"].close()
    listener.close()

    assert received, "服务端没有收到任何控制消息"
    assert isinstance(received[0], protocol.ControlCommand)
    assert received[0].name == "hello"
    assert received[0].args.get("n") == 1


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
        except Exception as exc:  # 未预期异常也算失败，但要打出来
            failures += 1
            print(f"[ERR ] {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"[ OK ] {name}")
    print(f"\n{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
