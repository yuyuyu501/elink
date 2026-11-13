"""主窗口：自绘标题栏 + 分组侧栏 + 页面栈 + 状态页脚。

窗口只负责「把页面和服务连起来」：

- 页面发出信号 → 窗口构造设置 → 调 ``SessionService``；
- 服务发出 ``frame_ready`` → 窗口转发给对应页面的预览控件；
- 关闭窗口时先停会话再保存设置，避免 ffmpeg 子进程残留。

视觉上参考网易 UU 远程 4.x：无系统标题栏、冷白底、窄侧栏、
白色圆角卡片与亮蓝主色；功能上仍是 elink 自己的主机 / 观看 / 设备。
"""

from __future__ import annotations

import socket
import sys
import time
from collections import deque
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QEvent, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QCloseEvent, QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QMainWindow,
    QMenu,
    QPushButton,
    QSizeGrip,
    QStackedWidget,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from .. import __version__, icons, settings as user_settings, startup
from ..discovery import DiscoveryService
from ..connection import DesktopConnection, saved_picture
from ..error_report import remember, save_error
from ..service import HostSettings, SessionService, SessionStats, ViewerSettings
from ..theme import ACCENT, MUTED
from .devices_page import DevicesPage
from .pages import ENCODERS, PRESETS, RESOLUTIONS, HostPage, OverviewPage
from .preview import PreviewFrame
from .settings_page import SettingsPage
from .stream_window import PictureDialog, StreamWindow
from .widgets import ClickableCard, IconTile, NoticeBar, StatusDot, TitleBar, divider, hint, label

NAV_ITEMS = [
    ("overview", "总览", "activity"),
    ("host", "主机模式", "monitor"),
    ("devices", "我的设备", "monitor"),
]
NAV_INDEX = {key: index for index, (key, _caption, _icon) in enumerate(NAV_ITEMS)}
SETTINGS_INDEX = len(NAV_ITEMS)
PAGE_INDEX = {**NAV_INDEX, "settings": SETTINGS_INDEX}


class MainWindow(QMainWindow):
    """应用主窗口。"""

    shutdown_complete = Signal()

    def __init__(self, root: Path | None = None, *, auto_refresh: bool = True) -> None:
        super().__init__()
        self.root = root or user_settings.data_dir()
        self.service = SessionService(self)
        self.service.upscale_changed.connect(self._upscale_capabilities)
        self._auto_refresh = auto_refresh
        self._native_frame_patched = False
        self._active_mode = ""  # 最近一次启动的模式：帧/统计可能晚于 mode 清零到达
        self._viewer_from_devices = False
        self._exit_requested = False
        self._tray = None
        self._tray_notified = False
        self._discovery_message = ""
        self._stats_overlay_enabled = False
        self._last_stats = None
        self._loading_settings = False
        self._recent_logs: deque[tuple[str, str, str]] = deque(maxlen=20)
        self._stream_window: StreamWindow | None = None
        self.ui_settings = user_settings.load_ui()
        self.discovery = DiscoveryService(self)
        self.discovery.peers_changed.connect(self._on_peers)
        self.discovery.status_changed.connect(self._on_discovery_status)
        app = QApplication.instance()
        if app is not None:
            app.setQuitOnLastWindowClosed(False)

        self.setWindowTitle("elink · 远程控制")
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowSystemMenuHint
            | Qt.WindowType.WindowMinMaxButtonsHint
        )
        self.resize(1120, 740)
        self.setMinimumSize(QSize(1080, 700))

        host_settings, viewer_settings = user_settings.load()
        self._host_settings = host_settings
        self._viewer_settings = viewer_settings
        self._stats_overlay_enabled = viewer_settings.stats_visible
        self.connection = DesktopConnection(
            self.service, self.discovery.identity, host_settings, self
        )

        central = QFrame(self)
        central.setObjectName("windowRoot")
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.title_bar = TitleBar(self)
        self.title_bar.refresh_button.hide()
        outer.addWidget(self.title_bar)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._build_sidebar())
        body.addWidget(self._build_content(), 1)
        outer.addLayout(body, 1)

        outer.addWidget(self._build_footer())
        self.setCentralWidget(central)
        self._wire()
        self._apply_settings(host_settings, viewer_settings)
        self._apply_ui_settings()
        self._show_page("devices")

        self.log("界面已就绪", "info")
        if auto_refresh:
            self._refresh_devices()

    # ---- 构建：侧栏 ----
    def _build_sidebar(self) -> QWidget:
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(210)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(12, 14, 12, 12)
        layout.setSpacing(6)

        device = ClickableCard("deviceCard")
        device.setFixedHeight(64)
        hostname = socket.gethostname() or "this device"
        device.setToolTip(f"本机 · {hostname}（返回设备列表）")
        device_row = QHBoxLayout(device)
        device_row.setContentsMargins(12, 10, 10, 10)
        device_row.setSpacing(10)
        device_row.addWidget(IconTile("monitor", "blue", 34))

        text_box = QVBoxLayout()
        text_box.setSpacing(2)
        text_box.addWidget(label("本机", "cardTitle"))
        host_text = hint(_platform_text())
        host_text.setMaximumWidth(126)
        text_box.addWidget(host_text)
        device_row.addLayout(text_box, 1)

        self.sidebar_dot = StatusDot("ok")
        device_row.addWidget(self.sidebar_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        device_row.addWidget(icons.icon_label("chevron-right", 13, "#A9B4C4"))
        device.clicked.connect(self._show_device_list)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self._nav_buttons: dict[str, QPushButton] = {}
        self._nav_icons: dict[str, str] = {}

        layout.addWidget(label("工作空间", "navSection"))
        self._add_nav(sidebar, layout, "devices")

        layout.addStretch(1)
        layout.addWidget(device)
        layout.addWidget(divider())

        settings_button = QPushButton("设置")
        settings_button.setObjectName("navItem")
        settings_button.setCheckable(True)
        settings_button.setIcon(icons.icon("settings", MUTED))
        settings_button.setIconSize(QSize(17, 17))
        settings_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.nav_group.addButton(settings_button, SETTINGS_INDEX)
        self._nav_buttons["settings"] = settings_button
        self._nav_icons["settings"] = "settings"
        layout.addWidget(settings_button)
        return sidebar

    def _add_nav(self, sidebar: QWidget, layout: QVBoxLayout, key: str) -> None:
        _key, caption, icon_name = next(item for item in NAV_ITEMS if item[0] == key)
        button = QPushButton(caption)
        button.setObjectName("navItem")
        button.setCheckable(True)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setIcon(icons.icon(icon_name, ACCENT if key == "devices" else MUTED))
        button.setIconSize(QSize(17, 17))
        if key == "devices":
            button.setChecked(True)
        self.nav_group.addButton(button, NAV_INDEX[key])
        self._nav_buttons[key] = button
        self._nav_icons[key] = icon_name
        layout.addWidget(button)

    # ---- 构建：内容 ----
    def _build_content(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(24, 18, 24, 14)
        layout.setSpacing(0)

        displays = [display.describe() for display in self.service.displays()]
        self._display_count = len(displays)
        self.stack = QStackedWidget()

        self.overview_page = OverviewPage(
            display_count=len(displays), control_port=self._host_settings.control_port
        )
        self.host_page = HostPage(displays)
        self.devices_page = DevicesPage()
        self.settings_page = SettingsPage(self.root)

        self.notice_bar = NoticeBar()
        layout.addWidget(self.notice_bar)
        layout.addSpacing(4)

        for page in (
            self.overview_page,
            self.host_page,
            self.devices_page,
            self.settings_page,
        ):
            self.stack.addWidget(page)
        layout.addWidget(self.stack)
        return container

    # ---- 构建：页脚 ----
    def _build_footer(self) -> QWidget:
        footer = QFrame()
        footer.setObjectName("footer")
        footer.setFixedHeight(30)
        row = QHBoxLayout(footer)
        row.setContentsMargins(16, 0, 4, 0)
        row.setSpacing(8)

        self.status_dot = StatusDot("idle")
        row.addWidget(self.status_dot)
        self.status_label = label("空闲", "statusText")
        row.addWidget(self.status_label)
        row.addStretch(1)
        self.control_label = label("控制通道未连接", "caption")
        self.control_label.setToolTip("控制通道未连接")
        row.addWidget(self.control_label)
        row.addWidget(divider(vertical=True, length=16))
        version = label(f"v{__version__} · PID {_pid()}", "caption")
        row.addWidget(version)
        grip = QSizeGrip(footer)
        grip.setFixedSize(16, 16)
        row.addWidget(grip, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom)
        return footer

    # ---- 连接 ----
    def _wire(self) -> None:
        self.nav_group.idClicked.connect(
            lambda index: self._show_device_list()
            if index == PAGE_INDEX["devices"] else self._on_nav(index)
        )
        self.title_bar.minimize_clicked.connect(self.showMinimized)
        self.title_bar.maximize_clicked.connect(self._toggle_maximize)
        self.title_bar.close_clicked.connect(self.close)
        self.title_bar.refresh_clicked.connect(self._refresh_devices)

        self.overview_page.navigate.connect(self._show_page)
        self.overview_page.start_requested.connect(
            lambda: self._start_host(self.host_page.settings(), navigate=True)
        )
        self.overview_page.snapshot_requested.connect(self._snapshot_current)
        self.overview_page.preset_selected.connect(self._apply_overview_preset)

        self.host_page.start_requested.connect(lambda settings: self._start_host(settings, navigate=True))
        self.host_page.stop_requested.connect(self.service.stop)
        self.host_page.back_requested.connect(lambda: self._show_page("overview"))
        self.host_page.clipboard_toggled.connect(lambda enabled: self._toggle_clipboard("host", enabled))
        self.host_page.report_requested.connect(lambda: self._export_report("host"))
        self.devices_page.connect_peer.connect(self._connect_peer)
        self.connection.error.connect(lambda message: self.log(
            message, "error", fault_id=self.connection.fault_id,
            source=self.connection._fault_source))
        self.connection.changed.connect(self._connection_changed)
        self.connection.connected.connect(self._session_connected)
        self.connection.picture_applied.connect(self._picture_applied)
        self.connection.codec_selection_required.connect(self._edit_picture)
        self.connection.mode_applied.connect(self._mode_applied)
        self.connection.finished.connect(self._session_finished)

        settings = self.settings_page
        settings.startup_changed.connect(self._change_startup)
        settings.tray_changed.connect(self._change_tray)
        settings.open_data_requested.connect(lambda: self._open_dir(self.root))
        settings.open_logs_requested.connect(lambda: self._open_dir(self.root / "logs"))
        settings.copy_diagnostics_requested.connect(self._copy_diagnostics)

        self.host_page.snapshot_button.clicked.connect(
            lambda: self._snapshot(self.host_page.preview_frame)
        )
        self.service.frame_ready.connect(self._on_frame)
        self.service.cursor_changed.connect(self._on_cursor)
        self.service.state_changed.connect(self._on_state)
        self.service.log.connect(lambda message, level: self.log(
            message, level, fault_id=self.service.failure_id))
        self.service.stats_changed.connect(self._on_stats)
        self.service.devices_changed.connect(self._on_devices)
        self.service.control_changed.connect(self._on_control)
        self.service.input_available.connect(self._on_input_available)

    # ---- 服务回调 ----
    def _show_device_list(self) -> None:
        self.devices_page.show_list()
        self._show_page("devices")

    def _start_host(self, settings: HostSettings, *, navigate: bool = False) -> None:
        if self.service.running:
            self._show_page("host")
            return
        self._host_settings = settings
        self._active_mode = "host"
        self.host_page.preview_frame.preview.clear()
        self.host_page.preview_frame.preview.set_badge("本机屏幕")
        if navigate:
            self._show_page("host")
        self.service.start_host(settings)

    def _export_report(self, mode: str) -> None:
        if not self.service.running:
            self.notice_bar.show_message("当前没有运行中的会话，无法导出性能报告。", "warn")
            return
        target, _filter = QFileDialog.getSaveFileName(
            self._stream_window or self,
            "导出性能报告",
            str(user_settings.captures_dir() / f"elink-{mode}-report-{time.strftime('%Y%m%d-%H%M%S')}.json"),
            "JSON (*.json)",
        )
        if not target:
            return
        try:
            path = self.service.export_performance(Path(target))
        except OSError as exc:
            self.notice_bar.show_message(f"性能报告写入失败：{exc}", "error")
            return
        self.notice_bar.show_message(f"性能报告已保存：{path}", "info")

    def _toggle_clipboard(self, mode: str, enabled: bool) -> None:
        if mode == "host":
            self._host_settings.clipboard_enabled = enabled
        else:
            self._viewer_settings.clipboard_enabled = enabled
        if self.service.running and self.service.mode == mode:
            self.service.set_clipboard_enabled(enabled)
        if not self._loading_settings:
            self.log("剪贴板同步已" + ("开启" if enabled else "关闭"), "info")

    def _toggle_control(self, enabled: bool) -> None:
        if self._loading_settings:
            return
        if self.connection.role == "viewer" and self.connection.busy:
            if not enabled:
                self.service.set_control_enabled(False, self._viewer_settings)
            self.connection.set_control(enabled)
            return
        self._viewer_settings.control_enabled = enabled
        if self.service.running and self.service.mode == "viewer":
            self.service.set_control_enabled(enabled, self._viewer_settings)
        else:
            self.log("键鼠接管已" + ("开启" if enabled else "关闭") + "（连接后生效）", "info")

    def _on_frame(self, frame) -> None:
        # 用 _active_mode 而不是 service.mode：文件回放可能在 idle 之后才把排队帧送到界面
        if self._active_mode == "host":
            self.host_page.preview_frame.preview.set_frame(frame)
        elif self._active_mode == "viewer":
            if self._stream_window is not None:
                self._stream_window.set_frame(frame)

    def _on_state(self, state: str, detail: str) -> None:
        if state == "idle" and self.service.running:
            return
        running = state in {"running", "stopping"}
        self.host_page.set_running(running)
        if self.service.mode == "host":
            self.host_page.set_state(state, detail)
        else:
            self.host_page.set_state("idle", "")
        self.overview_page.set_state(state, detail)

        tone = {"running": "ok", "idle": "idle", "stopping": "busy", "error": "error"}.get(state, "idle")
        text = {"running": "运行中", "idle": "空闲", "stopping": "停止中", "error": "出错"}.get(state, state)
        self.status_dot.set_tone(tone)
        self.status_label.setText(f"{text}{' · ' + detail if detail else ''}")
        self.title_bar.status_hint.setText(text)

        if self._stream_window is not None:
            stream_tone = {"running": "ok", "idle": "idle", "stopping": "busy", "error": "error"}.get(
                state, "idle"
            )
            self._stream_window.set_status(text, stream_tone)
        if state == "idle" and self.connection.phase in {"idle", "stopping"}:
            if self.service.mode == "":
                self.host_page.preview_frame.preview.set_info("")
            self._on_control("控制通道未连接")
            self._close_stream_window()
        if state == "error":
            self.log(detail or "未知错误", "error",
                     fault_id=self.service.failure_id or self.connection.fault_id)
        self._sync_discovery_hosting(state)

    def _on_stats(self, stats: SessionStats) -> None:
        self._last_stats = stats
        self.overview_page.set_stats(stats)
        if self._active_mode == "host":
            self.host_page.show_stats(stats)
        elif self._active_mode == "viewer":
            if self._stream_window is not None:
                self._stream_window.set_stats_lines(self._stats_lines(stats))
            if self._stats_overlay_enabled:
                self._update_stats_overlay()

    def _stats_lines(self, stats: SessionStats | None = None) -> list[str]:
        stats = stats if stats is not None else self._last_stats
        if stats is None:
            return ["等待会话统计…"]
        now = time.monotonic()
        extra = stats.extra
        fresh = now - extra.get("sampled_at", 0) < 3
        fps = f"{stats.fps:.1f}" if fresh else "--"
        bitrate = f"{stats.bitrate_kbps / 1000:.2f}" if fresh else "--"
        rtt = f"{extra['rtt_ms']:.1f}" if now - extra.get("rtt_sampled_at", 0) < 3 else "--"
        decode = f"{extra['decode_ms']:.1f}" if fresh and "decode_ms" in extra else "--"
        encoder = extra.get("encoder", "--") if now - extra.get("host_sampled_at", 0) < 3 else "--"
        decoder = extra.get("decoder", "--") if fresh else "--"
        codec = {"h264": "H.264", "hevc": "H.265", "av1": "AV1"}.get(extra.get("codec"), "--")
        return [f"{fps} FPS", f"{bitrate} Mbps", f"RTT {rtt} ms", f"解码 {decode} ms",
                codec, f"编码 {encoder}", f"解码 {decoder}", "丢包 --"]

    def _on_cursor(self, state) -> None:
        if self._stream_window is not None:
            self._stream_window.preview.set_cursor_state(state)

    def _update_stats_overlay(self) -> None:
        lines = self._stats_lines()
        if self._stream_window is not None:
            self._stream_window.set_stats_lines(lines)

    def _open_stream_window(self) -> None:
        if self._stream_window is not None:
            self._stream_window.show()
            self._stream_window.raise_()
            self._stream_window.activateWindow()
            return
        window = StreamWindow(self)
        window.set_stats_visible(self._stats_overlay_enabled)
        window.set_stats_lines(self._stats_lines())
        window.set_status(self.status_label.text(), self.status_dot.property("tone") or "ok")
        window.stats_toggled.connect(self._toggle_stats_overlay)
        window.picture_requested.connect(self._edit_picture)
        window.watch_toggled.connect(lambda watching: self._toggle_control(not watching))
        window.snapshot_requested.connect(self._snapshot_current)
        window.report_requested.connect(lambda: self._export_report("viewer"))
        window.preference_changed.connect(self._viewer_preference)
        window.upscale_refresh.connect(self.service.refresh_upscale)
        window.upscale_cancel.connect(self.service.cancel_upscale_probe)
        window.set_upscale_capabilities(self.service.upscale_capabilities, self.service.upscale_status)
        window.preview.bind_input(self.service.send_input, self.service.set_input_focus)
        window.preview.input_error.connect(self.service._input_failure)
        window.preview.presented.connect(self.service.frame_presented)
        window.apply_settings(self._viewer_settings)
        window.closed.connect(self._on_stream_closed)
        self._stream_window = window
        window.show()
        window.activateWindow()

    def _upscale_capabilities(self):
        if self._stream_window is not None:
            self._stream_window.set_upscale_capabilities(
                self.service.upscale_capabilities, self.service.upscale_status)

    def _on_stream_closed(self) -> None:
        if self._stream_window is None:
            return
        self._stream_window = None
        self._disconnect_session()
        self._show_device_list()
        self.raise_()
        self.activateWindow()

    def _close_stream_window(self) -> None:
        window, self._stream_window = self._stream_window, None
        if window is not None:
            window.close()

    def _toggle_stats_overlay(self, enabled: bool) -> None:
        self._stats_overlay_enabled = bool(enabled)
        self._viewer_settings.stats_visible = self._stats_overlay_enabled
        self._persist_ui()
        if self._stream_window is not None:
            self._stream_window.set_stats_visible(self._stats_overlay_enabled)
        if self._stats_overlay_enabled:
            self._update_stats_overlay()

    def _release_viewer_input(self) -> None:
        if self._active_mode != "viewer" or not self.service.running:
            self.notice_bar.show_message("释放输入仅在观看会话中可用。", "warn")
            return
        if self._stream_window is not None:
            self._stream_window.release_input()
        self.log("已释放键鼠输入（F8）", "info")

    def _on_control(self, text: str) -> None:
        self.control_label.setText(text or "控制通道未连接")
        self.control_label.setToolTip(text)
        self.overview_page.set_control(text)

    def _on_devices(self, items: list[dict]) -> None:
        self.devices_page.set_devices(items)
        displays = sum(1 for item in items if item.get("kind") == "display")
        hostname = socket.gethostname() or "本机"
        self.overview_page.update_environment(
            platform_text=_platform_text(),
            display_count=displays,
            control_port=int(self.host_page.control_port.value()),
        )
        self.overview_page.identity.setText(f"本机 · {hostname}")

    # ---- 设备发现 ----
    def _refresh_devices(self) -> None:
        if self.discovery.running:
            self.devices_page.set_discovery_state(True, "正在搜索同网设备…")
        self.discovery.refresh()
        self.service.refresh_devices_async()

    def _on_peers(self, peers: list[dict]) -> None:
        self.devices_page.set_peers(peers)
        if self.connection.busy:
            self.devices_page.enter_button.setEnabled(False)
            self.devices_page.watch_button.setEnabled(False)

    def _on_discovery_status(self, message: str) -> None:
        self._discovery_message = message
        enabled = self.discovery.running
        hint = "" if message.startswith("设备发现已") else message
        self.devices_page.set_discovery_state(enabled, hint)
        self.settings_page.set_discovery_state(enabled, message)
        if "不可用" in message:
            self.notice_bar.show_message(message, "error")

    def _sync_discovery_hosting(self, state: str | None = None) -> None:
        if state is None:
            state = "running" if self.service.running else "idle"
        hosting = state in {"running", "stopping"} and self.service.mode == "host"
        settings = self._host_settings
        srt_ready = settings.transport == "srt"
        port = _srt_port(settings.srt_url) if srt_ready else 0
        self.discovery.set_hosting(hosting, port, settings.control_port)
        self.devices_page.set_local_state(
            hosting, settings.srt_url, settings.control_port,
            srt_ready=self.connection.server.isListening()
        )
        self.discovery.set_connection(
            self.connection.server.serverPort() if self.connection.server.isListening() else 0,
            self.connection.busy,
        )

    def _connect_peer(self, peer: dict, control_enabled: bool = True) -> None:
        if self.connection.busy:
            self.notice_bar.show_message("已有一条会话在运行，请先停止后再连接。", "warn")
            return
        peer = next((item for item in self.discovery.peers() if item["id"] == peer.get("id")), None)
        if peer is None:
            self.notice_bar.show_message("设备已离线，请等待重新发现。", "warn")
            return
        if not peer.get("session_port") or peer.get("busy"):
            self.notice_bar.show_message("对方连接监听未就绪或正在使用中。", "warn")
            return
        self._viewer_from_devices = True
        self._viewer_settings.control_enabled = control_enabled
        self._active_mode = "viewer"
        self._open_stream_window()
        self.connection.connect_peer(peer, self._viewer_settings, control_enabled)
        if self.connection.phase == "idle":
            self._close_stream_window()

    def _connection_changed(self) -> None:
        phase = self.connection.phase
        if phase == "active" and self.connection.role == "viewer":
            for name in ("sr_enabled", "sr_mode", "sr_percent", "sr_sharpen", "sr_sharpness"):
                setattr(self._viewer_settings, name, getattr(self.connection._viewer, name))
            if self._stream_window is not None:
                self._stream_window.apply_settings(self._viewer_settings)
        self._on_peers(self.discovery.peers())
        if self._stream_window:
            self._stream_window.set_session_active(phase == "active")
            self._stream_window.set_status(
                "已连接" if phase == "active" else "调整中" if phase in {"updating", "validating", "negotiating"} else "连接中",
                "ok" if phase == "active" else "busy",
            )
            if phase == "updating":
                self._stream_window.preview.clear()
            elif phase in {"stopping", "idle"}:
                self._close_stream_window()
        if phase in {"connecting", "preparing", "receiving", "updating"}:
            self.notice_bar.show_message("正在调整画面…" if phase == "updating" else "正在建立桌面连接…", "info")
        elif (phase in {"stopping", "idle"} and not self.connection.fault_id
              and (phase == "stopping" or self.notice_bar._message in {"桌面已连接", "正在断开连接…"})):
            self.notice_bar.show_message("正在断开连接…" if phase == "stopping" else "桌面已断开", "info")
        self._sync_discovery_hosting()

    def _session_connected(self, settings: ViewerSettings) -> None:
        self._viewer_settings = replace(settings, stream_width=settings.output_width,
                                        stream_height=settings.output_height) if settings.sr_enabled else replace(settings)
        self._active_mode = "viewer"
        self._open_stream_window()
        self._stream_window.apply_settings(self._viewer_settings)
        self._stream_window.preview.request_mouse_mode()
        self._show_page("devices")

    def _session_finished(self) -> None:
        self._close_stream_window()
        self.devices_page.set_peers(self.discovery.peers())
        if self._viewer_from_devices:
            self._show_page("devices")

    def _disconnect_session(self) -> None:
        if self._stream_window is not None:
            self._stream_window.release_input()
        if self.connection.phase != "idle":
            self.connection.disconnect()
        else:
            self.service.stop()

    def _mode_applied(self, enabled: bool) -> None:
        self._viewer_settings.control_enabled = enabled
        if self._stream_window is not None:
            self._stream_window.apply_settings(self._viewer_settings)
        self.service.set_control_enabled(enabled, self._viewer_settings)
        self.service.set_clipboard_enabled(enabled and self._viewer_settings.clipboard_enabled)
        self.service.set_gamepad_enabled(enabled and self._viewer_settings.gamepad_enabled,
                                         self._viewer_settings)
        self._persist_ui()

    def _on_input_available(self, available: bool) -> None:
        if self._stream_window is not None:
            self._stream_window.set_input_available(available)

    def _viewer_preference(self, name: str, value) -> None:
        if self._stream_window is not None:
            self._stream_window.release_input()
        if name not in {"mouse_mode", "mouse_sensitivity", "clipboard_enabled", "gamepad_enabled",
                        "sr_enabled", "sr_mode", "sr_percent", "sr_sharpen", "sr_sharpness"}:
            return
        previous = replace(self._viewer_settings)
        setattr(self._viewer_settings, name, value)
        if name.startswith("sr_") and not self._viewer_settings.sr_enabled and name != "sr_enabled":
            setattr(self.connection._viewer, name, value)
            self._persist_ui()
            if self._stream_window is not None:
                self._stream_window.apply_settings(self._viewer_settings)
            return
        if not name.startswith("sr_"):
            setattr(self.connection._viewer, name, value)
        if name == "clipboard_enabled":
            self.service.set_clipboard_enabled(value and self._viewer_settings.control_enabled)
        elif name == "gamepad_enabled":
            self.service.set_gamepad_enabled(value and self._viewer_settings.control_enabled,
                                            self._viewer_settings)
        elif name.startswith("sr_") and self.connection.phase == "active":
            self.connection.configure(saved_picture(self._viewer_settings), upscale=self._viewer_settings)
            if self.connection.phase == "active":
                self._viewer_settings = previous
        if self._stream_window is not None:
            self._stream_window.apply_settings(self._viewer_settings)
            self._stream_window.set_session_active(self.connection.phase == "active")
        if name == "mouse_mode":
            if self._stream_window is not None:
                self._stream_window.preview.request_mouse_mode()
            self.service.set_mouse_mode(value)
        if not name.startswith("sr_") or self.connection.phase == "idle":
            self._persist_ui()

    def _picture_applied(self, picture: dict) -> None:
        for name in ("sr_enabled", "sr_mode", "sr_percent", "sr_sharpen", "sr_sharpness"):
            setattr(self._viewer_settings, name, getattr(self.connection._viewer, name))
        for name, value in picture.items():
            setattr(self._viewer_settings, f"stream_{name}", value)
        self._persist_ui()
        self.notice_bar.show_message("桌面已连接", "info")

    def _edit_picture(self) -> None:
        if self.connection.role != "viewer" or self.connection.phase not in {"active", "selection"}:
            return
        self._release_viewer_input()
        dialog = PictureDialog(saved_picture(self._viewer_settings), self._stream_window or self,
                               connection=self.connection)
        accepted = dialog.exec()
        if accepted:
            self.connection.configure(dialog.picture())
        elif self.connection.phase == "selection":
            self.connection.disconnect()
        dialog.deleteLater()

    # ---- 设置 / 托盘 / 发现开关 ----
    def _apply_ui_settings(self) -> None:
        tray_available = QSystemTrayIcon.isSystemTrayAvailable()
        if not tray_available:
            self.ui_settings.close_to_tray = False
        self.settings_page.set_tray_available(tray_available)
        self.settings_page.set_startup_available(startup.supports_startup())
        actual_startup = startup.is_enabled()
        self.ui_settings.start_on_login = actual_startup
        self.settings_page.set_startup_state(actual_startup)
        self.settings_page.set_runtime(
            version=__version__,
            build="打包版" if getattr(sys, "frozen", False) else "源码运行",
        )
        self.settings_page.set_ui(self.ui_settings)
        self._setup_tray(self.ui_settings.close_to_tray)
        self.connection.start()
        self.discovery.start()
        self.devices_page.set_discovery_state(self.discovery.running, self._discovery_message)
        self.settings_page.set_discovery_state(self.discovery.running)
        self._on_peers(self.discovery.peers())
        self._sync_discovery_hosting("idle")
        self._persist_ui()

    def _persist_ui(self) -> None:
        user_settings.save(self._host_settings, self._viewer_settings, self.ui_settings)

    def _change_startup(self, enabled: bool) -> None:
        if not startup.supports_startup():
            self.settings_page.set_startup_state(False)
            self.notice_bar.show_message("当前平台不支持开机自启动。", "warn")
            return
        ok = startup.set_enabled(enabled)
        if not ok:
            self.settings_page.set_startup_state(not enabled)
            self.notice_bar.show_message("开机自启动设置失败：请检查当前用户权限。", "error")
            return
        self.ui_settings.start_on_login = enabled
        self._persist_ui()
        self.log("开机自启动已" + ("开启" if enabled else "关闭"), "info")

    def _change_tray(self, enabled: bool) -> None:
        if enabled and not QSystemTrayIcon.isSystemTrayAvailable():
            self.settings_page.set_tray_available(False)
            self.notice_bar.show_message("当前系统没有可用的系统托盘。", "warn")
            return
        self.ui_settings.close_to_tray = enabled
        self._setup_tray(enabled)
        self._persist_ui()
        self.log("关闭到托盘已" + ("开启" if enabled else "关闭"), "info")

    def _setup_tray(self, enabled: bool) -> None:
        if not enabled:
            self._destroy_tray()
            return
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        if self._tray is not None:
            self._tray.show()
            return
        app_icon = QApplication.windowIcon()
        tray = QSystemTrayIcon(
            app_icon if not app_icon.isNull() else icons.icon("monitor", ACCENT), self
        )
        tray.setToolTip("elink · 远程控制")
        menu = QMenu(self)
        show_action = menu.addAction("显示主窗口")
        show_action.triggered.connect(self._restore_window)
        quit_action = menu.addAction("退出 elink")
        quit_action.triggered.connect(self.request_exit)
        tray.setContextMenu(menu)
        tray.activated.connect(self._tray_activated)
        tray.show()
        self._tray = tray

    def _destroy_tray(self) -> None:
        tray = self._tray
        self._tray = None
        if tray is not None:
            tray.hide()
            tray.deleteLater()

    def _tray_activated(self, reason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.DoubleClick,
            QSystemTrayIcon.ActivationReason.Trigger,
        ):
            self._restore_window()

    def _restore_window(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def _open_dir(self, path: Path) -> None:
        try:
            path.mkdir(parents=True, exist_ok=True)
            opened = QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))
        except OSError as exc:
            opened = False
            self.notice_bar.show_message(f"无法打开目录：{exc}", "error")
        if not opened:
            self.notice_bar.show_message(f"无法打开目录：{path}", "warn")

    def _copy_diagnostics(self) -> None:
        app = QApplication.instance()
        if app is None:
            return
        lines = [
            f"elink {__version__}",
            f"python {sys.version.split()[0]}",
            f"platform {sys.platform}",
            f"frozen {bool(getattr(sys, 'frozen', False))}",
            f"data {self.root}",
            f"mode {self.service.mode or 'idle'}",
            f"status {self.status_label.text()}",
            f"control {self.control_label.text()}",
            f"discovery {'on' if self.discovery.running else 'off'}",
            "",
            "recent logs:",
        ]
        lines.extend(f"[{stamp}] {level} {message}" for stamp, level, message in self._recent_logs)
        app.clipboard().setText("\n".join(lines))
        self.log("诊断信息已复制到剪贴板", "info")

    # ---- 交互 ----
    def _on_nav(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        for key, button in self._nav_buttons.items():
            checked = PAGE_INDEX[key] == index or (key == "devices" and index != SETTINGS_INDEX)
            button.setIcon(icons.icon(self._nav_icons[key], ACCENT if checked else MUTED))

    def _show_page(self, key: str) -> None:
        index = PAGE_INDEX.get(key)
        if index is None:
            return
        self.stack.setCurrentIndex(index)
        nav_key = key if key in self._nav_buttons else "devices"
        self._nav_buttons[nav_key].setChecked(True)
        self._on_nav(index)

    def _apply_overview_preset(self, key: str) -> None:
        if key == "file":
            self._show_device_list()
            return
        self.host_page.apply_profile(key)
        self._show_page("host")

    def _toggle_maximize(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    # ---- 工具 ----
    def log(self, message: str, level: str = "info", *, fault_id: str = "",
            source: str = "local") -> None:
        self._recent_logs.append((time.strftime("%H:%M:%S"), level, message))
        remember(f"{level}: {message}")
        if level == "error":
            save_error(message, fault_id=fault_id, source=source, context=self.service.error_context)
        if level in {"warn", "error"}:
            self.notice_bar.show_message(message, level)
            if self._stream_window is not None:
                self._stream_window.notice.show_message(message, level)
                self._stream_window._layout()

    def _snapshot(self, frame: PreviewFrame) -> None:
        target = user_settings.captures_dir() / f"elink-{time.strftime('%Y%m%d-%H%M%S')}.png"
        if frame.preview.save_snapshot(str(target)):
            self.log(f"截图已保存: {target}", "info")
        else:
            self.log("当前没有画面可截图", "warn")

    def _snapshot_current(self) -> None:
        if self._active_mode == "host":
            self._snapshot(self.host_page.preview_frame)
            return
        if self.service.mode == "viewer":
            if self._stream_window is not None:
                self._snapshot(self._stream_window)
            return
        current = self.stack.currentWidget()
        if isinstance(current, HostPage):
            self._snapshot(self.host_page.preview_frame)
        else:
            self.log("当前没有画面可截图", "warn")

    def _apply_settings(self, host: HostSettings, viewer: ViewerSettings) -> None:
        """把持久化设置回填到控件（只回填存在的项）。"""
        self._loading_settings = True
        page = self.host_page
        if page.display.count() > 1:
            page.display.setCurrentIndex(max(0, min(host.display_index, page.display.count() - 1)))
        target = f"{host.width}×{host.height}"
        if target in RESOLUTIONS:
            page.resolution.setCurrentText(target)
        page.fps.setValue(round(host.fps))
        page.bitrate.setValue(host.bitrate_kbps / 1000)
        if host.encoder in ENCODERS:
            page.encoder.setCurrentIndex(ENCODERS.index(host.encoder))
        if host.preset in PRESETS:
            page.preset.setCurrentIndex(PRESETS.index(host.preset))
        page.transport.setCurrentIndex(0 if host.transport == "srt" else 1)
        page.srt_url.setText(host.srt_url)
        page.file_path.setText(host.file_path)
        page.control_port.setValue(host.control_port)
        page.allow_inject.setChecked(host.allow_inject)
        page.clipboard_switch.setChecked(host.clipboard_enabled)
        page.sync_transport()

        self._loading_settings = False

        self.overview_page.update_environment(
            platform_text=_platform_text(),
            display_count=self._display_count,
            control_port=host.control_port,
        )

    # ---- 窗口生命周期 ----
    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().showEvent(event)
        if not self._native_frame_patched:
            self._native_frame_patched = True
            self._patch_native_frame()

    def _patch_native_frame(self) -> None:
        """Windows 上给无边框窗口补回 WS_THICKFRAME：保留系统缩放、阴影与贴边。"""
        if not sys.platform.startswith("win"):
            return
        from PySide6.QtGui import QGuiApplication

        if QGuiApplication.platformName() == "offscreen":
            return
        try:  # pragma: no cover - 只在真实 Windows 窗口上执行
            import ctypes

            hwnd = int(self.winId())
            user32 = ctypes.windll.user32
            gwl_style = -16
            ws_thickframe = 0x00040000
            style = user32.GetWindowLongW(hwnd, gwl_style)
            user32.SetWindowLongW(hwnd, gwl_style, style | ws_thickframe)
            flags = 0x0020 | 0x0002 | 0x0001 | 0x0004  # FRAMECHANGED|NOMOVE|NOSIZE|NOZORDER
            user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, flags)
        except Exception:  # pragma: no cover
            pass

    def changeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self.title_bar.set_maximized(self.isMaximized())

    def request_exit(self) -> None:
        self._exit_requested = True
        self._close_stream_window()
        if self._tray is not None:
            self._tray.hide()
        self.close()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt 命名
        if (
            not self._exit_requested
            and self.ui_settings.close_to_tray
            and self._tray is not None
            and self._tray.isVisible()
        ):
            self._persist_ui()
            self.hide()
            event.ignore()
            if not self._tray_notified:
                self._tray_notified = True
                self._tray.showMessage(
                    "elink 仍在托盘运行",
                    "会话与控制通道保持连接；从托盘菜单可退出。",
                    QSystemTrayIcon.MessageIcon.Information,
                    2500,
                )
            return
        self.connection.close()
        deadline = time.monotonic() + 4.0
        while self.connection.busy and time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.01)
        self._close_stream_window()
        self.discovery.stop()
        if self._tray is not None:
            self._tray.hide()
        user_settings.save(self._host_settings, self._viewer_settings, self.ui_settings)
        event.accept()
        self.shutdown_complete.emit()


def _pid() -> int:
    import os

    return os.getpid()


def _platform_text() -> str:
    import platform

    system = platform.system()
    release = platform.release()
    if system == "Windows":
        return f"Windows {release}".strip()
    return f"{system} {release}".strip()


def _srt_port(url: str, default: int = 9000) -> int:
    try:
        port = int(str(url).rsplit(":", 1)[-1].split("/")[0])
    except (ValueError, IndexError):
        return default
    return port if 0 < port <= 65535 else default
