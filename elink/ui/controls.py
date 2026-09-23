"""Shared stream preferences and keyboard-accessible device rows."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QHBoxLayout,
                               QLabel, QPushButton, QSpinBox, QVBoxLayout, QWidget)
from ..paths import resource_dir
from ..core.mouse import MOUSE_MODES, mouse_mode


class StreamOptions(QWidget):
    fields = ('fps', 'decoder', 'bitrate', 'audio', 'mouse_mode', 'controllers')

    def __init__(self, parent=None, *, remote_display=False):
        super().__init__(parent)
        form = QFormLayout(self)
        form.setSpacing(14)
        self.display_snapshot = None
        self.display_resolution = QComboBox()
        self.display_refresh = QComboBox()
        self.scale = QComboBox()
        if remote_display:
            self.display_resolution.addItem('正在读取被控端…')
            self.display_refresh.addItem('正在读取被控端…')
            self.scale.addItem('正在读取被控端…')
            self.display_resolution.setEnabled(False)
            self.display_refresh.setEnabled(False)
            self.scale.setEnabled(False)
            form.addRow('被控端分辨率', self.display_resolution)
            form.addRow('被控端显示刷新率', self.display_refresh)
            form.addRow('被控端系统缩放', self.scale)
        else:
            self.display_resolution.setParent(self)
            self.display_refresh.setParent(self)
            self.scale.setParent(self)
            self.display_resolution.hide()
            self.display_refresh.hide()
            self.scale.hide()
        self.fps = QComboBox()
        for fps in (60, 90, 120, 144, 165, 240, 30):
            self.fps.addItem(f'{fps} FPS', fps)
        if remote_display:
            self.fps.hide()
        self.decoder = QComboBox()
        for label, value in [('自动（优先硬件）', 'auto'), ('软件 H.264', 'software'), ('D3D11VA', 'hardware')]:
            self.decoder.addItem(label, value)
        self.bitrate = QSpinBox()
        self.bitrate.setRange(1, 80)
        self.bitrate.setValue(20)
        self.bitrate.setSuffix(' Mbps')
        for label, widget in ([('目标帧率', self.fps)] if not remote_display else []) + [
                              ('目标码率', self.bitrate), ('解码器', self.decoder)]:
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

    def set_display(self, snapshot):
        self.display_snapshot = snapshot
        self.display_resolution.clear()
        for size in snapshot['resolutions']:
            label = f'{size[0]} × {size[1]}'
            if size == snapshot['current']:
                label += '（当前）'
            self.display_resolution.addItem(label, size)
        self.display_resolution.setCurrentIndex(self.display_resolution.findData(snapshot['current']))
        self.display_resolution.setEnabled(True)
        self._populate_refresh(snapshot['current'])
        self.scale.clear()
        for value in snapshot['scales']:
            label = f'{value}%'
            if value == snapshot['recommended_scale']:
                label += '（推荐）'
            self.scale.addItem(label, value)
        if snapshot['scales']:
            self.scale.setCurrentIndex(self.scale.findData(snapshot['scale']))
            self.scale.setEnabled(True)
        else:
            self.scale.addItem('此显示器不支持调整缩放')
            self.scale.setToolTip(snapshot.get('scale_error', ''))
            self.scale.setEnabled(False)

    def _display_resolution_changed(self):
        if self.display_snapshot:
            self._populate_refresh(self.display_resolution.currentData())

    def _populate_refresh(self, resolution):
        snapshot = self.display_snapshot
        if not snapshot or not isinstance(resolution, list):
            return
        modes = snapshot.get('display_modes', [])
        rates = sorted({int(mode[2]) for mode in modes
                        if len(mode) == 3 and mode[:2] == resolution and int(mode[2]) > 0})
        if not rates:
            rates = list(snapshot.get('refresh_rates', []))
        current = snapshot.get('current_refresh', 60)
        self.display_refresh.blockSignals(True)
        self.display_refresh.clear()
        for rate in rates:
            self.display_refresh.addItem(f'{rate} Hz', rate)
        selected = current if current in rates else (rates[0] if rates else None)
        if selected is not None:
            self.display_refresh.setCurrentIndex(self.display_refresh.findData(selected))
            self._sync_fps(selected)
        self.display_refresh.setEnabled(bool(rates))
        self.display_refresh.blockSignals(False)

    def _sync_fps(self, value):
        index = self.fps.findData(value)
        if index < 0:
            self.fps.addItem(f'{value} FPS', value)
            index = self.fps.findData(value)
        self.fps.setCurrentIndex(index)

    def display_unavailable(self, message):
        for widget in (self.display_resolution, self.display_refresh, self.scale):
            widget.clear()
            widget.addItem('不可用，请查看下方提示')
            widget.setToolTip(message)
            widget.setEnabled(False)

    def display_change(self):
        snapshot = self.display_snapshot
        if not snapshot:
            return None
        resolution = self.display_resolution.currentData()
        refresh = self.display_refresh.currentData() if self.display_refresh.isEnabled() else snapshot.get('current_refresh', 60)
        scale = self.scale.currentData() if self.scale.isEnabled() else snapshot['scale']
        if (resolution == snapshot['current'] and refresh == snapshot.get('current_refresh', refresh)
                and scale == snapshot['scale']):
            return None
        return dict(device=snapshot['device'], resolution=resolution, refresh=refresh, scale=scale)

    def values(self):
        return {name: (widget.currentData() if name == 'mouse_mode' else widget.currentIndex() if isinstance(widget, QComboBox) else
                       widget.isChecked() if isinstance(widget, QCheckBox) else widget.value())
                for name in self.fields for widget in (getattr(self, name),)}

    def restore(self, values):
        for name in self.fields:
            if name not in values:
                continue
            widget, value = getattr(self, name), values[name]
            if name == 'mouse_mode':
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
