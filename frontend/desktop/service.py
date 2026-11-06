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
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import __version__

from PySide6.QtCore import QObject, Signal

from backend import clipboard as clipboard_backend
from backend.bootstrap import build_receive_resources, build_send_resources, prepare_environment
from backend.config.schema import AppConfig
from backend.control_channel.protocol import EventType, InputEvent
from backend.control_channel.tcp import TcpControlChannel
from backend.media.device.display import DisplayInfo, list_displays
from backend.media.device.enumerate import DeviceKind, enumerate_devices
from backend.media.frame import Frame
from backend.media.video.filter.pixel_format import BaseVideoFilter
from backend.media.video.filter.scaler import Scaler

from .paths import resource_dir

MAX_PREVIEW_FPS = 30.0


@dataclass
class HostSettings:
    """主机（被控）模式的界面参数。"""

    display_index: int = 0
    width: int = 1920
    height: int = 1080
    fps: float = 30.0
    bitrate_kbps: int = 4000
    encoder: str = "libx264"
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
    file_path: str = "out/host.ts"
    control_host: str = "127.0.0.1"
    control_port: int = 7000
    control_enabled: bool = False
    clipboard_enabled: bool = True
    gamepad_enabled: bool = True
    scale_percent: int = 100
    sr_enabled: bool = False
    sr_mode: str = "fsr"  # fsr / rtx
    sr_output: str = "1080p"  # 1080p / 1440p / 4K
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
    """把管线里的帧转交给界面（限速，避免信号队列堆积）。"""

    name = "preview_tap"

    def __init__(self, emit: Callable[[Frame], None], max_fps: float = MAX_PREVIEW_FPS) -> None:
        self._emit = emit
        self._interval = 1.0 / max(1.0, max_fps)
        self._last = 0.0
        self.frames = 0

    def process(self, frame: Frame) -> Frame:
        self.frames += 1
        now = time.monotonic()
        if now - self._last >= self._interval:
            self._last = now
            self._emit(frame)
        return frame


class _QtRenderer:
    """接收侧的渲染器：把解码帧交给界面（替代写 PNG 的 ImageSink）。"""

    name = "qt_preview"

    def __init__(self, emit: Callable[[Frame], None], upscaler=None) -> None:
        self._emit = emit
        self._upscaler = upscaler
        self.frames = 0
        self._interval = 1.0 / MAX_PREVIEW_FPS
        self._last = 0.0

    def open(self) -> None:
        pass

    def render(self, frame: Frame) -> bool:
        self.frames += 1
        now = time.monotonic()
        if now - self._last >= self._interval:
            self._last = now
            if self._upscaler is not None:
                try:
                    frame = self._upscaler.upscale(frame)
                except Exception as exc:
                    logger.warning("FSR 超分辨率失败，回退原帧: %s", exc)
                    self._upscaler = None
            self._emit(frame)
        return True

    def close(self) -> None:
        if self._upscaler is not None:
            try:
                self._upscaler.close()
            except Exception:
                pass
            self._upscaler = None


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

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self._pipeline = None
        self._control: TcpControlChannel | None = None
        self._injector = None
        self._hook = None
        self._clipboard: ClipboardBridge | None = None
        self._pads = None
        self._pad_poll = None
        self._settings_snapshot: dict | None = None
        self._stats = SessionStats()
        self._mode = ""
        self._started_at = 0.0
        self._devices_loading = False

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
        self._begin("host")
        config, filters = _host_config(settings)
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
        self._begin("viewer")
        config = _viewer_config(settings)
        self._worker = threading.Thread(
            target=self._run_viewer,
            args=(config, settings),
            name="viewer-session",
            daemon=True,
        )
        self._worker.start()

    def stop(self) -> None:
        if not self.running:
            return
        self._stop.set()
        self.state_changed.emit("stopping", "正在停止…")
        pipeline = self._pipeline
        if pipeline is not None and hasattr(pipeline, "stop"):
            try:
                pipeline.stop()
            except Exception:
                pass
        control = self._control
        if control is not None:
            try:
                control.close()
            except Exception:
                pass
        self._stop_control()

    # ---- 内部：公共 ----
    def _begin(self, mode: str) -> None:
        self._mode = mode
        self._stop.clear()
        self._stats = SessionStats()
        self._started_at = time.monotonic()
        self.state_changed.emit("running", "正在启动…")
        self.stats_changed.emit(self._stats)

    def _finish(self, reason: str = "") -> None:
        self._stop_clipboard()
        self._stop_gamepad()
        self._stop_control()
        self._pipeline = None
        self._mode = ""
        self.state_changed.emit("idle", reason)
        if self._started_at:
            self._stats.elapsed_s = round(time.monotonic() - self._started_at, 1)
        self.stats_changed.emit(self._stats)

    def _stats_loop(self, source, *, is_host: bool) -> None:
        """每秒把管线统计搬到界面（读取失败不影响主流程）。"""
        while not self._stop.wait(1.0):
            try:
                stats = getattr(source, "stats", None)
                if stats is None:
                    continue
                if is_host:
                    self._stats.frames_in = getattr(stats, "frames_captured", self._stats.frames_in)
                    self._stats.frames_out = getattr(stats, "frames_encoded", self._stats.frames_out)
                    self._stats.bytes_out = getattr(stats, "bytes_written", self._stats.bytes_out)
                    self._stats.key_frames = getattr(stats, "key_frames", self._stats.key_frames)
                    self._stats.fps = round(getattr(stats, "encoding_fps", 0.0), 1)
                    self._stats.bitrate_kbps = round(getattr(stats, "bitrate_kbps", 0.0), 0)
                else:
                    self._stats.frames_in = getattr(stats, "packets_read", self._stats.frames_in)
                    self._stats.frames_out = getattr(stats, "frames_decoded", self._stats.frames_out)
                    self._stats.key_frames = getattr(stats, "key_frames", self._stats.key_frames)
                    self._stats.fps = round(getattr(stats, "decode_fps", 0.0), 1)
                self._stats.media_seconds = round(getattr(stats, "media_seconds", 0.0), 2)
                self._stats.elapsed_s = round(time.monotonic() - self._started_at, 1)
                self.stats_changed.emit(self._stats)
            except Exception:
                continue

    # ---- 内部：主机 ----
    def _run_host(self, config: AppConfig, filters: list, settings: HostSettings) -> None:
        runtime = None
        try:
            _probe, registry = prepare_environment(config)
            runtime = build_send_resources(config, registry)
            pipeline = runtime.get("pipeline")
            self._pipeline = pipeline

            tap = _PreviewTap(self.frame_ready.emit)
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
            threading.Thread(
                target=self._stats_loop, args=(pipeline,), kwargs={"is_host": True}, daemon=True
            ).start()

            stats = pipeline.run()
            self.log.emit(f"发送结束：{stats.summary()}", "info")
            self._stats.bytes_out = getattr(stats, "bytes_written", self._stats.bytes_out)
        except Exception as exc:
            self.log.emit(f"主机模式失败：{exc}", "error")
            self.state_changed.emit("error", str(exc))
        finally:
            if runtime is not None:
                runtime.close()
            self._finish("发送已停止")

    def _run_viewer(self, config: AppConfig, settings: ViewerSettings) -> None:
        runtime = None
        try:
            _probe, registry = prepare_environment(config)
            runtime = build_receive_resources(config, registry)
            pipeline = runtime.get("pipeline")
            self._pipeline = pipeline

            # 换成把帧交给界面的渲染器（原来的写图渲染器不再使用）
            upscaler = None
            if settings.sr_enabled:
                from .upscale import FSR_OUTPUTS, FsrUpscaler, RtxUpscaler

                output = FSR_OUTPUTS.get(settings.sr_output, (1920, 1080))
                if settings.sr_mode == "rtx":
                    try:
                        if RtxUpscaler.available():
                            upscaler = RtxUpscaler(output)
                            self.log.emit(
                                f"RTX VSR 超分辨率已开启（输出 {output[0]}×{output[1]}）", "info"
                            )
                        else:
                            self.log.emit("RTX VSR 不可用（需要 NVIDIA RTX），尝试 FSR 1", "warn")
                    except Exception as exc:
                        self.log.emit(f"RTX VSR 不可用，尝试 FSR 1：{exc}", "warn")
                if upscaler is None:
                    try:
                        upscaler = FsrUpscaler(output)
                        self.log.emit(
                            f"FSR 1 超分辨率已开启（输出 {output[0]}×{output[1]}）", "info"
                        )
                    except Exception as exc:
                        self.log.emit(f"超分辨率不可用，回退原帧：{exc}", "warn")
            pipeline.renderer = _QtRenderer(self.frame_ready.emit, upscaler)

            self._start_control_client(settings)
            if settings.clipboard_enabled:
                self._start_clipboard()
            if settings.gamepad_enabled:
                self._start_gamepad_viewer()
            self.log.emit(f"观看模式已启动：{settings.srt_url if settings.transport == 'srt' else settings.file_path}", "info")
            threading.Thread(
                target=self._stats_loop, args=(pipeline,), kwargs={"is_host": False}, daemon=True
            ).start()

            stats = pipeline.run()
            self.log.emit(f"接收结束：{stats.summary()}", "info")
        except Exception as exc:
            self.log.emit(f"观看模式失败：{exc}", "error")
            self.state_changed.emit("error", str(exc))
        finally:
            if runtime is not None:
                runtime.close()
            self._finish("接收已停止")

    # ---- 内部：控制通道 ----
    def _start_control_server(self, settings: HostSettings) -> None:
        def run() -> None:
            try:
                channel = TcpControlChannel(
                    "0.0.0.0", settings.control_port, server_side=True, timeout_s=30.0
                )
                self._control = channel
                self.control_changed.emit(f"等待控制端连接 :{settings.control_port}")
                channel.open()
                self.control_changed.emit(f"控制端已接入 :{settings.control_port}")
                channel.on_message(lambda message: self._on_remote_input(message, settings))
                while not self._stop.is_set():
                    time.sleep(0.2)
            except Exception as exc:
                if not self._stop.is_set():
                    self.log.emit(f"控制通道异常：{exc}", "warn")
                    self.control_changed.emit("控制通道未连接")

        threading.Thread(target=run, name="control-server", daemon=True).start()

    def _start_control_client(self, settings: ViewerSettings) -> None:
        def run() -> None:
            try:
                channel = TcpControlChannel(
                    settings.control_host, settings.control_port, timeout_s=5.0
                )
                self._control = channel
                channel.on_message(lambda message: self._on_remote_event(message, settings))
                channel.open()
                self.control_changed.emit(f"已连接控制通道 {settings.control_host}:{settings.control_port}")
                if settings.control_enabled:
                    self._start_local_capture(settings)
                while not self._stop.is_set():
                    time.sleep(0.2)
            except Exception as exc:
                self.log.emit(f"控制通道连接失败：{exc}", "warn")
                self.control_changed.emit("控制通道未连接")

        threading.Thread(target=run, name="control-client", daemon=True).start()

    def set_control_enabled(self, enabled: bool, settings: ViewerSettings) -> None:
        """观看模式下切换「本机输入转发」。"""
        if enabled:
            self._start_local_capture(settings)
        else:
            self._stop_local_capture()

    def _start_local_capture(self, settings: ViewerSettings) -> None:
        if self._hook is not None:
            return
        try:
            from backend.input_capture.windows.send_input_hook import SendInputHookCapture

            hook = SendInputHookCapture(capture_injected=False)
            hook.open()
            self._hook = hook
            self.log.emit("已开始捕获本机输入（仅在画面点击后生效）", "info")
            self._filter_capture_thread = threading.Thread(
                target=self._forward_loop, name="input-forward", daemon=True
            )
            self._filter_capture_thread.start()
            self.control_changed.emit("本机输入转发已开启")
        except Exception as exc:
            self.log.emit(f"输入捕获不可用：{exc}", "warn")

    def _forward_loop(self) -> None:
        hook = self._hook
        while hook is not None and not self._stop.is_set():
            event = hook.read(timeout_s=0.2)
            channel = self._control
            if event is None or channel is None:
                continue
            try:
                channel.send_event(event)
            except Exception:
                continue

    def _stop_local_capture(self) -> None:
        hook = self._hook
        self._hook = None
        if hook is not None:
            try:
                hook.close()
            except Exception:
                pass
            self.control_changed.emit("本机输入转发已关闭")

    def _on_remote_input(self, message, settings: HostSettings) -> None:
        if not isinstance(message, InputEvent):
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
        if not settings.allow_inject:
            return
        try:
            if self._injector is None:
                from backend.input_injection.windows.send_input import SendInputInjector

                self._injector = SendInputInjector()
            if self._injector.inject(message):
                self._stats.injected_events += 1
        except Exception as exc:
            self.log.emit(f"输入注入失败：{exc}", "warn")

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
        self._stop_local_capture()
        control = self._control
        self._control = None
        if control is not None:
            try:
                control.close()
            except Exception:
                pass

    # ---- 性能报告 ----
    def performance_report(self) -> dict:
        pipeline = self._pipeline
        pipeline_stats = getattr(pipeline, "stats", None)
        extra = getattr(pipeline_stats, "extra", {}) if pipeline_stats is not None else {}
        stages = {}
        for name in ("encode_ms", "decode_ms", "render_ms"):
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
    config.encode.preset = settings.preset
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
        if (width, height) != (region[2], region[3]):
            filters.append(Scaler(width, height))
    return config, filters


def _viewer_config(settings: ViewerSettings) -> AppConfig:
    config = AppConfig(mode="receive")
    config.transport.kind = settings.transport
    config.transport.srt_url = settings.srt_url
    config.transport.path = settings.file_path
    config.encode.ffmpeg_path = settings.ffmpeg_path
    config.validate()
    return config


def ensure_output_dir(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
