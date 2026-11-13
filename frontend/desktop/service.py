"""界面与后端之间的桥。

界面（Qt）不直接碰 ffmpeg、socket、Win32：它只调用这里的两个方法
（:meth:`SessionService.start_host` / :meth:`start_viewer`）并监听信号。
所有阻塞操作用 ``threading.Thread`` 跑，信号跨线程发送由 Qt 自动排队，
因此不会卡住事件循环。

数据流：

    主机模式  采集 → [缩放] → 预览tap → 编码 → MPEG-TS → SRT/文件
              控制通道(TCP 服务端) → 输入注入

    观看模式  SRT/文件 → 解封装 → 解码 → 预览
              本机输入捕获 → 控制通道(TCP 客户端) → 对方
"""

from __future__ import annotations

import json
import platform
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from . import __version__

from PySide6.QtCore import QObject, Signal, Qt

from backend import clipboard as clipboard_backend
from backend.bootstrap import build_receive_resources, build_send_resources, prepare_environment
from backend.config.schema import AppConfig
from backend.control_channel.protocol import ControlCommand, EventType, InputEvent
from backend.control_channel.base import ChannelState
from backend.control_channel.tcp import TcpControlChannel
from backend.input_injection.session import InputSession
from backend.input_capture.mouse import mouse_mode
from backend.errors.network import TransportClosedError
from backend.media.device.display import DisplayInfo, list_displays
from backend.media.device.enumerate import DeviceKind, enumerate_devices
from backend.media.frame import Frame
from backend.media.video.codec.probe import probe_video_codecs, public_capabilities, verify_backend
from backend.media.video.filter.pixel_format import BaseVideoFilter
from backend.media.video.filter.scaler import Scaler
from backend.logging.logger import get_logger

from .paths import resource_dir
from .error_report import remember, save_error

logger = get_logger(__name__)


@dataclass
class HostSettings:
    """主机（被控）模式的界面参数。"""

    display_index: int = 0
    width: int = 1920
    height: int = 1080
    fps: float = 30.0
    bitrate_kbps: int = 4000
    encoder: str = "libx264"
    codec: str = "h264"
    preset: str = "veryfast"
    transport: str = "srt"  # srt / file
    srt_url: str = "srt://0.0.0.0:9000"
    srt_mode: str = "listener"
    file_path: str = "out/host.ts"
    control_port: int = 7000
    allow_inject: bool = True
    clipboard_enabled: bool = True
    gamepad_enabled: bool = True
    ffmpeg_path: str = "ffmpeg"


@dataclass
class ViewerSettings:
    """观看（控制）模式的界面参数。"""

    srt_url: str = "srt://127.0.0.1:9000"
    transport: str = "srt"  # srt / file
    srt_mode: str = "caller"
    file_path: str = "out/host.ts"
    control_host: str = "127.0.0.1"
    control_port: int = 7000
    control_enabled: bool = False
    mouse_mode: str = "smart"
    mouse_sensitivity: float = 0.0
    stats_visible: bool = False
    stream_width: int = 1920
    stream_height: int = 1080
    stream_fps: float = 30.0
    stream_bitrate_kbps: int = 4000
    stream_codec: str = "h264"
    decoder: str = "auto"
    codec_preferences: dict = field(default_factory=dict)
    clipboard_enabled: bool = True
    gamepad_enabled: bool = True
    scale_percent: int = 100
    sr_enabled: bool = False
    sr_mode: str = "fsr"  # fsr / rtx
    sr_percent: int = 100
    sr_sharpen: bool = False
    sr_sharpness: int = 50
    output_width: int = 1920
    output_height: int = 1080
    ffmpeg_path: str = "ffmpeg"


@dataclass
class SessionStats:
    """一次会话的累计统计（界面直接显示）。"""

    frames_in: int = 0
    frames_out: int = 0
    bytes_out: int = 0
    key_frames: int = 0
    media_seconds: float = 0.0
    bitrate_kbps: float = 0.0
    fps: float = 0.0
    injected_events: int = 0
    elapsed_s: float = 0.0
    extra: dict = field(default_factory=dict)


class _PreviewTap(BaseVideoFilter):
    """Forward to the latest-frame mailbox without a separate frame-rate cap."""

    name = "preview_tap"

    def __init__(self, emit: Callable[[Frame], None]) -> None:
        self._emit = emit
        self.frames = 0

    def process(self, frame: Frame) -> Frame:
        self.frames += 1
        self._emit(frame)
        return frame


class _QtRenderer:
    """接收侧的渲染器：把解码帧交给界面（替代写 PNG 的 ImageSink）。"""

    name = "qt_preview"

    def __init__(self, emit: Callable[[Frame], None], upscaler=None, *, upscaler_factory=None,
                 failure=None) -> None:
        self._emit = emit
        self._upscaler = upscaler
        self._upscaler_factory = upscaler_factory
        self._failure = failure
        self.frames = 0
        self._condition = threading.Condition()
        self._pending = None
        self._closed = False
        self._worker = None

    def open(self) -> None:
        self._worker = threading.Thread(target=self._loop, name="preview-prepare", daemon=True)
        self._worker.start()

    def render(self, frame: Frame) -> bool:
        self.frames += 1
        with self._condition:
            if not self._closed:
                self._pending = frame
                self._condition.notify()
        return True

    def _loop(self) -> None:
        try:
            if self._upscaler_factory is not None:
                self._upscaler = self._upscaler_factory()
            self._prepare_frames()
        except Exception as exc:
            if self._failure is not None:
                self._failure(exc)
            else:
                logger.exception("画面处理失败")
        finally:
            self._close_upscaler()

    def _close_upscaler(self) -> None:
        upscaler, self._upscaler = self._upscaler, None
        if upscaler is not None:
            try:
                upscaler.close()
            except Exception as exc:
                logger.warning("超分辨率资源释放失败: %s", exc)

    def _prepare_frames(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._pending is not None)
                if self._closed:
                    return
                frame, self._pending = self._pending, None
            if self._upscaler is not None:
                try:
                    frame = self._upscaler.upscale(frame)
                except Exception as exc:
                    if self._failure is not None:
                        raise
                    logger.warning("FSR 超分辨率失败，回退原帧: %s", exc)
                    self._close_upscaler()
            self._emit(frame)

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._pending = None
            self._condition.notify()
        if self._worker is not None:
            self._worker.join(timeout=5)


class ClipboardBridge:
    """轮询本机文本剪贴板并通过控制通道同步；防止自己写回时产生回声。"""

    def __init__(self, send, interval_s: float = 0.5) -> None:
        self._send = send
        self._interval = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_seq = clipboard_backend.sequence()
        self._last_text = clipboard_backend.read_text()
        self._applied_text = ""

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running or not clipboard_backend.available():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="clipboard-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread = None

    def apply_remote(self, text: str) -> None:
        if not text or len(text) > clipboard_backend.MAX_CLIPBOARD_CHARS:
            return
        if text == self._last_text and text == self._applied_text:
            return
        if clipboard_backend.write_text(text):
            self._applied_text = text
            self._last_text = text
            self._last_seq = clipboard_backend.sequence()

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                seq = clipboard_backend.sequence()
                if seq == self._last_seq:
                    continue
                self._last_seq = seq
                text = clipboard_backend.read_text()
                if not text or text == self._last_text or text == self._applied_text:
                    continue
                self._last_text = text
                self._send(text)
            except Exception:
                continue


class SessionService(QObject):
    """一次会话的生命周期 + 信号。"""

    frame_ready = Signal(object)          # Frame
    state_changed = Signal(str, str)      # (idle/running/stopping/error, 说明)
    log = Signal(str, str)                # (消息, level)
    stats_changed = Signal(object)        # SessionStats
    control_changed = Signal(str)         # 控制通道状态描述
    devices_changed = Signal(list)        # list[dict]
    host_prepared = Signal()
    input_available = Signal(bool)
    cursor_changed = Signal(object)
    _frame_pending = Signal(int)
    capabilities_changed = Signal()
    codec_validated = Signal(int, str)
    upscale_changed = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self._pipeline = None
        self._control: TcpControlChannel | None = None
        self._injector = None
        self._input_session: InputSession | None = None
        self._input_lock = threading.RLock()
        self._input_epoch = 0
        self._input_captured = False
        self._input_error_id = ""
        self._capture_region = None
        self._viewer_live: ViewerSettings | None = None
        self._clipboard: ClipboardBridge | None = None
        self._pads = None
        self._pad_poll = None
        self._settings_snapshot: dict | None = None
        self._stats = SessionStats()
        self._mode = ""
        self._started_at = 0.0
        self._devices_loading = False
        self._transport = None
        self._host_live: HostSettings | None = None
        self._stats_worker: threading.Thread | None = None
        self._control_worker: threading.Thread | None = None
        self.failure_id = ""
        self.failure_message = ""
        self.error_context: dict = {}
        self._frame_lock = threading.Lock()
        self._frame_generation = 0
        self._latest_frame = None
        self._frame_notified = False
        self._presented = 0
        self._frame_pending.connect(self._deliver_frame, Qt.ConnectionType.QueuedConnection)
        self.log.connect(lambda message, level: remember(f"{level}: {message}"))
        self.codec_capabilities: dict | None = None
        self._capability_worker = None
        self._capability_cancel = threading.Event()
        self._validation_cancel = threading.Event()
        self.upscale_capabilities = None
        self.upscale_status = ""
        self._upscale_worker = None
        self._upscale_cancel = threading.Event()
        self._upscale_checked = 0.0

    def refresh_upscale(self, force=False):
        if self._upscale_worker is not None and self._upscale_worker.is_alive():
            return
        if self.running and self._viewer_live is not None and self._viewer_live.sr_enabled:
            return
        if not force and time.monotonic() - self._upscale_checked < 30:
            return
        from .upscale import probe_native
        cancel = self._upscale_cancel = threading.Event()
        self.upscale_status = "正在检测超分能力…"
        self.upscale_changed.emit()

        def run():
            try:
                snapshot = probe_native(cancel)
                if not cancel.is_set():
                    self.upscale_capabilities = snapshot
                    self._upscale_checked = time.monotonic()
                    self.upscale_status = ""
            except Exception as exc:
                if not cancel.is_set():
                    self.upscale_status = str(exc)
            finally:
                if not cancel.is_set():
                    self.upscale_changed.emit()
        self._upscale_worker = threading.Thread(target=run, name="upscale-probe", daemon=True)
        self._upscale_worker.start()

    def cancel_upscale_probe(self):
        self._upscale_cancel.set()

    def _upscale_actual(self, mode, state):
        detail = {"unobserved": "未观察到增强", "bypass": "原尺寸，未放大"}.get(state, "处理中")
        text = mode.upper() + "：" + detail
        self._stats.extra["upscale"] = mode
        if self.upscale_status != text:
            self.upscale_status = text
            self.upscale_changed.emit()

    def refresh_codecs(self, ffmpeg_path="ffmpeg", *, refresh=False):
        if self._capability_worker is not None and self._capability_worker.is_alive():
            return
        if self.running and refresh:
            self.log.emit("正在传输画面，断开后可重新检测编解码能力", "info")
            return
        self.codec_capabilities = None
        self._capability_cancel = threading.Event()
        cancel = self._capability_cancel
        self.capabilities_changed.emit()

        def run():
            try:
                snapshot = probe_video_codecs(ffmpeg_path, refresh=refresh, cancel=cancel)
                if not cancel.is_set():
                    self.codec_capabilities = public_capabilities(snapshot)
            except Exception as exc:
                self.codec_capabilities = {"encoders": {}, "decoders": {}}
                self.log.emit(f"编解码检测失败：{exc}", "warn")
            finally:
                if not cancel.is_set():
                    self.capabilities_changed.emit()
        self._capability_worker = threading.Thread(target=run, name="codec-capabilities", daemon=True)
        self._capability_worker.start()

    def validate_codec(self, revision, kind, selection, picture, ffmpeg_path="ffmpeg"):
        self._validation_cancel.set()
        cancel = self._validation_cancel = threading.Event()

        def run():
            from backend.registry.codec_registry import build_default_registry
            from backend.media.stream import CodecId, StreamInfo
            from backend.config.schema import VideoEncodeConfig
            try:
                registry = build_default_registry(ffmpeg_path)
                width, height = ((picture["width"], picture["height"]) if kind == "encoder" else (320, 240))
                config = VideoEncodeConfig(
                    codec=selection["codec"], encoder=selection["encoder"], decoder=selection["decoder"],
                    ffmpeg_path=ffmpeg_path, bitrate_kbps=picture["bitrate_kbps"],
                    preset="ultrafast", realtime=True, gop=max(1, round(picture["fps"] * 2)))
                stream = StreamInfo(codec=CodecId(selection["codec"]), width=width, height=height,
                                    framerate=picture["fps"])
                create = registry.create_encoder if kind == "encoder" else registry.create_decoder
                adapter = create(selection["codec"], selection[kind], stream, config)
                reason = verify_backend(adapter, width=width, height=height, cancel=cancel)
            except Exception as exc:
                reason = str(exc)
            if not cancel.is_set():
                self.codec_validated.emit(revision, reason or "")
        threading.Thread(target=run, name="codec-preflight", daemon=True).start()

    def cancel_codec_validation(self, *, include_probe=False):
        self._validation_cancel.set()
        if include_probe:
            self._capability_cancel.set()

    def _clear_frames(self) -> None:
        with self._frame_lock:
            self._frame_generation += 1
            self._latest_frame = None
            self._frame_notified = False

    def _frame_sink(self):
        generation = self._frame_generation

        def submit(frame):
            with self._frame_lock:
                if generation != self._frame_generation or self._stop.is_set():
                    return
                self._latest_frame = (frame, time.perf_counter())
                if self._frame_notified:
                    return
                self._frame_notified = True
            self._frame_pending.emit(generation)
        return submit

    def _deliver_frame(self, generation: int) -> None:
        with self._frame_lock:
            if generation != self._frame_generation:
                return
            latest, self._latest_frame = self._latest_frame, None
            self._frame_notified = False
        if latest is not None:
            frame, entered = latest
            self._stats.extra["delivery_ms"] = (time.perf_counter() - entered) * 1000
            self.frame_ready.emit(frame)

    def frame_presented(self) -> None:
        self._presented += 1

    # ---- 查询 ----
    @property
    def running(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def stats(self) -> SessionStats:
        return self._stats

    @staticmethod
    def displays() -> list[DisplayInfo]:
        return list_displays()

    @staticmethod
    def devices() -> list[dict]:
        items: list[dict] = []
        for kind in (DeviceKind.DISPLAY, DeviceKind.CAMERA, DeviceKind.MICROPHONE):
            try:
                for device in enumerate_devices(kind):
                    items.append(
                        {
                            "kind": device.kind.value,
                            "name": device.name,
                            "id": device.identifier,
                            "default": device.is_default,
                        }
                    )
            except Exception:  # 单类枚举失败不影响其它类别
                continue
        return items

    def refresh_devices(self) -> None:
        self.devices_changed.emit(self.devices())

    def refresh_devices_async(self) -> None:
        """设备枚举可能卡在 ffmpeg dshow（最长 15s），放到后台线程避免窗口启动被拖住。"""
        if self._devices_loading:
            return
        self._devices_loading = True

        def run() -> None:
            try:
                items = self.devices()
            except Exception:
                items = []
            finally:
                self._devices_loading = False
            self.devices_changed.emit(items)

        threading.Thread(target=run, name="device-enum", daemon=True).start()

    # ---- 启动 ----
    def start_host(self, settings: HostSettings) -> None:
        if self.running:
            self.log.emit("已有会话在运行，请先停止", "warn")
            return
        self._settings_snapshot = {"mode": "host", **asdict(settings)}
        self._host_live = settings
        self._begin("host")
        config, filters = _host_config(settings)
        settings = replace(settings, width=config.encode.width or settings.width,
                           height=config.encode.height or settings.height)
        self._host_live = settings
        self._worker = threading.Thread(
            target=self._run_host,
            args=(config, filters, settings),
            name="host-session",
            daemon=True,
        )
        self._worker.start()

    def start_viewer(self, settings: ViewerSettings) -> None:
        if self.running:
            self.log.emit("已有会话在运行，请先停止", "warn")
            return
        self._settings_snapshot = {"mode": "viewer", **asdict(settings)}
        self._viewer_live = settings
        self._begin("viewer")
        config = _viewer_config(settings)
        self._worker = threading.Thread(
            target=self._run_viewer,
            args=(config, settings),
            name="viewer-session",
            daemon=True,
        )
        self._worker.start()

    def expect_stop(self) -> None:
        self.set_input_focus(False)
        self._stop.set()
        self._clear_frames()

    def stop(self) -> None:
        self.set_input_focus(False)
        self._clear_frames()
        if not self.running:
            self._stop_control()
            return
        self._stop.set()
        transport = self._transport
        if transport is not None:
            threading.Thread(target=transport.abort, name="media-stop", daemon=True).start()
        self.state_changed.emit("stopping", "正在停止…")
        pipeline = self._pipeline
        if pipeline is not None and hasattr(pipeline, "stop"):
            try:
                pipeline.stop()
            except Exception:
                pass
        self._stop_control()

    # ---- 内部：公共 ----
    def _begin(self, mode: str) -> None:
        self.failure_id = ""
        self.failure_message = ""
        self._input_error_id = ""
        self._mode = mode
        self._stop.clear()
        self._clear_frames()
        self._presented = 0
        self._stats = SessionStats()
        self._started_at = time.monotonic()
        self.state_changed.emit("running", "正在启动…")
        self.stats_changed.emit(self._stats)

    def _finish(self, reason: str = "") -> None:
        self._stop.set()
        if self._stats_worker is not None:
            self._stats_worker.join(timeout=2)
            self._stats_worker = None
        self._stop_clipboard()
        self._stop_gamepad()
        self._stop_control()
        if self._control_worker is not None:
            self._control_worker.join(timeout=2)
            self._control_worker = None
        self._input_session = None
        if self._injector is not None:
            self._injector.close()
            self._injector = None
        self._pipeline = None
        self._transport = None
        self._host_live = None
        self._viewer_live = None
        self._mode = ""
        self.state_changed.emit("idle", reason)
        if self._started_at:
            self._stats.elapsed_s = round(time.monotonic() - self._started_at, 1)
        self.stats_changed.emit(self._stats)

    def record_failure(self, error: BaseException | str) -> None:
        context = {
            **self.error_context, "role": self._mode,
            "settings": self._settings_snapshot,
            "srt": self._transport.diagnostics() if self._transport else None,
            "codec": getattr(self._pipeline, "process_diagnostics", None),
        }
        self.failure_id = save_error(error, fault_id=self.failure_id, context=context)
        if not self.failure_message:
            self.failure_message = str(error)

    def _check_media_end(self) -> None:
        if not self._stop.is_set() and self._transport is not None:
            raise TransportClosedError("画面传输意外结束", detail=self._transport.diagnostics())

    def _stats_loop(self, source, *, is_host: bool) -> None:
        """每秒把管线统计搬到界面（读取失败不影响主流程）。"""
        previous_time = time.monotonic()
        previous_frames = previous_bytes = 0
        while not self._stop.wait(1.0):
            try:
                stats = getattr(source, "stats", None)
                if stats is None:
                    continue
                if is_host:
                    self._stats.frames_in = getattr(stats, "frames_captured", self._stats.frames_in)
                    self._stats.frames_out = getattr(stats, "frames_encoded", self._stats.frames_out)
                    self._stats.bytes_out = getattr(stats, "bytes_written", self._stats.bytes_out)
                    self._stats.key_frames = stats.keyframes
                else:
                    self._stats.frames_in = getattr(stats, "packets_read", self._stats.frames_in)
                    self._stats.frames_out = getattr(stats, "frames_decoded", self._stats.frames_out)
                    self._stats.key_frames = stats.keyframes
                    self._stats.bytes_out = stats.bytes_read
                now = time.monotonic()
                interval = max(0.001, now - previous_time)
                frames = self._stats.frames_out if is_host else self._presented
                self._stats.fps = (frames - previous_frames) / interval
                self._stats.bitrate_kbps = (self._stats.bytes_out - previous_bytes) * 8 / interval / 1000
                previous_time, previous_frames, previous_bytes = now, frames, self._stats.bytes_out
                self._stats.extra["sampled_at"] = now
                self._stats.extra["encoder" if is_host else "decoder"] = stats.extra.get("encoder" if is_host else "decoder", "--")
                self._stats.extra["codec"] = source.stream.codec.value if source.stream else "--"
                values = list(stats.extra.get("decode_output_ms", []))
                if values:
                    self._stats.extra["decode_ms"] = sum(values[-120:]) / len(values[-120:])
                if is_host and self._control is not None and self._control.state == ChannelState.OPEN:
                    self._control.send_command(ControlCommand("host_stats", {
                        "encoder": stats.extra.get("encoder", "--"),
                    }))
                self._stats.media_seconds = round(getattr(stats, "media_seconds", 0.0), 2)
                self._stats.elapsed_s = round(time.monotonic() - self._started_at, 1)
                self.stats_changed.emit(replace(self._stats, extra=dict(self._stats.extra)))
            except Exception:
                continue

    # ---- 内部：主机 ----
    def _run_host(self, config: AppConfig, filters: list, settings: HostSettings) -> None:
        runtime = None
        self._capture_region = config.capture.clamped_region()
        try:
            _probe, registry = prepare_environment(config)
            runtime = build_send_resources(config, registry)
            pipeline = runtime.get("pipeline")
            self._pipeline = pipeline
            self._transport = runtime.get("srt")
            if self._stop.is_set():
                return

            tap = _PreviewTap(self._frame_sink())
            pipeline.filters.extend([*filters, tap])

            self._start_control_server(settings)
            if settings.clipboard_enabled:
                self._start_clipboard()
            if settings.gamepad_enabled:
                self._start_gamepad_host()
            self.log.emit(
                f"主机模式已启动：{config.transport.kind} · {settings.width}x{settings.height}"
                f"@{settings.fps:g} · {settings.bitrate_kbps}kbps · 编码器 {config.encode.encoder}",
                "info",
            )
            self._stats_worker = threading.Thread(
                target=self._stats_loop, args=(pipeline,), kwargs={"is_host": True}, daemon=True
            )
            self._stats_worker.start()
            self.host_prepared.emit()

            stats = pipeline.run()
            self._check_media_end()
            self.log.emit(f"发送结束：{stats.summary()}", "info")
            self._stats.bytes_out = getattr(stats, "bytes_written", self._stats.bytes_out)
        except Exception as exc:
            if not self._stop.is_set():
                self.record_failure(exc)
                self.log.emit(f"主机模式失败：{exc}", "error")
                self.state_changed.emit("error", str(exc))
        finally:
            if runtime is not None:
                transport = runtime.get("srt")
                if transport is not None:
                    transport.abort()
                runtime.close()
            self._finish("发送已停止")

    def _run_viewer(self, config: AppConfig, settings: ViewerSettings) -> None:
        runtime = None
        try:
            _probe, registry = prepare_environment(config)
            runtime = build_receive_resources(config, registry)
            pipeline = runtime.get("pipeline")
            self._pipeline = pipeline
            self._transport = runtime.get("srt")
            if self._stop.is_set():
                return

            pipeline.renderer = _QtRenderer(
                self._frame_sink(), upscaler_factory=lambda: self._create_upscaler(settings),
                failure=self._upscale_failure)

            self._start_control_client(settings)
            if settings.control_enabled and settings.clipboard_enabled:
                self._start_clipboard()
            if settings.control_enabled and settings.gamepad_enabled:
                self._start_gamepad_viewer()
            self.log.emit(f"观看模式已启动：{settings.srt_url if settings.transport == 'srt' else settings.file_path}", "info")
            self._stats_worker = threading.Thread(
                target=self._stats_loop, args=(pipeline,), kwargs={"is_host": False}, daemon=True
            )
            self._stats_worker.start()

            stats = pipeline.run()
            self._check_media_end()
            self.log.emit(f"接收结束：{stats.summary()}", "info")
        except Exception as exc:
            if not self._stop.is_set():
                self.record_failure(exc)
                self.log.emit(f"观看模式失败：{exc}", "error")
                self.state_changed.emit("error", str(exc))
        finally:
            if runtime is not None:
                transport = runtime.get("srt")
                if transport is not None:
                    transport.abort()
                runtime.close()
            self._finish("接收已停止")

    def _create_upscaler(self, settings: ViewerSettings):
        if not settings.sr_enabled:
            return None
        self.cancel_upscale_probe()
        if self._upscale_worker is not None:
            self._upscale_worker.join(timeout=3)
            if self._upscale_worker.is_alive():
                raise RuntimeError("超分检测尚未结束")
        from .upscale import UpscaleProcessor
        return UpscaleProcessor(
            settings.sr_mode, (settings.output_width, settings.output_height),
            settings.sr_sharpness if settings.sr_sharpen else 0, self._upscale_actual)

    def _upscale_failure(self, exc):
        self.record_failure(exc)
        self._upscale_checked = 0.0
        self.upscale_capabilities = None
        self.upscale_status = f"超分处理失败：{exc}"
        self.upscale_changed.emit()
        self.state_changed.emit("error", self.upscale_status)

    # ---- 内部：控制通道 ----
    def _start_control_server(self, settings: HostSettings) -> None:
        def run() -> None:
            try:
                channel = TcpControlChannel(
                    "0.0.0.0", settings.control_port, server_side=True, timeout_s=30.0
                )
                self._control = channel
                self._input_session = InputSession(self._inject_remote_input)
                channel.on_message(lambda message: self._on_remote_input(message, settings)
                                   if self._control is channel and not self._stop.is_set() else None)
                self.control_changed.emit(f"等待控制端连接 :{settings.control_port}")
                channel.open()
                self.control_changed.emit(f"控制端已接入 :{settings.control_port}")
                while not self._stop.wait(0.05) and channel.state == ChannelState.OPEN:
                    try:
                        capture = getattr(self._pipeline, "capture", None)
                        channel.send_command(ControlCommand("cursor", getattr(capture, "cursor_state", {
                            "mode": getattr(capture, "cursor_mode", "smart"),
                            "visible": getattr(capture, "cursor_visible", None),
                        })))
                        if self._input_session is not None:
                            self._input_session.watchdog()
                    except Exception as exc:
                        self._input_failure(exc)
            except Exception as exc:
                if not self._stop.is_set():
                    save_error(exc, context={**self.error_context, "stage": "control",
                                             "settings": self._settings_snapshot})
                    self.log.emit(f"控制通道异常：{exc}", "warn")
                    self.control_changed.emit("控制通道未连接")
            finally:
                self._release_remote_input()

        self._control_worker = threading.Thread(target=run, name="control-server", daemon=True)
        self._control_worker.start()

    def _start_control_client(self, settings: ViewerSettings) -> None:
        def run() -> None:
            try:
                deadline = time.monotonic() + 10.0
                while not self._stop.is_set():
                    channel = TcpControlChannel(
                        settings.control_host, settings.control_port, timeout_s=2.0
                    )
                    self._control = channel
                    channel.on_message(lambda message: self._on_remote_event(message, settings)
                                       if self._control is channel and not self._stop.is_set() else None)
                    try:
                        channel.open()
                        break
                    except Exception:
                        channel.close()
                        if time.monotonic() >= deadline:
                            raise
                        self._stop.wait(0.2)
                if self._stop.is_set():
                    return
                self.control_changed.emit(f"已连接控制通道 {settings.control_host}:{settings.control_port}")
                self.input_available.emit(True)
                channel.send_command(ControlCommand("mouse_mode", {"mode": mouse_mode(settings.mouse_mode)}))
                while not self._stop.wait(0.5) and channel.state == ChannelState.OPEN:
                    with self._input_lock:
                        if self._input_captured:
                            channel.send_command(ControlCommand("input_focus", {
                                "active": True, "epoch": self._input_epoch,
                            }))
                        else:
                            channel.send_ping()
            except Exception as exc:
                if not self._stop.is_set():
                    save_error(exc, context={**self.error_context, "stage": "control",
                                             "settings": self._settings_snapshot})
                    self.log.emit(f"控制通道连接失败：{exc}", "warn")
                    self.control_changed.emit("控制通道未连接")
            finally:
                self.set_input_focus(False)
                self.input_available.emit(False)

        self._control_worker = threading.Thread(target=run, name="control-client", daemon=True)
        self._control_worker.start()

    def set_control_enabled(self, enabled: bool, settings: ViewerSettings) -> None:
        settings.control_enabled = enabled
        self._viewer_live = settings
        if not enabled:
            self.set_input_focus(False)

    def set_host_control(self, enabled: bool) -> None:
        if self._host_live is not None:
            self._host_live.allow_inject = enabled
            self._host_live.gamepad_enabled = enabled
            self._host_live.clipboard_enabled = enabled
            if enabled:
                self._start_clipboard()
                self._start_gamepad_host()
            else:
                self._release_remote_input()
                self._stop_clipboard()
                self._stop_gamepad()

    def set_input_focus(self, active: bool) -> bool:
        with self._input_lock:
            channel = self._control
            if active and (self._mode != "viewer" or self._viewer_live is None
                           or not self._viewer_live.control_enabled or channel is None
                           or channel.state != ChannelState.OPEN):
                return False
            if active == self._input_captured:
                return True
            self._input_captured = active
            self._input_epoch += 1
            try:
                if channel is not None and channel.state == ChannelState.OPEN and self._mode == "viewer":
                    channel.send_command(ControlCommand("input_focus", {
                        "active": active, "epoch": self._input_epoch,
                    }))
            except Exception as exc:
                self._input_captured = False
                self._input_failure(exc)
                return False
            return True

    def send_input(self, event: InputEvent) -> bool:
        with self._input_lock:
            if not self._input_captured or self._control is None:
                return False
            try:
                event.extra = {**event.extra, "epoch": self._input_epoch}
                self._control.send_event(event)
                return True
            except Exception as exc:
                self._input_captured = False
                self._input_failure(exc)
                self.input_available.emit(False)
                return False

    def _input_failure(self, exc: Exception) -> None:
        self._input_error_id = save_error(exc, fault_id=self._input_error_id,
                                         context={**self.error_context, "stage": "input"})
        self.log.emit(f"远控输入失败：{exc}", "warn")

    def _inject_remote_input(self, event: InputEvent) -> bool:
        if self._injector is None:
            from backend.input_injection.windows.send_input import SendInputInjector
            self._injector = SendInputInjector(region=self._capture_region)
        success = self._injector.inject(event)
        if success:
            self._stats.injected_events += 1
        return success

    def _release_remote_input(self) -> None:
        with self._input_lock:
            if self._input_session is not None:
                try:
                    self._input_session.release()
                except Exception as exc:
                    self._input_failure(exc)

    def _on_remote_input(self, message, settings: HostSettings) -> None:
        if isinstance(message, ControlCommand):
            if message.name == "mouse_mode":
                capture = getattr(self._pipeline, "capture", None)
                if capture is not None:
                    capture.cursor_mode = mouse_mode(message.args.get("mode"))
                return
            if (self._input_session is not None and isinstance(message.args, dict)
                    and (settings.allow_inject or not message.args.get("active"))):
                try:
                    self._input_session.receive(message)
                except Exception as exc:
                    self._input_failure(exc)
            return
        if not isinstance(message, InputEvent) or not settings.allow_inject:
            return
        if message.event_type is EventType.CLIPBOARD:
            if settings.clipboard_enabled and self._clipboard is not None:
                self._clipboard.apply_remote(message.text)
            return
        if message.event_type is EventType.GAMEPAD:
            if not settings.gamepad_enabled:
                return
            extra = message.extra or {}
            if "values" in extra and self._pads is not None:
                try:
                    self._pads.update(int(extra.get("index", 0)), list(extra["values"]))
                except Exception as exc:
                    self.log.emit(f"手柄注入失败：{exc}", "warn")
            return
        try:
            if self._input_session is not None:
                self._input_session.receive(message)
        except Exception as exc:
            self._input_failure(exc)
            self._release_remote_input()

    # ---- 内部：剪贴板 ----
    def _start_clipboard(self) -> None:
        if self._clipboard is not None and self._clipboard.running:
            return
        bridge = ClipboardBridge(self._send_clipboard)
        bridge.start()
        self._clipboard = bridge
        if bridge.running:
            self.log.emit("剪贴板同步已开启（仅文本，最多 64000 字符）", "info")

    def _stop_clipboard(self) -> None:
        bridge = self._clipboard
        self._clipboard = None
        if bridge is not None:
            bridge.stop()

    def set_clipboard_enabled(self, enabled: bool) -> None:
        if enabled:
            self._start_clipboard()
        else:
            self._stop_clipboard()

    def _send_clipboard(self, text: str) -> None:
        channel = self._control
        if channel is None:
            return
        try:
            channel.send_event(InputEvent(event_type=EventType.CLIPBOARD, text=text))
        except Exception:
            return

    def _on_remote_event(self, message, settings: ViewerSettings) -> None:
        if isinstance(message, ControlCommand):
            if message.name == "cursor":
                self.cursor_changed.emit(message.args)
            elif message.name == "host_stats":
                self._stats.extra["encoder"] = str(message.args.get("encoder", "--"))
                self._stats.extra["host_sampled_at"] = time.monotonic()
            return
        if not isinstance(message, InputEvent):
            return
        if message.event_type is EventType.CLIPBOARD:
            if settings.clipboard_enabled and self._clipboard is not None:
                self._clipboard.apply_remote(message.text)
        elif message.event_type is EventType.GAMEPAD:
            extra = message.extra or {}
            poller = self._pad_poll
            if poller is not None and "left" in extra and "right" in extra:
                try:
                    poller.set_rumble(int(extra.get("index", 0)), int(extra["left"]), int(extra["right"]))
                except Exception:
                    pass

    # ---- 内部：手柄 ----
    def set_mouse_mode(self, mode: str) -> None:
        self.set_input_focus(False)
        channel = self._control
        if channel is not None and channel.state == ChannelState.OPEN:
            channel.send_command(ControlCommand("mouse_mode", {"mode": mouse_mode(mode)}))

    def _start_gamepad_host(self) -> None:
        if not sys.platform.startswith("win") or self._pads is not None:
            return
        try:
            from backend.input_injection.windows.vigem import VirtualPads

            self._pads = VirtualPads(
                resource_dir() / "ViGEmClient.dll", feedback=self._gamepad_feedback
            )
            self.log.emit("手柄注入已开启（ViGEmBus / Xbox 360 兼容）", "info")
        except Exception as exc:
            self._pads = None
            self.log.emit(f"手柄注入不可用：{exc}", "warn")

    def _start_gamepad_viewer(self) -> None:
        if not sys.platform.startswith("win") or self._pad_poll is not None:
            return
        try:
            from backend.input_capture.windows.xinput import XInputPoller

            poller = XInputPoller(self._send_gamepad)
            if poller.start():
                self._pad_poll = poller
                self.log.emit("手柄转发已开启（XInput，最多 4 个手柄）", "info")
            else:
                self.log.emit("未找到 XInput 运行库，手柄转发不可用", "warn")
        except Exception as exc:
            self._pad_poll = None
            self.log.emit(f"手柄采集失败：{exc}", "warn")

    def _stop_gamepad(self) -> None:
        poller, self._pad_poll = self._pad_poll, None
        if poller is not None:
            try:
                poller.stop()
            except Exception:
                pass
        pads, self._pads = self._pads, None
        if pads is not None:
            try:
                pads.close()
            except Exception:
                pass

    def set_gamepad_enabled(self, enabled: bool, settings: ViewerSettings | HostSettings) -> None:
        if enabled:
            if self._mode == "viewer":
                self._start_gamepad_viewer()
            elif self._mode == "host":
                self._start_gamepad_host()
        elif self._mode == "viewer":
            if self._pad_poll is not None:
                self._stop_gamepad()
        elif self._mode == "host":
            if self._pads is not None:
                self._stop_gamepad()

    def _send_gamepad(self, index: int, values: list[int]) -> None:
        channel = self._control
        if channel is None:
            return
        try:
            channel.send_event(
                InputEvent(
                    event_type=EventType.GAMEPAD,
                    extra={"index": int(index), "values": [int(value) for value in values]},
                )
            )
        except Exception:
            return

    def _gamepad_feedback(self, index: int, left: int, right: int) -> None:
        channel = self._control
        if channel is None:
            return
        try:
            channel.send_event(
                InputEvent(
                    event_type=EventType.GAMEPAD,
                    extra={"index": int(index), "left": int(left), "right": int(right)},
                )
            )
        except Exception:
            return

    def _stop_control(self) -> None:
        self.set_input_focus(False)
        self._release_remote_input()
        control = self._control
        self._control = None
        if control is not None:
            try:
                control.close()
            except Exception:
                pass
        self.input_available.emit(False)

    # ---- 性能报告 ----
    def performance_report(self) -> dict:
        pipeline = self._pipeline
        pipeline_stats = getattr(pipeline, "stats", None)
        extra = getattr(pipeline_stats, "extra", {}) if pipeline_stats is not None else {}
        stages = {}
        for name in ("capture_ms", "queue_ms", "encode_ms", "decode_feed_ms", "decode_output_ms", "render_ms"):
            values = [float(value) for value in list(extra.get(name, [])) if value >= 0]
            if values:
                stages[name.removesuffix("_ms")] = {
                    "samples": len(values),
                    "p50_ms": round(_percentile(values, 50), 3),
                    "p95_ms": round(_percentile(values, 95), 3),
                    "p99_ms": round(_percentile(values, 99), 3),
                    "min_ms": round(min(values), 3),
                    "max_ms": round(max(values), 3),
                }
        channel = self._control
        return {
            "app": "elink",
            "version": __version__,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "mode": self._mode or "idle",
            "environment": {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "frozen": bool(getattr(sys, "frozen", False)),
                "processor": platform.processor(),
            },
            "settings": self._settings_snapshot,
            "session": asdict(self._stats),
            "stages": stages,
            "control_channel": channel.stats() if channel is not None else {},
            "limitations": [
                "stage timings cover pipeline encode/decode/render only, not network RTT",
                "samples come from the current or most recent session and are capped at 4096",
            ],
        }

    def export_performance(self, path: Path) -> Path:
        report = self.performance_report()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        self.log.emit(f"性能报告已导出：{path}", "info")
        return path


def _percentile(values: list[float], percent: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round((percent / 100.0) * (len(ordered) - 1)))))
    return ordered[index]


# ---- 配置构造 ----
def _display_region(index: int) -> tuple[int, int, int, int] | None:
    displays = list_displays()
    if not displays:
        return None
    chosen = displays[min(index, len(displays) - 1)]
    return chosen.region


def _host_config(settings: HostSettings) -> tuple[AppConfig, list]:
    region = _display_region(settings.display_index)
    config = AppConfig(mode="send")
    config.capture.region = region
    config.capture.fps = settings.fps
    config.encode.encoder = settings.encoder
    config.encode.codec = settings.codec
    config.encode.preset = "ultrafast" if settings.transport == "srt" and settings.preset == "veryfast" else settings.preset
    config.encode.realtime = settings.transport == "srt"
    config.encode.bitrate_kbps = settings.bitrate_kbps
    config.encode.ffmpeg_path = settings.ffmpeg_path
    config.encode.gop = max(1, int(settings.fps * 2))
    config.transport.kind = settings.transport
    config.transport.srt_url = settings.srt_url
    config.transport.srt_mode = settings.srt_mode
    config.transport.path = settings.file_path
    config.validate()

    filters: list = []
    if region is not None:
        width = min(settings.width, region[2]) & ~1
        height = min(settings.height, region[3]) & ~1
        config.encode.width, config.encode.height = width, height
        if (width, height) != (region[2], region[3]):
            filters.append(Scaler(width, height))
    return config, filters


def _viewer_config(settings: ViewerSettings) -> AppConfig:
    config = AppConfig(mode="receive")
    config.transport.kind = settings.transport
    config.transport.srt_url = settings.srt_url
    config.transport.srt_mode = settings.srt_mode
    config.transport.path = settings.file_path
    config.encode.ffmpeg_path = settings.ffmpeg_path
    config.encode.realtime = settings.transport == "srt"
    config.encode.codec = settings.stream_codec
    config.encode.decoder = settings.decoder
    if settings.transport == "srt":
        config.encode.width = settings.stream_width
        config.encode.height = settings.stream_height
        config.encode.framerate = settings.stream_fps
    config.capture.fps = settings.stream_fps
    config.validate()
    return config


def ensure_output_dir(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
