from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMainWindow, QPlainTextEdit, QPushButton, QSpinBox, QTabWidget,
                               QVBoxLayout, QWidget)

from ..core.runtime import Runtime
from ..core.transport import Client, HostServer
from ..models import Endpoint
from ..network import netcheck, tailnet_ping, tailscale_path
from ..discovery import NetworkScope, discover, machine_id
from ..storage import atomic_json
from .player import Player
from .about import AboutPage
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
    def __init__(self, root: Path, *, auto_refresh=True):
        super().__init__()
        self.root = root
        self.runtime = Runtime(self)
        self.runtime.message.connect(self.report_error)
        self.server = None
        self.client = Client(root, self.runtime.message.emit, self.runtime.feedback.emit)
        self.player = None
        self.scope = NetworkScope()
        self.local_id = machine_id()
        self.discovery_future = None
        self.discovery_pending = False
        self.busy = False
        self._closing = self._closed = False
        self.poll_pending = False
        self.config_path = root / "desktop-v3.json"
        self.config_error = False
        self.setWindowTitle(f"Elink · 独立串流预览版 {__version__}")
        self.resize(1120, 800)
        self.setMinimumSize(900, 680)
        outer = QWidget()
        self.setCentralWidget(outer)
        layout = QVBoxLayout(outer)
        layout.setContentsMargins(28, 22, 28, 18)
        title = QHBoxLayout()
        title.addWidget(text_label("Elink", "pageTitle"))
        title.addStretch()
        title.addWidget(text_label(f"WINDOWS · H.264 / OPUS · {__version__} PREVIEW", "muted"))
        layout.addLayout(title)
        layout.addWidget(text_label("连接自己的游戏主机", "sectionTitle"))
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        self.make_remote()
        self.make_host()
        self.make_network()
        self.about = AboutPage(self)
        self.tabs.addTab(self.about, "关于")
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(200)
        self.log.setMaximumHeight(130)
        layout.addWidget(self.log)
        self.statusBar().showMessage("就绪 · 本机被控未启动")
        self.load_config()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_host)
        self.timer.start(1000)
        self.discovery_timer = QTimer(self)
        self.discovery_timer.timeout.connect(self.refresh_network)
        if auto_refresh:
            self.discovery_timer.start(10000)
            QTimer.singleShot(100, self.refresh_network)

    def page(self, name):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 20, 4, 10)
        self.tabs.addTab(page, name)
        return layout

    def make_remote(self):
        layout = self.page("远程连接")
        layout.addWidget(text_label("两端使用 Elink 0.4.2 或更新版本。主机开启被控后，这里自动发现并直接连接。", "notice"))
        row = QHBoxLayout()
        self.hosts = QComboBox()
        self.hosts.addItem("选择发现的主机…", None)
        self.hosts.activated.connect(self.select_host)
        row.addWidget(self.hosts, 1)
        self.refresh_button = button("刷新主机", self.refresh_network)
        row.addWidget(self.refresh_button)
        layout.addLayout(row)
        self.discovery_state = text_label("自动发现同局域网 / 同 Tailscale 下已开启被控的主机。", "muted")
        layout.addWidget(self.discovery_state)
        form = QFormLayout()
        self.address = QLineEdit()
        self.address.setPlaceholderText("自动填入，也可手动填写 IP / 主机名，默认端口 49200")
        form.addRow("主机地址", self.address)
        self.resolution = QComboBox()
        for label, size in [("1920 × 1080", (1920, 1080)), ("1600 × 900", (1600, 900)), ("1280 × 720", (1280, 720))]:
            self.resolution.addItem(label, size)
        form.addRow("分辨率", self.resolution)
        self.fps = QComboBox()
        for value in (60, 90, 120, 30):
            self.fps.addItem(f"{value} FPS", value)
        form.addRow("目标帧率", self.fps)
        self.bitrate = QSpinBox()
        self.bitrate.setRange(1, 80)
        self.bitrate.setValue(20)
        self.bitrate.setSuffix(" Mbps")
        form.addRow("视频码率上限", self.bitrate)
        self.decoder = QComboBox()
        for label, value in [("自动（优先 D3D11VA）", "auto"), ("软件 H.264", "software"), ("D3D11VA", "hardware")]:
            self.decoder.addItem(label, value)
        form.addRow("解码器", self.decoder)
        layout.addLayout(form)
        row = QHBoxLayout()
        self.audio = QCheckBox("传输系统声音（立体声）")
        self.audio.setChecked(True)
        self.game_mouse = QCheckBox("游戏相对鼠标")
        self.game_mouse.setChecked(True)
        self.controllers = QCheckBox("XInput 手柄 / 震动")
        self.controllers.setChecked(True)
        for item in (self.audio, self.game_mouse, self.controllers):
            row.addWidget(item)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.connect_button = button("开始串流", self.connect_remote, True)
        self.disconnect_button = button("断开", self.disconnect_remote)
        for item in (self.connect_button, self.disconnect_button):
            row.addWidget(item)
        row.addStretch()
        layout.addLayout(row)
        layout.addWidget(text_label("当前支持当前桌面串流，可在桌面中启动游戏。120 FPS 是请求目标，实际表现取决于显卡、显示器、网络和解码性能。", "muted"))
        layout.addStretch()

    def make_host(self):
        layout = self.page("本机被控")
        layout.addWidget(text_label("开启后，同局域网或同 Tailscale 下的设备可直接控制本机，无需邀请、配对或批准。关闭被控即停止接入。", "notice"))
        row = QHBoxLayout()
        self.port = QSpinBox()
        self.port.setRange(1024, 65535)
        self.port.setValue(49200)
        row.addWidget(text_label("监听端口"))
        row.addWidget(self.port)
        self.host_toggle = button("开启被控", self.toggle_host, True)
        row.addWidget(self.host_toggle)
        self.host_status = text_label("已关闭", "muted")
        row.addWidget(self.host_status, 1)
        layout.addLayout(row)
        self.pad_backend = QComboBox()
        self.pad_backend.addItem("ViGEmBus（兼容模式，需已安装）", "vigem")
        self.pad_backend.addItem("ElinkPad（实验驱动，单手柄）", "elinkpad")
        self.pad_backend.addItem("禁用虚拟手柄", "disabled")
        pad_form = QFormLayout()
        pad_form.addRow("主机手柄后端", self.pad_backend)
        layout.addLayout(pad_form)
        self.session_status = text_label("当前无串流连接", "sectionTitle")
        layout.addWidget(self.session_status)
        self.end_session_button = button("断开当前控制端", self.end_host_session)
        self.end_session_button.setEnabled(False)
        layout.addWidget(self.end_session_button, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addStretch()
        layout.addWidget(text_label("ElinkPad 尚未签名或完成实机游戏验证，需在独立测试系统加载。手柄后端失败不会自动切换；键鼠控制暂不支持锁屏和 UAC 安全桌面。", "muted"))

    def make_network(self):
        layout = self.page("网络诊断")
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
        layout.addWidget(text_label("远程连接页会合并同一台主机的局域网 / Tailscale 地址。Tailscale 在线不等于 Elink 被控已开启；路径探测才能确认网络路径。HTTPS 使用 TCP 49200，自动发现使用 UDP 49201，媒体使用 ICE 动态 UDP 端口；防火墙需允许 Elink 应用入站。", "muted"))

    def append_log(self, message):
        self.log.appendPlainText(f"{time.strftime('%H:%M:%S')}  {message}")

    def report_error(self, message):
        self.append_log(message)
        self.statusBar().showMessage(str(message), 12000)

    def set_busy(self, busy):
        self.busy = busy
        self.connect_button.setEnabled(not busy)

    def connect_remote(self):
        if self.player:
            self.player.raise_()
            return
        try:
            address = Endpoint.parse(self.address.text()).authority
        except ValueError as exc:
            self.report_error(str(exc))
            return
        self.save_config()
        self.set_busy(True)
        width, height = self.resolution.currentData()
        config = dict(width=width, height=height, fps=self.fps.currentData(), bitrate=self.bitrate.value(), audio=self.audio.isChecked())
        host = self.hosts.currentData()
        addresses = [address]
        if host and address in host.routes:
            addresses += [route for route in host.routes if route != address]
        decoder = self.decoder.currentData()
        async def connect():
            import aiohttp
            for index, route in enumerate(addresses):
                try:
                    return await self.client.connect(route, config, decoder)
                except (OSError, asyncio.TimeoutError, aiohttp.ClientError):
                    if index == len(addresses) - 1:
                        raise
        self.runtime.submit(connect(), self.connected, self.connect_failed)

    def connected(self, result):
        if self._closing:
            return
        self.player = Player(self.runtime, self.client, game_mouse=self.game_mouse.isChecked(), controllers=self.controllers.isChecked())
        self.player.ended.connect(self.disconnect_remote)
        self.player.show()
        self.set_busy(False)
        self.append_log("串流窗口已打开，正在等待画面。")

    def connect_failed(self, message):
        self.set_busy(False)
        self.report_error(message)

    def disconnect_remote(self):
        player, self.player = self.player, None
        if player:
            player.close()
            player.deleteLater()
        self.set_busy(True)
        self.runtime.submit(self.client.disconnect(), lambda _: self.set_busy(False), self.connect_failed)

    def toggle_host(self):
        self.host_toggle.setEnabled(False)
        if self.server and self.server.runner:
            self.runtime.submit(self.server.stop(), lambda _: self.host_changed(False), self.host_failed)
        else:
            self.save_config()
            port = self.port.value()
            pad_backend = self.pad_backend.currentData()
            self.pad_backend.setEnabled(False)
            async def start():
                if self.server is None:
                    self.server = HostServer(self.root, notify=self.runtime.message.emit, pad_backend=pad_backend, scope=self.scope)
                self.server.pad_backend = pad_backend
                return await self.server.start(port=port)
            self.runtime.submit(start(), lambda _: self.host_changed(True), self.host_failed)

    def host_changed(self, active):
        self.host_toggle.setEnabled(True)
        self.host_toggle.setText("关闭被控" if active else "开启被控")
        self.port.setEnabled(not active)
        self.pad_backend.setEnabled(not active)
        self.host_status.setText(f"已开启 · TCP {self.port.value()}" if active else "已关闭")
        if not active:
            self.session_status.setText("当前无串流连接")
            self.end_session_button.setEnabled(False)
        self.append_log("本机被控已开启。" if active else "本机被控已关闭，已释放输入。")

    def host_failed(self, message):
        self.host_toggle.setEnabled(True)
        self.pad_backend.setEnabled(not (self.server and self.server.runner))
        self.report_error(message)

    def end_host_session(self):
        if self.server:
            self.runtime.submit(self.server.end_session(), lambda _: self.append_log("已断开当前控制端。"))

    def refresh_host(self):
        if not self.server or self.poll_pending or self._closing:
            return
        self.poll_pending = True
        async def snapshot():
            return self.server.device_id, self.server.pc is not None
        def display(result):
            self.poll_pending = False
            address, active = result
            self.session_status.setText(f"当前控制端：{address}" if active else "当前无串流连接")
            self.end_session_button.setEnabled(active)
        self.runtime.submit(snapshot(), display, lambda _: setattr(self, "poll_pending", False))

    def select_host(self, index):
        host = self.hosts.itemData(index)
        if host:
            self.address.setText(host.address)

    def display_hosts(self, hosts):
        previous = self.hosts.currentData()
        selected = previous.id if previous else None
        follow = not self.address.text() or previous and self.address.text() in previous.routes
        self.hosts.blockSignals(True)
        self.hosts.clear()
        self.hosts.addItem("选择发现的主机…", None)
        for host in hosts:
            routes = " / ".join(dict.fromkeys(host.routes.values()))
            self.hosts.addItem(f"{host.name} · {routes} · {host.address}", host)
        index = next((i for i in range(1, self.hosts.count()) if self.hosts.itemData(i).id == selected), 0)
        if not index and follow and hosts:
            index = 1
        self.hosts.setCurrentIndex(index)
        self.hosts.blockSignals(False)
        if follow and index:
            self.select_host(index)
        self.discovery_state.setText(f"发现 {len(hosts)} 台主机 · 选择后点击开始串流 · 每 10 秒刷新" if hosts else
                                    "暂未发现主机：请确认对端已开启被控，或直接填写地址连接。")

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
                self.discovery_state.setText(f"发现暂不可用：{message}，可直接填写主机地址。")
        self.discovery_future = self.runtime.submit(probe(), display, failed)

    def select_peer(self, item):
        self.address.setText(item.data(Qt.ItemDataRole.UserRole))
        self.tabs.setCurrentIndex(0)

    def probe_route(self):
        executable = tailscale_path()
        if not executable:
            self.report_error("未找到外部 Tailscale。")
            return
        self.runtime.submit(asyncio.to_thread(tailnet_ping, executable, self.address.text()), lambda p: self.report_error(f"路径 {p.kind} · RTT {p.latency_ms} ms · {p.detail}"))

    def check_nat(self):
        executable = tailscale_path()
        if executable:
            self.runtime.submit(asyncio.to_thread(netcheck, executable), lambda report: self.report_error(json.dumps(report, ensure_ascii=False)))
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
            for name in ("resolution", "fps", "decoder"):
                widget = getattr(self, name)
                widget.setCurrentIndex(max(0, min(widget.count() - 1, value.get(name, 0))))
            for name in ("audio", "game_mouse", "controllers"):
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
        for name in ("resolution", "fps", "decoder"):
            value[name] = getattr(self, name).currentIndex()
        for name in ("audio", "game_mouse", "controllers"):
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
            await self.client.disconnect()
            if self.server:
                await self.server.stop()
        def finished(_):
            self.runtime.stop()
            self._closed = True
            self.close()
        self.runtime.submit(cleanup(), finished, finished)
