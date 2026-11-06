"""设置页：窗口行为、设备发现、数据目录与关于。

行为参考 ``E:\\elink`` 的设置/关于页：用分组卡片 + 开关行呈现「本机行为」，
把日志目录、数据目录、版本与诊断入口集中在一处；不假装支持尚未实现的
在线更新或账号功能。
"""

from __future__ import annotations

import platform
import sys
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..settings import UiSettings
from .widgets import Card, IconTile, ToggleSwitch, divider, hint, label, title


class SettingRow(QWidget):
    """左侧图标 + 标题说明，右侧一个开关。"""

    def __init__(
        self,
        icon_name: str,
        title_text: str,
        description: str,
        tone: str = "blue",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(IconTile(icon_name, tone, 32))
        text_box = QVBoxLayout()
        text_box.setSpacing(2)
        text_box.addWidget(label(title_text, "cardTitle"))
        hint_label = hint(description)
        hint_label.setWordWrap(True)
        text_box.addWidget(hint_label)
        layout.addLayout(text_box, 1)
        self.toggle = ToggleSwitch()
        layout.addWidget(self.toggle, 0, Qt.AlignmentFlag.AlignVCenter)
        self.description = hint_label


class SettingsPage(QWidget):
    startup_changed = Signal(bool)
    tray_changed = Signal(bool)
    discovery_changed = Signal(bool)
    auto_host_changed = Signal(bool)
    open_data_requested = Signal()
    open_logs_requested = Signal()
    copy_diagnostics_requested = Signal()

    def __init__(self, root: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.root = root

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(14)
        outer.addWidget(title("设置"))
        outer.addWidget(label("本机行为、设备发现与诊断入口。", "pageSubtitle"))

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        holder = QWidget()
        content = QVBoxLayout(holder)
        content.setContentsMargins(0, 0, 6, 0)
        content.setSpacing(14)
        scroll.setWidget(holder)
        outer.addWidget(scroll, 1)

        general = Card("常规", "启动与关闭时的本机行为")
        self.startup_row = SettingRow(
            "power", "开机时自启动", "登录 Windows 后自动启动 elink（当前用户启动项）"
        )
        self.tray_row = SettingRow(
            "minus", "关闭时最小化到托盘", "关闭主窗口时保留会话，从托盘菜单退出", "slate"
        )
        self.auto_host_row = SettingRow(
            "play", "启动后自动开启主机模式", "使用上次保存的推流参数开始等待连接", "green"
        )
        general.body().addWidget(self.startup_row)
        general.add(divider())
        general.body().addWidget(self.tray_row)
        general.add(divider())
        general.body().addWidget(self.auto_host_row)
        content.addWidget(general)

        discovery = Card("设备发现", "同局域网中自动找到运行 elink 的机器")
        self.discovery_row = SettingRow(
            "link", "允许局域网发现", "通过 UDP 广播身份；不经过账号或云端", "cyan"
        )
        discovery.body().addWidget(self.discovery_row)
        self.discovery_hint = hint("发现服务已开启。")
        discovery.add(self.discovery_hint)
        content.addWidget(discovery)

        data = Card("数据与诊断", "设置、日志与截图都保存在本机")
        path_label = QLabel(str(root))
        path_label.setObjectName("mono")
        path_label.setWordWrap(True)
        path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        data.add(path_label)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        open_data = QPushButton("打开数据目录")
        open_logs = QPushButton("打开日志目录")
        copy_info = QPushButton("复制诊断信息")
        open_data.clicked.connect(self.open_data_requested.emit)
        open_logs.clicked.connect(self.open_logs_requested.emit)
        copy_info.clicked.connect(self.copy_diagnostics_requested.emit)
        buttons.addWidget(open_data)
        buttons.addWidget(open_logs)
        buttons.addWidget(copy_info)
        buttons.addStretch(1)
        data.body().addLayout(buttons)
        data.add(hint(f"日志文件：{root / 'logs' / 'desktop.log'}"))
        content.addWidget(data)

        about = Card("关于")
        self.version_value = label(__version__, "mono")
        runtime = f"Python {platform.python_version()} · {platform.system()} {platform.release()}"
        self.runtime_value = label(runtime, "muted")
        self.build_value = label("源码运行" if not getattr(sys, "frozen", False) else "打包版", "muted")
        for index, (name, value) in enumerate(
            (
                ("版本", self.version_value),
                ("运行环境", self.runtime_value),
                ("构建方式", self.build_value),
            )
        ):
            if index:
                about.add(divider())
            row = QHBoxLayout()
            row.setSpacing(12)
            row.addWidget(hint(name))
            row.addStretch(1)
            value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            value.setWordWrap(True)
            row.addWidget(value, 1)
            about.body().addLayout(row)
        about.add(hint("界面参考网易 UU 远程 4.x，并吸收 E:\\elink 的设备发现与设置逻辑。"))
        content.addWidget(about)
        content.addStretch(1)

        self.startup_row.toggle.toggled.connect(self.startup_changed.emit)
        self.tray_row.toggle.toggled.connect(self.tray_changed.emit)
        self.discovery_row.toggle.toggled.connect(self.discovery_changed.emit)
        self.auto_host_row.toggle.toggled.connect(self.auto_host_changed.emit)

    # ---- 状态 ----
    def set_ui(self, ui: UiSettings) -> None:
        widgets = (
            (self.startup_row.toggle, ui.start_on_login),
            (self.tray_row.toggle, ui.close_to_tray),
            (self.discovery_row.toggle, ui.allow_discovery),
            (self.auto_host_row.toggle, ui.auto_host),
        )
        for widget, checked in widgets:
            widget.blockSignals(True)
            widget.setChecked(checked)
            widget.blockSignals(False)

    def set_tray_available(self, available: bool) -> None:
        self.tray_row.toggle.setEnabled(available)
        self.tray_row.description.setText(
            "关闭主窗口时保留会话，从托盘菜单退出"
            if available
            else "当前系统没有可用的系统托盘"
        )

    def set_startup_available(self, available: bool) -> None:
        self.startup_row.toggle.setEnabled(available)
        self.startup_row.description.setText(
            "登录 Windows 后自动启动 elink（当前用户启动项）"
            if available
            else "当前平台不支持开机自启动"
        )

    def set_discovery_state(self, enabled: bool, message: str = "") -> None:
        self.discovery_hint.setText(message or ("发现服务已开启。" if enabled else "发现服务已关闭。"))

    def set_startup_state(self, enabled: bool) -> None:
        self.startup_row.toggle.blockSignals(True)
        self.startup_row.toggle.setChecked(enabled)
        self.startup_row.toggle.blockSignals(False)

    def set_runtime(self, *, version: str, build: str) -> None:
        self.version_value.setText(version)
        self.build_value.setText(build)
