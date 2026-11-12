"""编解码能力探测。

运行时探测而不是硬编码：同一份代码在不同机器上可用后端不同。
这里只回答「本机 ffmpeg 提供了哪些编解码器」，选型逻辑在
``registry/codec_registry.py``，pipeline 不感知。
"""

from __future__ import annotations

import functools
import subprocess
import sys
import base64
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from backend.logging.logger import get_logger

logger = get_logger(__name__)


def resolve_ffmpeg_path(ffmpeg_path: str = "ffmpeg") -> str:
    """Frozen applications use their own dependency, including with older saved settings."""
    if getattr(sys, "frozen", False) and sys.platform == "win32":
        return str(Path(sys.executable).parent / "ffmpeg" / "ffmpeg.exe")
    return ffmpeg_path

@dataclass
class ProbeResult:
    """一次探测的结果快照。"""

    ffmpeg_path: str = "ffmpeg"
    version: str = ""
    encoders: set[str] = field(default_factory=set)
    decoders: set[str] = field(default_factory=set)
    error: str | None = None
    hwaccels: set[str] = field(default_factory=set)

    @property
    def available(self) -> bool:
        return self.error is None

    def has_encoder(self, name: str) -> bool:
        return name in self.encoders

    def has_decoder(self, name: str) -> bool:
        return name in self.decoders

    def describe(self) -> str:
        if not self.available:
            return f"ffmpeg 不可用: {self.error}"
        video = sorted(name for name in VIDEO_ENCODERS if name in self.encoders)
        return f"ffmpeg {self.version}; 视频编码接口: {', '.join(video) or '无'}"


def _list_codecs(ffmpeg: str, flag: str) -> tuple[set[str], str]:
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", flag],
        capture_output=True,
        text=True,
        timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"{flag} 返回 {proc.returncode}")

    names: set[str] = set()
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[0].startswith("-"):
            continue
        flags, name = parts[0], parts[1]
        if len(flags) != 6:
            continue
        names.add(name)
    return names, proc.stdout


def _read_version(ffmpeg: str) -> str:
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-version"],
        capture_output=True,
        text=True,
        timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    first = proc.stdout.splitlines()[0] if proc.stdout else ""
    return first.replace("ffmpeg version ", "").split(" Copyright")[0].strip()


@functools.lru_cache(maxsize=4)
def probe_ffmpeg(ffmpeg_path: str = "ffmpeg") -> ProbeResult:
    """探测 ffmpeg 可用编码器/解码器（同一路径只探测一次）。"""
    ffmpeg_path = resolve_ffmpeg_path(ffmpeg_path)
    try:
        version = _read_version(ffmpeg_path)
        encoders, _ = _list_codecs(ffmpeg_path, "-encoders")
        decoders, _ = _list_codecs(ffmpeg_path, "-decoders")
        accelerators = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-hwaccels"], capture_output=True, text=True,
            timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.split()
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        logger.error("ffmpeg 探测失败: %s", exc)
        return ProbeResult(ffmpeg_path=ffmpeg_path, error=str(exc))

    result = ProbeResult(
        ffmpeg_path=ffmpeg_path,
        version=version,
        encoders=encoders,
        decoders=decoders,
        hwaccels=set(accelerators),
    )
    logger.info("%s", result.describe())
    return result


#: 全部视频编码后端（含硬件），用于能力上报与前端下拉框
VIDEO_ENCODERS: dict[str, str] = {
    "libx264": "h264",
    "h264_nvenc": "h264",
    "h264_amf": "h264",
    "h264_qsv": "h264",
    "h264_vaapi": "h264",
    "h264_mediacodec": "h264",
    "libx265": "hevc",
    "hevc_nvenc": "hevc",
    "hevc_amf": "hevc",
    "hevc_qsv": "hevc",
    "hevc_vaapi": "hevc",
    "hevc_mediacodec": "hevc",
    "libsvtav1": "av1",
    "av1_nvenc": "av1",
    "av1_amf": "av1",
    "av1_qsv": "av1",
    "av1_vaapi": "av1",
    "av1_mediacodec": "av1",
}

VIDEO_DECODERS: dict[str, str] = {
    "h264": "h264",
    "h264_cuvid": "h264",
    "h264_amf": "h264",
    "h264_qsv": "h264",
    "h264_vaapi": "h264",
    "h264_mediacodec": "h264",
    "hevc": "hevc",
    "hevc_cuvid": "hevc",
    "hevc_amf": "hevc",
    "hevc_qsv": "hevc",
    "hevc_vaapi": "hevc",
    "hevc_mediacodec": "hevc",
    "av1": "av1",
    "av1_cuvid": "av1",
    "av1_amf": "av1",
    "av1_qsv": "av1",
    "av1_vaapi": "av1",
    "av1_mediacodec": "av1",
}


def available_encoders(
    codec: str | None = None, ffmpeg_path: str = "ffmpeg"
) -> list[str]:
    """本机可用的视频编码后端；``codec`` 为空则返回全部。"""
    snapshot = probe_video_codecs(ffmpeg_path)
    return [
        name for name, item in snapshot["encoders"].items()
        if (codec is None or item["codec"] == codec) and item["state"] == "available"
    ]


def available_decoders(
    codec: str | None = None, ffmpeg_path: str = "ffmpeg"
) -> list[str]:
    snapshot = probe_video_codecs(ffmpeg_path)
    return [
        name for name, item in snapshot["decoders"].items()
        if (codec is None or item["codec"] == codec) and item["state"] == "available"
    ]


def detect_video_dimensions(annexb: bytes, codec) -> tuple[int, int] | None:
    """按编码格式分派到对应的参数集解析，返回画面宽高。

    TS 的 PMT 只给 stream_type，分辨率必须从码流的参数集里解出来；
    AV1 的 sequence header 解析成本高，当前返回 None（由能力协商提供宽高）。
    """
    from backend.media.stream import CodecId

    if codec is CodecId.H264:
        from backend.media.video.codec.decoding.h264.h264 import parse_sps_dimensions

        return parse_sps_dimensions(annexb)
    if codec is CodecId.H265:
        from backend.media.video.codec.decoding.h265.hevc import parse_sps_dimensions

        return parse_sps_dimensions(annexb)
    return None


# Three red 320x240 frames, above hardware minimum dimensions.
PROBE_SAMPLES = {
    "h264": "AAAAAQkQAAAAAWdCwA3aBQfsBEAAAAMAQAAADyPFCqgAAAABaM4PyAAAAAFliIQ6EYoAAhjxwABA9jgACHlJycnJycnJycnJycnJycnJycnJ111111111111111111111111111111111111111111111111111111111111111111111111111111114AAAAAFlAyiIQ6EYoAAhjxwABA9jgACHlJycnJycnJycnJycnJycnJycnJ111111111111111111111111111111111111111111111111111111111111111111111111111111114AAAABZQGSIhDoRigACGPHAAED2OAAIeUnJycnJycnJycnJycnJycnJycnJycnXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXgAAAAAEJMAAAAAFBmiAmgMsAAAABQQMpogJoDLAAAAABQQGSaICaAywAAAABCTAAAAABQZpAKoDLAAAAAUEDKaQCqAywAAAAAUEBkmkAqgMs",
    "hevc": "AAAAAUYBEAAAAAFAAQwB//8BYAAAAwCQAAADAAADADy6AkAAAAABQgEBAWAAAAMAkAAAAwAAAwA8oAoIDxZbpKTC8BaAgAAAAwCAAAAPBAAAAAFEAcBxgxIAAAEoAa3EFic5ilLAx6f/hqv/uuKXdbI+8PI+uEjdEsRuvXCO3IYHJ1wjtqCigAlj9tQAN+ANTjAABqwO4AAASMAAAAMAA36HAAADAAADAAM+AAAAAUYBMAAAAQIB0Al4g+IbSSRgmACwgAATsAAhoABMQACLgADagAGTASkAAAABRgEwAAABAgHQEf4g8IbSSRiYALCAABOwACGgAExAAIuAANqAAZMBKQ==",
    "av1": "EgAKCwAAAAQ8/7za+QBAMh0QAIQAAACAAAAA68huiBEmxWU3r6NFI8iQcQ0laBIAMhUwA8CAAAAGkAAAAKAACACZI6obLxgSADITMAQBBAAABpAAAACgAAgAubMcwA==",
}
_video_cache: dict[tuple, dict] = {}
_probe_lock = threading.Lock()


def _device_signature():
    if sys.platform != "win32":
        return tuple((str(p), p.stat().st_mtime_ns) for p in sorted(Path("/sys/class/drm").glob("card*")))
    import ctypes
    from ctypes import wintypes
    import winreg

    class DisplayDevice(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("name", wintypes.WCHAR * 32),
                    ("description", wintypes.WCHAR * 128), ("flags", wintypes.DWORD),
                    ("identity", wintypes.WCHAR * 128), ("key", wintypes.WCHAR * 128)]
    result = []
    for index in range(32):
        device = DisplayDevice()
        device.cb = ctypes.sizeof(device)
        if not ctypes.windll.user32.EnumDisplayDevicesW(None, index, ctypes.byref(device), 0):
            break
        version = ""
        try:
            path = device.key.removeprefix("\\Registry\\Machine\\")
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
                version = str(winreg.QueryValueEx(key, "DriverVersion")[0])
        except OSError:
            pass
        result.append((device.identity, device.flags, version))
    return tuple(result)


def codec_label(name: str) -> str:
    vendor = next((label for suffix, label in (
        ("nvenc", "NVIDIA"), ("cuvid", "NVIDIA"), ("amf", "AMD"),
        ("qsv", "Intel"), ("vaapi", "VAAPI"), ("mediacodec", "MediaCodec")
    ) if name.endswith("_" + suffix)), "软件")
    return f"{vendor} · {name}"


def codec_options(values: dict) -> dict:
    """Validate the public identifiers without accepting command-line fragments."""
    if not isinstance(values, dict):
        raise ValueError("编解码设置格式无效")
    result = {key: values.get(key, "auto") for key in ("codec", "encoder", "decoder")}
    for key, allowed in (
        ("codec", {"auto", "h264", "hevc", "av1"}),
        ("encoder", {"auto", *VIDEO_ENCODERS}),
        ("decoder", {"auto", *VIDEO_DECODERS}),
    ):
        if not isinstance(result[key], str) or result[key] not in allowed:
            raise ValueError(f"无效编解码选项：{key}")
    for key, mapping in (("encoder", VIDEO_ENCODERS), ("decoder", VIDEO_DECODERS)):
        if result["codec"] != "auto" and result[key] != "auto" and mapping[result[key]] != result["codec"]:
            raise ValueError("编解码器与视频格式不匹配")
    return result


def checked_capabilities(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) - {"encoders", "decoders"}:
        raise ValueError("能力信息无效")
    result = {}
    for kind, mapping in (("encoders", VIDEO_ENCODERS), ("decoders", VIDEO_DECODERS)):
        items = value.get(kind, {})
        if not isinstance(items, dict) or len(items) > len(mapping):
            raise ValueError("能力信息过长")
        result[kind] = {}
        for name, item in items.items():
            if name not in mapping or not isinstance(item, dict) or item.get("codec") != mapping[name]:
                raise ValueError("能力后端无效")
            if item.get("state") == "available":
                result[kind][name] = {"codec": mapping[name], "state": "available"}
    return result


def public_capabilities(snapshot: dict) -> dict:
    return checked_capabilities({
        kind: {name: {"codec": item["codec"], "state": "available"}
               for name, item in snapshot.get(kind, {}).items() if item["state"] == "available"}
        for kind in ("encoders", "decoders")
    })


def compatible_choices(remote: dict, local: dict, preference: dict) -> list[dict]:
    options = codec_options(preference)
    encoders, decoders = remote.get("encoders", {}), local.get("decoders", {})
    result = []
    for codec in ("h264", "hevc", "av1"):
        if options["codec"] not in ("auto", codec):
            continue
        for encoder, enc in encoders.items():
            if enc["codec"] != codec or options["encoder"] not in ("auto", encoder):
                continue
            for decoder, dec in decoders.items():
                if dec["codec"] == codec and options["decoder"] in ("auto", decoder):
                    result.append({"codec": codec, "encoder": encoder, "decoder": decoder})
    if not result:
        raise ValueError("所选编解码器不可用或双方没有共同视频格式，请重新选择")
    return result


def _sample_packets(codec):
    from backend.media.packet import Packet, split_annexb
    from backend.media.stream import CodecId
    from .framing import HevcSplitter
    from .base import AccessUnitSplitter
    data = base64.b64decode(PROBE_SAMPLES[codec])
    if codec == "av1":
        units, current = [], bytearray()
        from .framing import sized_av1_obus
        for kind, _, raw in sized_av1_obus(data):
            if kind == 2 and current:
                units.append(bytes(current))
                current.clear()
            current += raw
        if current:
            units.append(bytes(current))
    else:
        splitter = HevcSplitter() if codec == "hevc" else AccessUnitSplitter()
        units = []
        for nal in split_annexb(data):
            units.extend(splitter.push(nal))
        tail = splitter.take()
        if tail:
            units.append(tail)
    return [Packet(data=data, codec=CodecId(codec), pts=index * 3000)
            for index, data in enumerate(units)]


def verify_backend(adapter, *, width=320, height=240, cancel=None, timeout=8.0) -> str | None:
    """Exercise the actual adapter with a watchdog, including output validation."""
    from backend.media.frame import Frame
    errors, done, expired = [], threading.Event(), threading.Event()

    def check_cancel():
        if expired.is_set() or (cancel is not None and cancel.is_set()):
            raise RuntimeError("探测已取消")

    def run():
        try:
            adapter.open()
            check_cancel()
            if hasattr(adapter, "encode"):
                packets = []
                for index in range(12):
                    check_cancel()
                    pixels = bytes([index * 8, 32, 200, 255]) * (width * height)
                    packets.extend(adapter.encode(Frame(data=pixels, width=width, height=height, pts=index * 3000)))
                    if sum(p.size for p in packets) > 32 * 1024 * 1024:
                        raise RuntimeError("编码输出过大")
                packets.extend(adapter.flush())
                if sum(p.size for p in packets) > 32 * 1024 * 1024:
                    raise RuntimeError("编码输出过大")
                if len(packets) != 12 or not packets[0].keyframe:
                    raise RuntimeError("编码未输出完整有效帧")
            else:
                frames = []
                adapter.output_callback = frames.append
                for packet in _sample_packets(adapter.codec):
                    check_cancel()
                    adapter.decode(packet)
                adapter.flush()
                if len(frames) != 3 or any(
                    f.width != 320 or f.height != 240 or len(f.data) != 320 * 240 * 4
                    or f.data[2] < 200 or f.data[0] > 40 or f.data[1] > 40 for f in frames
                ):
                    raise RuntimeError("解码未输出正确画面")
            proc = adapter._proc
            if proc is not None and proc.wait(timeout=1) != 0:
                raise RuntimeError(adapter._stderr_text())
        except Exception as exc:
            errors.append(f"{exc}: {adapter._stderr_text()}")
        finally:
            try:
                if expired.is_set() and adapter._proc is not None:
                    try:
                        adapter._proc.kill()
                    except OSError:
                        pass
                adapter.close()
            finally:
                done.set()

    worker = threading.Thread(target=run, name="codec-validation", daemon=True)
    worker.start()
    deadline = time.monotonic() + timeout
    while not done.wait(0.05):
        if time.monotonic() >= deadline or (cancel is not None and cancel.is_set()):
            expired.set()
            proc = adapter._proc
            if proc is not None:
                try:
                    proc.kill()
                except OSError:
                    pass
            worker.join(timeout=3)
            return "探测已取消" if cancel is not None and cancel.is_set() else "编解码探测超时"
    return errors[0][:1500] if errors else None


def probe_video_codecs(ffmpeg_path="ffmpeg", *, refresh=False, cancel=None, progress=None):
    from backend.config.schema import VideoEncodeConfig
    from backend.media.stream import CodecId, StreamInfo
    from backend.registry.codec_registry import build_default_registry
    path = resolve_ffmpeg_path(ffmpeg_path)
    resolved = Path(shutil.which(path) or path)
    stat = resolved.stat() if resolved.is_file() else None
    key = (str(resolved.resolve()), stat.st_size if stat else 0,
           stat.st_mtime_ns if stat else 0, _device_signature())
    with _probe_lock:
        if not refresh and key in _video_cache:
            return _video_cache[key]
        probe_ffmpeg.cache_clear()
        registry = build_default_registry(path)
        snapshot = {"encoders": {}, "decoders": {}}
        for kind in snapshot:
            for codec, entry in registry.candidates(kind):
                item = {"codec": codec, "state": "pending", "reason": ""}
                snapshot[kind][entry.name] = item
                if cancel is not None and cancel.is_set():
                    return snapshot
                platform_ok = not (
                    (entry.name.endswith("_vaapi") and not sys.platform.startswith("linux"))
                    or (entry.name.endswith("_mediacodec") and sys.platform != "android")
                    or (entry.name.endswith("_amf") and sys.platform != "win32")
                )
                reason = "当前平台或 FFmpeg 不提供此后端"
                if platform_ok and entry.is_available():
                    cfg = VideoEncodeConfig(codec=codec, encoder=entry.name, decoder=entry.name,
                                            ffmpeg_path=path, preset="ultrafast", realtime=True)
                    adapter = entry.factory(StreamInfo(codec=CodecId(codec), width=320, height=240,
                                                       framerate=30), cfg)
                    reason = verify_backend(adapter, cancel=cancel)
                item.update(state="unavailable" if reason else "available", reason=reason or "")
                if progress:
                    progress(snapshot)
        _video_cache.clear()
        _video_cache[key] = snapshot
        return snapshot
