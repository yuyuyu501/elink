"""Shared stream preferences and keyboard-accessible device rows."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QHBoxLayout,
                               QFrame, QLabel, QPushButton, QDoubleSpinBox, QSpinBox, QVBoxLayout, QWidget)
from .icons import icon_label
from ..core.mouse import MOUSE_MODES, mouse_mode, sensitivity_value


def section_heading(title, icon_name, aside=None):
    heading = QFrame()
    heading.setObjectName('sectionHeading')
    row = QHBoxLayout(heading)
    row.setContentsMargins(12, 12, 12, 12)
    row.setSpacing(12)
    row.addWidget(icon_label(icon_name))
    label = QLabel(title)
    label.setObjectName('sectionTitle')
    row.addWidget(label)
    row.addStretch()
    if aside is not None:
        row.addWidget(aside)
    return heading


class OptionRow(QFrame):
    """Aligned label/control row; checkbox rows also accept clicks on the label."""

    def __init__(self, label, control, icon_name=None, parent=None):
        super().__init__(parent)
        self.setObjectName('optionRow')
        self.control = control
        row = QHBoxLayout(self)
        row.setContentsMargins(14, 8, 14, 8)
        row.setSpacing(12)
        if icon_name:
            row.addWidget(icon_label(icon_name, 18))
        self.label = QLabel(label)
        self.label.setWordWrap(True)
        self.label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        row.addWidget(self.label, 1)
        control.setAccessibleName(label)
        row.addWidget(control)
        if isinstance(control, QCheckBox):
            control.setText('')
            self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and isinstance(self.control, QCheckBox):
            if self.control.isEnabled() and self.rect().contains(event.position().toPoint()):
                self.control.click()
            event.accept()
        else:
            super().mouseReleaseEvent(event)


class StreamOptions(QWidget):
    fields = ('resolution', 'renderer', 'render_scale', 'upscaler', 'fps', 'decoder', 'bitrate', 'audio', 'mouse_mode', 'mouse_sensitivity', 'controllers')

    def __init__(self, parent=None):
        super().__init__(parent)
        form = QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(20)
        form.setVerticalSpacing(12)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
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
        self.render_scale = QComboBox()
        for label, value in [('原生 100%', 100), ('75%', 75), ('66.7%', 67), ('50%', 50)]:
            self.render_scale.addItem(label, value)
        self.upscaler = QComboBox()
        self.upscaler.addItem('RTX Video Super Resolution', 'rtx')
        self.upscaler.addItem('FSR 1（兼容）', 'fsr')
        self.upscaler.setEnabled(False)
        self.render_scale.currentIndexChanged.connect(
            lambda: self.renderer.setCurrentIndex(1) if self.render_scale.currentData() != 100 else None)
        self.render_scale.currentIndexChanged.connect(
            lambda: self.upscaler.setEnabled(self.render_scale.currentData() != 100))
        self.renderer.currentIndexChanged.connect(
            lambda: self.render_scale.setCurrentIndex(0) if self.renderer.currentData() == 'cpu' else None)
        self.bitrate = QSpinBox()
        self.bitrate.setRange(1, 80)
        self.bitrate.setValue(20)
        self.bitrate.setSuffix(' Mbps')
        for label, widget in [('目标帧率', self.fps),
                              ('目标码率', self.bitrate), ('解码器', self.decoder), ('显示方式', self.renderer),
                              ('超分采样', self.render_scale), ('超分算法', self.upscaler)]:
            form.addRow(label, widget)
        self.audio = QCheckBox('传输远端系统声音')
        self.mouse_mode = QComboBox()
        for label, mode, hint in MOUSE_MODES:
            self.mouse_mode.addItem(label, mode)
            self.mouse_mode.setItemData(self.mouse_mode.count() - 1, hint, Qt.ItemDataRole.ToolTipRole)
        # Mouse settings are available only from the session's dedicated menu.
        self.mouse_mode.setParent(self)
        self.mouse_mode.hide()
        self.mouse_sensitivity = QDoubleSpinBox(self)
        self.mouse_sensitivity.setRange(-5, 5)
        self.mouse_sensitivity.setDecimals(1)
        self.mouse_sensitivity.hide()
        self.controllers = QCheckBox('转发 XInput 手柄与震动')
        for widget in (self.audio, self.controllers):
            widget.setChecked(True)
            form.addRow(widget)

    def values(self):
        return {name: (widget.currentData() if name in ('mouse_mode', 'resolution', 'renderer', 'render_scale', 'upscaler') else widget.currentIndex() if isinstance(widget, QComboBox) else
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
            elif name in ('renderer', 'render_scale', 'upscaler'):
                widget.setCurrentIndex(max(0, widget.findData(value)))
            elif name == 'mouse_mode':
                widget.setCurrentIndex(widget.findData(mouse_mode(value)))
            elif name == 'mouse_sensitivity':
                widget.setValue(sensitivity_value(value))
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
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(18)
        symbol = icon_label('monitor', 44, '#ffffff', glyph_size=26)
        symbol.setObjectName('deviceIcon')
        layout.addWidget(symbol)
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
        action = QFrame()
        action.setObjectName('connectAction')
        action_row = QHBoxLayout(action)
        action_row.setContentsMargins(14, 9, 12, 9)
        action_row.setSpacing(8)
        arrow = QLabel('连接')
        arrow.setObjectName('deviceAction')
        action_row.addWidget(arrow)
        action_row.addWidget(icon_label('chevron-right', 16, '#ffffff'))
        layout.addWidget(action, 0, Qt.AlignmentFlag.AlignVCenter)
        for child in self.findChildren(QWidget):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
