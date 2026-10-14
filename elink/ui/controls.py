"""Shared stream preferences and keyboard-accessible device rows."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QHBoxLayout,
                               QLabel, QPushButton, QSpinBox, QVBoxLayout, QWidget)
from ..paths import resource_dir
from ..core.mouse import MOUSE_MODES, mouse_mode


class StreamOptions(QWidget):
    fields = ('resolution', 'renderer', 'fps', 'decoder', 'bitrate', 'audio', 'mouse_mode', 'controllers')

    def __init__(self, parent=None):
        super().__init__(parent)
        form = QFormLayout(self)
        form.setSpacing(14)
        self.resolution = QComboBox()
        for width, height in ((1920, 1080), (1600, 900), (1280, 720), (2560, 1440), (3840, 2160)):
            self.resolution.addItem(f'{width} × {height}', [width, height])
        form.addRow('串流分辨率', self.resolution)
        self.fps = QComboBox()
        for fps in (60, 90, 120, 144, 165, 240, 30):
            self.fps.addItem(f'{fps} FPS', fps)
        self.decoder = QComboBox()
        for label, value in [('自动（优先硬件）', 'auto'), ('软件 H.264', 'software'), ('D3D11VA', 'hardware')]:
            self.decoder.addItem(label, value)
        self.renderer = QComboBox()
        self.renderer.addItem('兼容显示（CPU）', 'cpu')
        self.renderer.addItem('GPU 纹理显示（实验）', 'gpu')
        self.bitrate = QSpinBox()
        self.bitrate.setRange(1, 80)
        self.bitrate.setValue(20)
        self.bitrate.setSuffix(' Mbps')
        for label, widget in [('目标帧率', self.fps),
                              ('目标码率', self.bitrate), ('解码器', self.decoder), ('显示方式', self.renderer)]:
            form.addRow(label, widget)
        self.audio = QCheckBox('传输远端系统声音')
        self.mouse_mode = QComboBox()
        for label, mode, hint in MOUSE_MODES:
            self.mouse_mode.addItem(label, mode)
            self.mouse_mode.setItemData(self.mouse_mode.count() - 1, hint, Qt.ItemDataRole.ToolTipRole)
        # Mouse settings are available only from the session's dedicated menu.
        self.mouse_mode.setParent(self)
        self.mouse_mode.hide()
        self.controllers = QCheckBox('转发 XInput 手柄与震动')
        for widget in (self.audio, self.controllers):
            widget.setChecked(True)
            form.addRow(widget)

    def values(self):
        return {name: (widget.currentData() if name in ('mouse_mode', 'resolution', 'renderer') else widget.currentIndex() if isinstance(widget, QComboBox) else
                       widget.isChecked() if isinstance(widget, QCheckBox) else widget.value())
                for name in self.fields for widget in (getattr(self, name),)}

    def restore(self, values):
        for name in self.fields:
            if name not in values:
                continue
            widget, value = getattr(self, name), values[name]
            if name == 'resolution':
                index = widget.findData(value) if isinstance(value, list) else -1
                widget.setCurrentIndex(index if index >= 0 else 0)
            elif name == 'renderer':
                widget.setCurrentIndex(max(0, widget.findData(value)))
            elif name == 'mouse_mode':
                widget.setCurrentIndex(widget.findData(mouse_mode(value)))
            elif isinstance(widget, QComboBox):
                widget.setCurrentIndex(max(0, min(widget.count() - 1, value)))
            elif isinstance(widget, QCheckBox):
                widget.setChecked(value)
            else:
                widget.setValue(value)


class DeviceCard(QPushButton):
    def __init__(self, name, description, parent=None):
        super().__init__(parent)
        self.setObjectName('deviceCard')
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(88)
        self.setAccessibleName(f'连接 {name}')
        self.setToolTip(description)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 14, 22, 14)
        layout.setSpacing(18)
        icon = QLabel()
        icon.setPixmap(QIcon(str(resource_dir() / 'monitor.svg')).pixmap(28, 28))
        icon.setObjectName('deviceIcon')
        icon.setFixedSize(44, 44)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(icon)
        text = QVBoxLayout()
        text.setSpacing(5)
        title = QLabel(name)
        title.setObjectName('sectionTitle')
        detail = QLabel(description)
        detail.setObjectName('muted')
        for label in (title, detail):
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            text.addWidget(label)
        layout.addLayout(text, 1)
        arrow = QLabel('连接  ›')
        arrow.setObjectName('deviceAction')
        layout.addWidget(arrow)
        for child in self.findChildren(QWidget):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
