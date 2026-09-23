from __future__ import annotations

import asyncio
import json
import socket
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QComboBox, QDialog, QFormLayout, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMainWindow, QPlainTextEdit, QPushButton, QSpinBox, QTabWidget, QScrollArea, QFrame,
                               QVBoxLayout, QWidget)

from ..core.runtime import Runtime
from ..core.transport import Client, HostServer
from ..models import Endpoint
from ..network import netcheck, tailnet_ping, tailscale_path
from ..discovery import NetworkScope, discover, machine_id
from ..storage import atomic_json
from .player import Player
from .about import AboutPage
from .controls import StreamOptions, DeviceCard
from .. import __version__


def button(text, action, primary=False):
    widget = QPushButton(text)
    if primary:
        widget.setObjectName("primary")
    widget.clicked.connect(action)
    return widget


def text_label(text, style=""):
    widget = QLabel(text)
    widget.setWordWrap(True)
    if style:
        widget.setObjectName(style)
    return widget


class MainWindow(QMainWindow):
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
        self.poll_pending = False
        self.config_path = root / "desktop-v3.json"
        self.config_error = False
        self.setWindowTitle(f"Elink · {__version__}")
        self.resize(1040, 760)
        self.setMinimumSize(900, 680)
        outer = QWidget()
        self.setCentralWidget(outer)
        layout = QVBoxLayout(outer)
        layout.setContentsMargins(28, 22, 28, 18)
        title = QHBoxLayout()
        title.addWidget(text_label("Elink", "pageTitle"))
        title.addStretch()
        title.addWidget(text_label(f"WINDOWS  /  {__version__} PREVIEW", "muted"))
        layout.addLayout(title)
        layout.addWidget(text_label("你的设备，随时连接。", "muted"))
        self.notice = text_label("", "errorNotice")
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        self.notice.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.notice.hide()
        layout.addWidget(self.notice)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        self.make_remote()
        self.make_host()
        self.make_network()
        self.about = AboutPage(self)
        self.tabs.addTab(self.about, "关于")
        self.statusBar().showMessage("正在准备本机连接…" if auto_host else "诊断模式")
        self.statusBar().hide()
        self.load_config()
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

    def page(self, name, scroll=False):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 20, 4, 10)
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
        heading.addWidget(text_label("全部设备", "pageTitle"))
        heading.addStretch()
        self.refresh_button = button("刷新", self.refresh_network)
        heading.addWidget(self.refresh_button)
        self.manual_button = button("通过地址连接", self.manual_connect)
        heading.addWidget(self.manual_button)
        self.network_button = button("网络诊断", self.show_network_diagnostics)
        heading.addWidget(self.network_button)
        layout.addLayout(heading)
        self.discovery_state = text_label("正在寻找同局域网 / Tailscale 中的设备…", "muted")
        layout.addWidget(self.discovery_state)
        local = QFrame()
        local.setObjectName("localDevice")
        local_layout = QHBoxLayout(local)
        labels = QVBoxLayout()
        labels.addWidget(text_label(f"{socket.gethostname()}  ·  本机", "sectionTitle"))
        self.host_status = text_label("正在准备…", "muted")
        labels.addWidget(self.host_status)
        self.session_status = text_label("等待连接", "muted")
        labels.addWidget(self.session_status)
        local_layout.addLayout(labels, 1)
        self.end_session_button = button("断开当前控制端", self.end_host_session)
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
        layout.addWidget(text_label("本机连接设置", "pageTitle"))
        layout.addWidget(text_label("画质与显示设置请在远控窗口的控制中心调整。这里仅设置本机接入方式。", "muted"))
        self.options = StreamOptions(self)
        self.options.hide()
        for name in StreamOptions.fields:
            setattr(self, name, getattr(self.options, name))
        self.advanced = QWidget()
        advanced_layout = QVBoxLayout(self.advanced)
        form = QFormLayout()
        self.port = QSpinBox()
        self.port.setRange(1024, 65535)
        self.port.setValue(49200)
        form.addRow("本机监听端口", self.port)
        self.pad_backend = QComboBox()
        self.pad_backend.addItem("ViGEmBus（需已安装）", "vigem")
        self.pad_backend.addItem("ElinkPad（实验驱动，单手柄）", "elinkpad")
        self.pad_backend.addItem("禁用虚拟手柄", "disabled")
        form.addRow("本机手柄后端", self.pad_backend)
        advanced_layout.addLayout(form)
        self.apply_host_button = button("应用连接设置", self.apply_host_settings)
        advanced_layout.addWidget(self.apply_host_button, alignment=Qt.AlignmentFlag.AlignLeft)
        advanced_layout.addWidget(text_label("ElinkPad 尚未签名或完成游戏验证。更改本机连接设置前需结束被控会话。", "muted"))
        layout.addWidget(self.advanced)
        layout.addStretch()

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
        self.notice.setObjectName("errorNotice" if error else "notice")
        self.notice.setText(str(message))
        self.notice.style().unpolish(self.notice)
        self.notice.style().polish(self.notice)
        self.notice.show()

    def handle_message(self, message):
        if message.startswith(("控制端会话：", "被控会话：")):
            self.append_log(message)
            if message.endswith(":failed") or message.endswith("：failed"):
                self.report_error("连接已中断，请检查网络或对端状态，然后重新连接。")
        else:
            self.report_error(message)

    def append_log(self, message):
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
            widget.setEnabled(not busy)

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
        self.notice.hide()
        self.session_address = address
        self.save_config()
        self.show_notice(f"正在连接 {address}…", error=False)
        self.set_busy(True)
        config = dict(width=1920, height=1080, follow_display=True, fps=self.fps.currentData(), bitrate=self.bitrate.value(), audio=self.audio.isChecked())
        host = self.selected_host
        addresses = [address]
        if host and address in host.routes:
            addresses += [route for route in host.routes if route != address]
        decoder = self.decoder.currentData()
        muted = bool(self.resume_view and self.resume_view['muted'])
        async def connect():
            import aiohttp
            self.client.set_muted(muted)
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
        self.notice.hide()
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
        self.options.restore(values)
        self.save_config()

    def reconfigure_session(self, values):
        if self.busy or self._closing or not self.player:
            return
        self.update_session_preferences(values)
        address = self.session_address
        player, self.player = self.player, None
        self.resume_view = dict(geometry=player.normalGeometry(), fullscreen=player.isFullScreen(),
                                statistics=player.show_statistics, muted=player.muted,
                                maximized=player.isMaximized(), was_maximized=player.was_maximized)
        player.ended.disconnect(self.disconnect_remote)
        player.close()
        player.deleteLater()
        self.set_busy(True)
        self.show_notice("正在应用画质设置，重新连接中…", error=False)
        def reconnect(_):
            self.set_busy(False)
            if not self._closing:
                self.address.setText(address)
                self.connect_remote()
        self.runtime.submit(self.client.disconnect(restore_display=False), reconnect, self.connect_failed)

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
            self.runtime.submit(self.server.end_session(), lambda _: self.append_log("已断开当前控制端。"))

    def refresh_host(self):
        if self.auto_host and not self._closing and not self.host_starting and time.monotonic() >= self.host_retry_at:
            if not self.server or not self.server.runner:
                self.start_host()
        if not self.server or self.poll_pending or self._closing:
            return
        self.poll_pending = True
        async def snapshot():
            return self.server.device_id, self.server.pc is not None or self.server.ending, self.server.port if self.server.runner else None
        def display(result):
            self.poll_pending = False
            if self._closing:
                return
            address, active, port = result
            if port:
                self.host_status.setText(f"{'占用中' if active else '可连接'} · TCP {port}")
            self.apply_host_button.setEnabled(not active and not self.host_starting)
            self.session_status.setText(f"当前控制端：{address}" if active else "当前无串流连接")
            self.end_session_button.setEnabled(active)
            self.end_session_button.setVisible(active)
        self.runtime.submit(snapshot(), display, lambda _: setattr(self, "poll_pending", False))

    def select_host(self, host):
        if self.busy:
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
            card = DeviceCard(host.name, f"{routes}  ·  {host.address}")
            card.clicked.connect(lambda checked=False, host=host: self.select_host(host))
            card.setEnabled(not self.busy)
            self.device_buttons.append(card)
            self.devices_layout.insertWidget(self.devices_layout.count() - 1, card)
        if not self.device_buttons:
            empty = text_label("还没有发现其他设备\n在另一台电脑打开 Elink，连接同一局域网或 Tailscale 后即可在这里看到。", "emptyState")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setMinimumHeight(160)
            self.devices_layout.insertWidget(0, empty)
        self.discovery_state.setText(f"{len(self.device_buttons)} 台可连接设备 · 点击即可连接 · 自动刷新")

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
            self.options.restore({'mouse_mode': value.get('mouse_mode', 'smart')})
            for name in ("audio", "controllers"):
                getattr(self, name).setChecked(value.get(name, True))
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
        self._closing = True
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
        self.runtime.submit(cleanup(), finished, finished)
