from __future__ import annotations

import asyncio
import json
import logging
import socket
import sys
import time
from pathlib import Path

from PySide6.QtCore import Qt, QSize, QTimer, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMainWindow, QPlainTextEdit, QPushButton, QSpinBox, QTabWidget, QScrollArea, QFrame,
                               QVBoxLayout, QWidget, QSystemTrayIcon, QMenu, QMessageBox, QButtonGroup)

from ..core.runtime import Runtime
from ..core.transport import Client, HostServer
from ..models import Endpoint
from ..network import netcheck, tailnet_ping, tailscale_path
from ..discovery import NetworkScope, discover, machine_id
from ..storage import atomic_json
from .. import startup
from .player import Player
from .about import AboutPage
from .controls import StreamOptions, DeviceCard, OptionRow, section_heading
from .chrome import window_icon
from .icons import icon, icon_label
from .. import __version__


def button(text, action, primary=False, icon_name=None):
    widget = QPushButton(text)
    if primary:
        widget.setObjectName("primary")
    if icon_name:
        widget.setIcon(icon(icon_name, '#ffffff' if primary else '#172b46'))
        widget.setIconSize(QSize(18, 18))
    widget.clicked.connect(action)
    return widget


def text_label(text, style=""):
    widget = QLabel(text)
    widget.setWordWrap(True)
    if style:
        widget.setObjectName(style)
    return widget


class MainWindow(QMainWindow):
    shutdown_complete = Signal()

    def __init__(self, root: Path, *, auto_refresh=True, auto_host=True):
        super().__init__()
        self.root = root
        self.runtime = Runtime(self)
        self.runtime.message.connect(self.handle_message)
        self.server = None
        self.auto_host = auto_host
        self.host_future = None
        self.host_starting = False
        self.host_retry_at = 0.0
        self.client = Client(root, self.runtime.message.emit, self.runtime.feedback.emit)
        self.player = None
        self.selected_host = None
        self.session_address = None
        self.resume_view = None
        self.device_buttons = []
        self.scope = NetworkScope()
        self.local_id = machine_id()
        self.discovery_future = None
        self.discovery_pending = False
        self.busy = False
        self._closing = self._closed = False
        self._exit_requested = False
        self.tray = None
        self.poll_pending = False
        self.config_path = root / "desktop-v3.json"
        self.config_error = False
        self.setWindowTitle(f"Elink · {__version__}")
        self.resize(1040, 760)
        self.setMinimumSize(900, 680)
        outer = QWidget()
        self.setCentralWidget(outer)
        self.sidebar = None
        self.navigation_buttons = []
        if sys.platform == 'win32':
            shell = QHBoxLayout(outer)
            shell.setContentsMargins(0, 0, 0, 0)
            shell.setSpacing(0)
            self.sidebar = QFrame()
            self.sidebar.setObjectName('navigationSidebar')
            self.sidebar.setFixedWidth(204)
            navigation = QVBoxLayout(self.sidebar)
            navigation.setContentsMargins(16, 28, 16, 20)
            navigation.setSpacing(8)
            shell.addWidget(self.sidebar)
            body = QWidget()
            shell.addWidget(body, 1)
            layout = QVBoxLayout(body)
        else:
            layout = QVBoxLayout(outer)
        layout.setContentsMargins(28, 22, 28, 18)
        layout.setSpacing(16)
        title = QHBoxLayout()
        title.setSpacing(14)
        brand = icon_label('link', 44, '#ffffff', glyph_size=28)
        brand.setObjectName('brandMark')
        title.addWidget(brand)
        brand_text = QVBoxLayout()
        brand_text.setSpacing(2)
        brand_text.addWidget(text_label('Elink', 'brandTitle'))
        if self.sidebar is None:
            brand_text.addWidget(text_label('你的设备，随时连接。', 'muted'))
        title.addLayout(brand_text)
        title.addStretch()
        platform_label = 'WINDOWS' if sys.platform == 'win32' else 'LINUX'
        version = text_label(f'{platform_label}  /  {__version__}\nPREVIEW', 'versionLabel')
        if self.sidebar is not None:
            navigation.addLayout(title)
            navigation.addSpacing(32)
        else:
            title.addWidget(version, 0, Qt.AlignmentFlag.AlignTop)
            layout.addLayout(title)
        self.setWindowIcon(icon('link', '#2855eb'))
        self.notice_bar = QFrame()
        self.notice_bar.setObjectName('errorNotice')
        notice_row = QHBoxLayout(self.notice_bar)
        notice_row.setContentsMargins(12, 8, 8, 8)
        self.notice = text_label('', 'noticeText')
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        self.notice.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        notice_row.addWidget(self.notice, 1)
        self.notice_close = QPushButton()
        self.notice_close.setObjectName('noticeClose')
        self.notice_close.setIcon(window_icon('close'))
        self.notice_close.setToolTip('关闭提示')
        self.notice_close.setAccessibleName('关闭提示')
        self.notice_close.setFixedSize(32, 32)
        self.notice_close.clicked.connect(self.notice_bar.hide)
        notice_row.addWidget(self.notice_close, 0, Qt.AlignmentFlag.AlignTop)
        self.notice_bar.hide()
        layout.addWidget(self.notice_bar)
        self.tabs = QTabWidget()
        if self.sidebar is not None:
            self.tabs.setObjectName('sidebarPages')
            self.tabs.tabBar().hide()
        layout.addWidget(self.tabs, 1)
        self.make_remote()
        self.make_host()
        self.make_network()
        self.about = AboutPage(self)
        self.tabs.addTab(self.about, "关于")
        if self.sidebar is not None:
            self.navigation_group = QButtonGroup(self)
            for index, glyph in enumerate(('monitor', 'settings', 'link')):
                name = self.tabs.tabText(index)
                entry = QPushButton(name)
                entry.setObjectName('navigationItem')
                entry.setAccessibleName(name)
                entry.setCheckable(True)
                entry.setFixedHeight(48)
                entry.setIconSize(QSize(20, 20))
                entry.setProperty('navigationIcon', glyph)
                entry.setCursor(Qt.CursorShape.PointingHandCursor)
                self.navigation_group.addButton(entry, index)
                self.navigation_buttons.append(entry)
                navigation.addWidget(entry)
            navigation.addStretch()
            navigation.addWidget(version)
            self.navigation_group.idClicked.connect(self.tabs.setCurrentIndex)
            self.tabs.currentChanged.connect(self.update_navigation)
            self.update_navigation(self.tabs.currentIndex())
        self.statusBar().showMessage("正在准备本机连接…" if auto_host else "诊断模式")
        self.statusBar().hide()
        self.load_config()
        self.options.refresh_upscalers(self.runtime)
        self.make_tray()
        self.startup_enabled.toggled.connect(self.change_startup)
        self.close_to_tray.toggled.connect(lambda _: self.save_config())
        if self.admin_startup:
            self.admin_startup.toggled.connect(self.change_admin_startup)
        if self.startup_enabled.isChecked():
            self.change_startup(True)  # Refresh the path after moving a portable installation.
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_host)
        self.timer.start(1000)
        self.discovery_timer = QTimer(self)
        self.discovery_timer.timeout.connect(self.refresh_network)
        if auto_host:
            QTimer.singleShot(0, self.start_host)
        if auto_refresh:
            self.discovery_timer.start(10000)
            QTimer.singleShot(100, self.refresh_network)

    def update_navigation(self, index):
        for number, entry in enumerate(self.navigation_buttons):
            entry.setChecked(number == index)
            entry.setIcon(icon(entry.property('navigationIcon'), '#2855eb' if number == index else '#536781'))

    def page(self, name, scroll=False):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 0 if self.sidebar is not None else 20, 4, 10)
        if scroll:
            area = QScrollArea()
            area.setWidgetResizable(True)
            area.setWidget(page)
            self.tabs.addTab(area, name)
        else:
            self.tabs.addTab(page, name)
        return layout

    def make_remote(self):
        layout = self.page("设备")
        heading = QHBoxLayout()
        heading.setSpacing(10)
        introduction = QVBoxLayout()
        introduction.setSpacing(5)
        introduction.addWidget(text_label('全部设备', 'pageTitle'))
        self.discovery_state = text_label('正在寻找同局域网 / Tailscale 中的设备…', 'muted')
        introduction.addWidget(self.discovery_state)
        if self.sidebar is not None:
            layout.addLayout(introduction)
            layout.addSpacing(12)
            heading.addStretch()
        else:
            heading.addLayout(introduction, 1)
        self.refresh_button = button('刷新', self.refresh_network, icon_name='refresh-cw')
        heading.addWidget(self.refresh_button)
        self.manual_button = button('通过地址连接', self.manual_connect, icon_name='link')
        heading.addWidget(self.manual_button)
        self.network_button = button('网络诊断', self.show_network_diagnostics, icon_name='activity')
        heading.addWidget(self.network_button)
        layout.addLayout(heading)
        layout.addSpacing(12)
        self.device_count = text_label('', 'muted')
        layout.addWidget(section_heading('设备列表', 'monitor', self.device_count))
        local = QFrame()
        local.setObjectName("localDevice")
        local_layout = QHBoxLayout(local)
        local_layout.setContentsMargins(20, 16, 20, 16)
        local_layout.setSpacing(18)
        local_layout.addWidget(icon_label('monitor', 34, '#6c7b90'))
        labels = QVBoxLayout()
        labels.setSpacing(4)
        labels.addWidget(text_label(f'{socket.gethostname()}  ·  本机', 'deviceName'))
        self.host_status = text_label('正在准备…', 'hostStatus')
        labels.addWidget(self.host_status)
        self.session_status = text_label("等待连接", "muted")
        labels.addWidget(self.session_status)
        local_layout.addLayout(labels, 1)
        self.end_session_button = button('断开当前控制端', self.end_host_session, icon_name='power')
        self.end_session_button.setObjectName("danger")
        self.end_session_button.hide()
        local_layout.addWidget(self.end_session_button)
        layout.addWidget(local)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        self.devices_layout = QVBoxLayout(content)
        self.devices_layout.setContentsMargins(0, 8, 0, 0)
        self.devices_layout.setSpacing(10)
        self.devices_layout.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        self.display_hosts([])
        self.address = QLineEdit(self)
        self.address.hide()  # Persist the last/manual endpoint without a permanent form.

    def manual_connect(self):
        from PySide6.QtWidgets import QInputDialog
        address, accepted = QInputDialog.getText(self, "通过地址连接", "IP 或主机名（可附端口）", text=self.address.text())
        if accepted and address.strip():
            self.selected_host = None
            self.address.setText(address.strip())
            self.connect_remote()

    def make_host(self):
        layout = self.page("设置", scroll=True)
        layout.setSpacing(0)
        if self.sidebar is not None:
            layout.addWidget(text_label('设置', 'pageTitle'))
            layout.addSpacing(16)
        layout.addWidget(section_heading('常规', 'settings'))
        self.startup_enabled = QCheckBox("开机时自启动")
        self.close_to_tray = QCheckBox("关闭时最小化到托盘")
        self.admin_startup = QCheckBox('以管理员身份启动') if sys.platform == 'win32' else None
        layout.addWidget(OptionRow('开机时自启动', self.startup_enabled))
        layout.addWidget(OptionRow('关闭时最小化到托盘', self.close_to_tray))
        if self.admin_startup:
            layout.addWidget(OptionRow('以管理员身份启动', self.admin_startup))
            layout.addWidget(text_label('每次启动都会请求 Windows 管理员授权；若同时启用开机自启动，登录时也会显示授权提示。', 'muted'))
        layout.addSpacing(24)
        layout.addWidget(section_heading('本机连接设置', 'monitor'))
        self.options = StreamOptions(self)
        self.options.hide()
        for name in StreamOptions.fields:
            setattr(self, name, getattr(self.options, name))
        self.advanced = QWidget()
        advanced_layout = QVBoxLayout(self.advanced)
        advanced_layout.setContentsMargins(0, 0, 0, 0)
        advanced_layout.setSpacing(0)
        self.port = QSpinBox()
        self.port.setRange(1024, 65535)
        self.port.setValue(49200)
        self.port.setMinimumWidth(360)
        self.port.setMaximumWidth(480)
        advanced_layout.addWidget(OptionRow('本机监听端口', self.port))
        self.pad_backend = QComboBox()
        self.pad_backend.addItem("ViGEmBus（需已安装）", "vigem")
        self.pad_backend.addItem("ElinkPad（实验驱动，单手柄）", "elinkpad")
        self.pad_backend.addItem("禁用虚拟手柄", "disabled")
        if sys.platform.startswith('linux'):
            self.pad_backend.clear()
            self.pad_backend.addItem('Linux uinput', 'uinput')
            self.pad_backend.addItem('禁用虚拟手柄', 'disabled')
        self.pad_backend.setMinimumWidth(360)
        self.pad_backend.setMaximumWidth(480)
        advanced_layout.addWidget(OptionRow('本机手柄后端', self.pad_backend))
        advanced_layout.addSpacing(16)
        self.apply_host_button = button('应用连接设置', self.apply_host_settings, primary=True, icon_name='save')
        advanced_layout.addWidget(self.apply_host_button, alignment=Qt.AlignmentFlag.AlignLeft)
        advanced_layout.addSpacing(12)
        advanced_layout.addWidget(text_label("ElinkPad 尚未签名或完成游戏验证。更改本机连接设置前需结束被控会话。", "muted"))
        layout.addWidget(self.advanced)
        layout.addStretch()

    def change_startup(self, enabled):
        try:
            startup.set_enabled(enabled)
        except OSError as exc:
            self.startup_enabled.blockSignals(True)
            self.startup_enabled.setChecked(not enabled)
            self.startup_enabled.blockSignals(False)
            self.report_error(f"无法修改开机自启：{exc}")
            return
        self.save_config()

    def change_admin_startup(self, enabled):
        if not self.admin_startup:
            return
        self.save_config()
        from ..platforms.windows.privileges import is_elevated, restart_elevated
        if enabled and not is_elevated():
            try:
                restart_elevated(self.root, startup='--startup' in sys.argv)
            except OSError as exc:
                self.admin_startup.blockSignals(True)
                self.admin_startup.setChecked(False)
                self.admin_startup.blockSignals(False)
                self.save_config()
                self.report_error(str(exc))
                return
            self.request_exit()
        elif not enabled and is_elevated():
            self.show_notice('设置已关闭。当前进程仍以管理员权限运行；完全退出后重新打开，才会切换为普通权限。', error=False)

    def make_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        tray_icon = icon('link', '#2855eb')
        self.tray = QSystemTrayIcon(tray_icon, self)
        self.tray.setToolTip('Elink')
        menu = QMenu(self)
        menu.addAction('打开 Elink').triggered.connect(self.restore_window)
        menu.addSeparator()
        menu.addAction('退出 Elink').triggered.connect(self.request_exit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.tray_activated)
        self.tray.show()

    def tray_activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.restore_window()

    def restore_window(self):
        if self._closing:
            return
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()

    def show_startup(self):
        if not self.tray or not self.tray.isVisible():
            self.show()

    def request_exit(self):
        self._exit_requested = True
        self.close()

    def make_network(self):
        self.network_dialog = QDialog(self)
        self.network_dialog.setWindowTitle("网络诊断")
        self.network_dialog.setMinimumSize(720, 560)
        layout = QVBoxLayout(self.network_dialog)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.addWidget(text_label("无需自建服务器。有可达地址时直接连接；没有直连路径时使用外部 Tailscale。当前未提供独立的公网打洞或中继服务。", "notice"))
        row = QHBoxLayout()
        row.addWidget(button("刷新设备与地址", self.refresh_network))
        row.addWidget(button("探测当前主机路径", self.probe_route))
        row.addWidget(button("检测 UDP / NAT", self.check_nat))
        row.addStretch()
        layout.addLayout(row)
        self.network_state = text_label("未检测", "muted")
        layout.addWidget(self.network_state)
        self.peers = QListWidget()
        self.peers.itemDoubleClicked.connect(self.select_peer)
        layout.addWidget(self.peers)
        layout.addWidget(text_label("设备页会合并同一台主机的局域网 / Tailscale 地址。Tailscale 在线不等于 Elink 正在运行；路径探测才能确认网络路径。HTTPS 使用 TCP 49200，自动发现使用 UDP 49201，媒体使用 ICE 动态 UDP 端口；防火墙需允许 Elink 应用入站。", "muted"))

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(200)
        self.log.setMaximumHeight(150)
        self.log.hide()
        layout.addWidget(button("查看诊断记录 / 收起", lambda: self.log.setVisible(not self.log.isVisible())),
                         alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.log)

    def show_network_diagnostics(self):
        if self._closing:
            return
        self.network_dialog.show()
        self.network_dialog.raise_()
        self.network_dialog.activateWindow()

    def show_notice(self, message, error=True):
        self.notice_bar.setObjectName("errorNotice" if error else "notice")
        self.notice.setText(str(message))
        for widget in (self.notice_bar, self.notice):
            widget.style().unpolish(widget)
            widget.style().polish(widget)
        self.notice_bar.show()

    def handle_message(self, message):
        if message.startswith(("控制端会话：", "被控会话：")):
            self.append_log(message)
            if message.endswith(":failed") or message.endswith("：failed"):
                self.report_error("连接已中断，请检查网络或对端状态，然后重新连接。")
        else:
            self.report_error(message)

    def append_log(self, message):
        logging.getLogger(__name__).info('%s', message)
        self.log.appendPlainText(f"{time.strftime('%H:%M:%S')}  {message}")

    def report_error(self, message):
        self.append_log(message)
        self.show_notice(message)
        if self.player:
            self.player.show_error(message)

    def set_busy(self, busy):
        self.busy = busy
        self.manual_button.setEnabled(not busy)
        for widget in self.device_buttons:
            widget.setEnabled(not busy and widget.connectable)

    def connect_remote(self):
        if self.busy or self._closing:
            return
        if self.player:
            self.player.raise_()
            return
        try:
            address = Endpoint.parse(self.address.text()).authority
        except ValueError as exc:
            self.report_error(str(exc))
            return
        self.notice_bar.hide()
        self.session_address = address
        self.save_config()
        self.show_notice(f"正在连接 {address}…", error=False)
        self.set_busy(True)
        width, height = self.resolution.currentData()
        config = dict(width=width, height=height, follow_display=False, fps=self.fps.currentData(),
                      bitrate=self.bitrate.value(), audio=self.audio.isChecked(), render_scale=self.options.render_scale.currentData())
        host = self.selected_host
        addresses = [address]
        if host and address in host.routes:
            addresses += [route for route in host.routes if route != address]
        decoder = self.decoder.currentData()
        renderer = self.options.renderer.currentData()
        muted = bool(self.resume_view and self.resume_view['muted'])
        async def connect():
            import aiohttp
            self.client.set_muted(muted)
            self.client.set_renderer(renderer)
            for index, route in enumerate(addresses):
                try:
                    result = await self.client.connect(route, config, decoder)
                    return route, result
                except (OSError, asyncio.TimeoutError, aiohttp.ClientError):
                    if index == len(addresses) - 1:
                        raise
        self.runtime.submit(connect(), self.connected, self.connect_failed)

    def connected(self, result):
        if self._closing:
            return
        self.session_address = result[0]
        self.notice_bar.hide()
        self.player = Player(self.runtime, self.client, controllers=self.controllers.isChecked(), preferences=self.options.values())
        self.player.settings_changed.connect(self.update_session_preferences)
        self.player.reconfigure.connect(self.reconfigure_session)
        self.player.ended.connect(self.disconnect_remote)
        view, self.resume_view = self.resume_view, None
        if view:
            self.player.setGeometry(view['geometry'])
            self.player.show_statistics = view['statistics']
            self.player.muted = view['muted']
            self.player.was_maximized = view.get('was_maximized', False)
        if view and view['fullscreen']:
            self.player.showFullScreen()
        elif view and view.get('maximized'):
            self.player.showMaximized()
        else:
            self.player.show()
        self.set_busy(False)
        self.append_log("串流窗口已打开，正在等待画面。")

    def connect_failed(self, message):
        self.resume_view = None
        self.set_busy(False)
        self.report_error(f"连接失败：{message or '连接超时或对端未响应'}\n请确认对端 Elink 正在运行、设备可以互访，或刷新设备后重试。")

    def disconnect_remote(self):
        self.resume_view = None
        player, self.player = self.player, None
        if player:
            player.ended.disconnect(self.disconnect_remote)
            player.close()
            player.deleteLater()
        self.set_busy(True)
        self.runtime.submit(self.client.disconnect(), lambda _: self.set_busy(False), self.connect_failed)

    def update_session_preferences(self, values):
        if values.get('upscaler') == 'rtx':
            self.options.set_rtx_available(True)
        self.options.restore(values)
        self.save_config()

    def reconfigure_session(self, values):
        if self.busy or self._closing or not self.player:
            return
        player = self.player
        width, height = values['resolution']
        config = dict(width=width, height=height, fps=self.fps.itemData(values['fps']),
                      bitrate=values['bitrate'], audio=values['audio'], render_scale=values['render_scale'])
        def applied(_):
            if not self._closing and self.player is player:
                player.apply_preferences(values)
                self.update_session_preferences(values)
        def failed(message):
            if not self._closing and self.player is player:
                player.finish_quality(f'应用失败：{message}')
        self.runtime.submit(self.client.reconfigure(config, self.decoder.itemData(values['decoder']), values['renderer']),
                            applied, failed)

    def start_host(self):
        if self._closing or self.host_starting or self.server and self.server.runner:
            return
        self.host_starting = True
        self.apply_host_button.setEnabled(False)
        port, backend = self.port.value(), self.pad_backend.currentData()
        async def start():
            if self.server is None:
                self.server = HostServer(self.root, notify=self.runtime.message.emit, pad_backend=backend, scope=self.scope)
            self.server.pad_backend = backend
            return await self.server.start(port=port)
        self.host_future = self.runtime.submit(start(), self.host_ready, self.host_failed)

    def host_ready(self, port):
        self.host_starting = False
        self.host_future = None
        if self._closing:
            return
        self.apply_host_button.setEnabled(True)
        self.host_status.setText(f"可连接 · TCP {port}")
        self.statusBar().showMessage("本机已就绪 · 等待控制端连接")
        self.append_log(f"本机自动就绪 · TCP {port} · 同时只允许一台控制端。")

    def host_failed(self, message):
        self.host_starting = False
        self.host_future = None
        self.host_retry_at = time.monotonic() + 10
        if self._closing:
            return
        self.apply_host_button.setEnabled(True)
        self.host_status.setText("设置未应用 · 当前连接服务继续运行" if self.server and self.server.runner
                                 else "暂不可连接 · 10 秒后重试")
        self.report_error(message)

    def apply_host_settings(self):
        if self._closing or self.host_starting:
            return
        self.save_config()
        self.host_starting = True
        self.apply_host_button.setEnabled(False)
        port, backend = self.port.value(), self.pad_backend.currentData()
        async def apply():
            if self.server:
                if self.server.pc or self.server.ending or self.server.lock.locked():
                    raise ValueError("当前正在被控制或连接中，请断开会话后再修改连接设置。")
                await self.server.stop()
            else:
                self.server = HostServer(self.root, notify=self.runtime.message.emit, pad_backend=backend, scope=self.scope)
            self.server.pad_backend = backend
            return await self.server.start(port=port)
        self.host_future = self.runtime.submit(apply(), self.host_ready, self.host_failed)

    def end_host_session(self):
        if self.server:
            def ended(_):
                self.append_log('已断开当前控制端。')
                self.refresh_host()
            self.runtime.submit(self.server.end_session(), ended, self.report_error)

    def refresh_host(self):
        if self.auto_host and not self._closing and not self.host_starting and time.monotonic() >= self.host_retry_at:
            if not self.server or not self.server.runner:
                self.start_host()
        if not self.server or self.poll_pending or self._closing:
            return
        self.poll_pending = True
        async def snapshot():
            return (self.server.device_id, self.server.pc is not None, self.server.ending,
                    self.server.port if self.server.runner else None)
        def display(result):
            self.poll_pending = False
            if self._closing:
                return
            address, connected, ending, port = result
            active = connected or ending
            if port:
                state = '正在断开' if ending else '占用中' if connected else '可连接'
                self.host_status.setText(f'{state} · TCP {port}')
            self.apply_host_button.setEnabled(not active and not self.host_starting)
            self.session_status.setText('正在释放会话资源…' if ending else f'当前控制端：{address}' if connected else '当前无串流连接')
            self.end_session_button.setEnabled(active)
            self.end_session_button.setVisible(active)
        self.runtime.submit(snapshot(), display, lambda _: setattr(self, "poll_pending", False))

    def select_host(self, host):
        if self.busy or not host.connectable:
            return
        self.selected_host = host
        self.address.setText(host.address)
        self.connect_remote()

    def display_hosts(self, hosts):
        while self.devices_layout.count() > 1:
            item = self.devices_layout.takeAt(0)
            item.widget().hide()
            item.widget().deleteLater()
        self.device_buttons = []
        for host in hosts:
            if host.id == self.local_id:
                continue
            routes = " / ".join(dict.fromkeys(host.routes.values()))
            platform = {'android': 'Android', 'windows': 'Windows', 'linux': 'Linux'}.get(host.platform, '')
            state = '仅控制端 · 不支持被控' if not host.connectable else '控制中' if host.busy else '在线'
            card = DeviceCard(host.name, f"{platform} · {state}  ·  {routes}  ·  {host.address}",
                              connectable=host.connectable)
            card.clicked.connect(lambda checked=False, host=host: self.select_host(host))
            card.setEnabled(not self.busy and host.connectable)
            self.device_buttons.append(card)
            self.devices_layout.insertWidget(self.devices_layout.count() - 1, card)
        if not self.device_buttons:
            empty = text_label("还没有发现其他设备\n在另一台电脑打开 Elink，连接同一局域网或 Tailscale 后即可在这里看到。", "emptyState")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setMinimumHeight(160)
            self.devices_layout.insertWidget(0, empty)
        self.discovery_state.setText(f"{len(self.device_buttons)} 台在线设备 · 支持被控的设备可点击连接 · 自动刷新")
        self.device_count.setText(f'{len(self.device_buttons) + 1} 台设备')

    def refresh_network(self):
        if self.discovery_pending or self._closing:
            return
        self.discovery_pending = True
        self.refresh_button.setEnabled(False)
        address = self.address.text()
        async def probe():
            hosts = await discover(self.scope, self.local_id, address)
            return hosts, self.scope.tailnet, [str(i.ip) for i in self.scope.interfaces]
        def done():
            self.discovery_pending = False
            self.discovery_future = None
            self.refresh_button.setEnabled(True)
        def display(result):
            done()
            if self._closing:
                return
            hosts, status, addresses = result
            self.display_hosts(hosts)
            self.network_state.setText("本机局域网地址：" + (" / ".join(addresses) or "无") + "\nTailscale：" + status.state + "  " + " / ".join(status.addresses) + "\n" + "\n".join(status.health))
            self.peers.clear()
            for peer in status.peers:
                item = QListWidgetItem(f"{peer.name}  ·  {peer.address}  ·  {'在线' if peer.online else '离线'}")
                item.setData(Qt.ItemDataRole.UserRole, peer.address)
                self.peers.addItem(item)
        def failed(message):
            done()
            if not self._closing:
                self.discovery_state.setText(f"发现暂不可用：{message}，可通过地址连接。")
        self.discovery_future = self.runtime.submit(probe(), display, failed)

    def select_peer(self, item):
        self.selected_host = None
        self.address.setText(item.data(Qt.ItemDataRole.UserRole))
        self.tabs.setCurrentIndex(0)
        self.connect_remote()

    def probe_route(self):
        executable = tailscale_path()
        if not executable:
            self.report_error("未找到外部 Tailscale。")
            return
        self.runtime.submit(asyncio.to_thread(tailnet_ping, executable, self.address.text()), lambda p: self.show_notice(f"路径 {p.kind} · RTT {p.latency_ms} ms · {p.detail}", error=False))

    def check_nat(self):
        executable = tailscale_path()
        if executable:
            self.runtime.submit(asyncio.to_thread(netcheck, executable), lambda report: self.show_notice(json.dumps(report, ensure_ascii=False), error=False))
        else:
            self.report_error("未找到外部 Tailscale。")

    def load_config(self):
        if not self.config_path.exists():
            return
        try:
            value = json.loads(self.config_path.read_text(encoding="utf-8"))
            self.address.setText(value.get("address", ""))
            self.about.previews.setChecked(value.get("update_previews", True))
            self.port.setValue(value.get("port", 49200))
            self.bitrate.setValue(value.get("bitrate", 20))
            self.pad_backend.setCurrentIndex(max(0, self.pad_backend.findData(value.get("pad_backend", "vigem"))))
            for name in ("fps", "decoder"):
                widget = getattr(self, name)
                widget.setCurrentIndex(max(0, min(widget.count() - 1, value.get(name, 0))))
            self.options.restore({'mouse_mode': value.get('mouse_mode', 'smart'),
                                  'mouse_sensitivity': value.get('mouse_sensitivity', 0),
                                  'resolution': value.get('stream_resolution', [1920, 1080]),
                                  'renderer': value.get('renderer', 'cpu'), 'render_scale': value.get('render_scale', 100),
                                  'upscaler': value.get('upscaler', 'fsr')})
            for name in ("audio", "controllers"):
                getattr(self, name).setChecked(value.get(name, True))
            self.startup_enabled.setChecked(value.get('startup_enabled') is True)
            self.close_to_tray.setChecked(value.get('close_to_tray') is True)
            if self.admin_startup:
                self.admin_startup.setChecked(value.get('admin_startup') is True)
        except (ValueError, TypeError, OSError, AttributeError) as exc:
            self.config_error = True
            self.report_error(f"设置文件读取失败，保留原文件：{exc}")

    def save_config(self):
        if self.config_error:
            return
        value = dict(address=self.address.text(), port=self.port.value(), bitrate=self.bitrate.value())
        value["pad_backend"] = self.pad_backend.currentData()
        value["update_previews"] = self.about.previews.isChecked()
        for name in ("fps", "decoder"):
            value[name] = getattr(self, name).currentIndex()
        value['mouse_mode'] = self.mouse_mode.currentData()
        value['mouse_sensitivity'] = self.mouse_sensitivity.value()
        value['startup_enabled'] = self.startup_enabled.isChecked()
        value['close_to_tray'] = self.close_to_tray.isChecked()
        value['admin_startup'] = bool(self.admin_startup and self.admin_startup.isChecked())
        value['stream_resolution'] = self.resolution.currentData()
        value['renderer'] = self.renderer.currentData()
        value['render_scale'] = self.options.render_scale.currentData()
        value['upscaler'] = self.options.upscaler.currentData()
        for name in ("audio", "controllers"):
            value[name] = getattr(self, name).isChecked()
        try:
            atomic_json(self.config_path, value)
        except OSError as exc:
            self.report_error(f"无法保存设置：{exc}")

    def closeEvent(self, event):
        if self._closed:
            event.accept()
            return
        event.ignore()
        if self.about.installing and not self.about.handoff_complete:
            return
        if self._closing:
            return
        if (self.close_to_tray.isChecked() and not self._exit_requested and not self.about.handoff_complete
                and self.tray and self.tray.isVisible() and QSystemTrayIcon.isSystemTrayAvailable()):
            self.save_config()
            self.hide()
            return
        self._closing = True
        if self.tray:
            self.tray.hide()
        self.about.shutdown()
        self.timer.stop()
        self.save_config()
        self.discovery_timer.stop()
        if self.discovery_future:
            self.discovery_future.cancel()
        if self.player:
            self.player.ended.disconnect(self.disconnect_remote)
            self.player.close()
        self.setEnabled(False)
        async def cleanup():
            pending = self.host_future
            if pending:
                try:
                    await asyncio.wrap_future(pending)
                except Exception:
                    pass
            await self.client.disconnect()
            if self.server:
                await self.server.stop()
        def finished(_):
            self.runtime.stop()
            self._closed = True
            self.close()
            self.shutdown_complete.emit()
        self.runtime.submit(cleanup(), finished, finished)
