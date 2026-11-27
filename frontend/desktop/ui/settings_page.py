"""Settings for local behavior, input backends, diagnostics and application updates."""

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
    QComboBox,
    QCheckBox,
    QProgressBar,
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
    open_data_requested = Signal()
    open_logs_requested = Signal()
    copy_diagnostics_requested = Signal()
    admin_changed = Signal(bool)
    gamepad_changed = Signal(str)
    capture_backend_changed = Signal(str)
    check_update_requested = Signal()
    cancel_update_requested = Signal()
    previews_changed = Signal(bool)

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
        general.body().addWidget(self.startup_row)
        general.add(divider())
        general.body().addWidget(self.tray_row)
        general.add(divider())
        self.admin_row = SettingRow("shield", "以管理员身份启动", "控制被控端的高权限窗口时使用")
        self.admin_row.toggle.setEnabled(sys.platform == "win32")
        general.body().addWidget(self.admin_row)
        self.gamepad_backend = QComboBox()
        self.gamepad_backend.addItem("ViGEm（兼容模式）", "vigem")
        self.gamepad_backend.addItem("ElinkPad（实验，单手柄）", "elinkpad")
        self.gamepad_backend.setEnabled(sys.platform == "win32")
        general.add(label("本机手柄注入后端"))
        general.add(self.gamepad_backend)
        self.capture_backend = QComboBox()
        for name, value in (
            ("自动", "auto"), ("D3D11", "d3d11"), ("DXCam", "dxcam"), ("DXGI", "dxgi")
        ):
            self.capture_backend.addItem(name, value)
        self.capture_backend.setEnabled(sys.platform == "win32")
        general.add(divider())
        general.add(label("桌面采集方式"))
        general.add(self.capture_backend)
        content.addWidget(general)

        discovery = Card("设备发现", "通过各网络接口自动寻找运行 Elink 的机器")
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
        self.update_previews = QCheckBox("包含预览版本")
        about.add(self.update_previews)
        update_buttons = QHBoxLayout()
        self.check_update = QPushButton("检测更新")
        self.cancel_update = QPushButton("取消下载")
        self.cancel_update.hide()
        update_buttons.addWidget(self.check_update)
        update_buttons.addWidget(self.cancel_update)
        update_buttons.addStretch()
        about.body().addLayout(update_buttons)
        self.update_progress = QProgressBar()
        self.update_progress.hide()
        about.add(self.update_progress)
        self.update_status = hint("")
        self.update_status.setWordWrap(True)
        about.add(self.update_status)
        content.addWidget(about)
        content.addStretch(1)

        self.startup_row.toggle.toggled.connect(self.startup_changed.emit)
        self.tray_row.toggle.toggled.connect(self.tray_changed.emit)
        self.admin_row.toggle.toggled.connect(self.admin_changed.emit)
        self.gamepad_backend.currentIndexChanged.connect(
            lambda: self.gamepad_changed.emit(self.gamepad_backend.currentData()))
        self.capture_backend.currentIndexChanged.connect(
            lambda: self.capture_backend_changed.emit(self.capture_backend.currentData()))
        self.check_update.clicked.connect(self.check_update_requested.emit)
        self.cancel_update.clicked.connect(self.cancel_update_requested.emit)
        self.update_previews.toggled.connect(self.previews_changed.emit)

    # ---- 状态 ----
    def set_ui(self, ui: UiSettings) -> None:
        widgets = (
            (self.startup_row.toggle, ui.start_on_login),
            (self.tray_row.toggle, ui.close_to_tray),
            (self.admin_row.toggle, ui.admin_startup),
            (self.update_previews, ui.update_previews),
        )
        for widget, checked in widgets:
            widget.blockSignals(True)
            widget.setChecked(checked)
            widget.blockSignals(False)

    def set_update_busy(self, text, *, downloading=False):
        self.update_status.setText(text)
        self.check_update.setEnabled(not text or text not in {"正在检测更新…", "正在下载更新…", "正在准备更新…"})
        self.cancel_update.setVisible(downloading)
        self.update_progress.setVisible(downloading)

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
