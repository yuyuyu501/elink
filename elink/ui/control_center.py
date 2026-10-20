"""Session controls that stay open while users adjust multiple options."""
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QCheckBox, QDoubleSpinBox, QFormLayout, QFrame,
                               QPushButton, QRadioButton, QScrollArea, QVBoxLayout, QWidget)

from ..core.mouse import MOUSE_MODES
from .controls import OptionRow
from .icons import icon


class ControlCenter(QFrame):
    def __init__(self, player):
        super().__init__(player)
        self.player = player
        self.setObjectName('controlCenter')
        self.setFixedWidth(320)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(1, 1, 1, 1)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(self.scroll)
        body = QWidget()
        self.rows = QVBoxLayout(body)
        self.rows.setContentsMargins(8, 8, 8, 8)
        self.rows.setSpacing(4)
        self.scroll.setWidget(body)
        self.quality = self.command('画质与传输设置…', player.open_quality, 'sliders-horizontal')
        self.quality.setEnabled(player.client.pc is not None)
        self.mouse_button = self.command('鼠标设置', self.toggle_mouse, 'mouse')
        self.mouse_button.setCheckable(True)
        self.mouse_options = QWidget()
        mouse_rows = QVBoxLayout(self.mouse_options)
        mouse_rows.setContentsMargins(38, 4, 10, 12)
        self.modes = {}
        for label, mode, hint in MOUSE_MODES:
            radio = QRadioButton(label)
            radio.setToolTip(hint)
            radio.clicked.connect(lambda checked=False, mode=mode: self.select_mode(mode))
            mouse_rows.addWidget(radio)
            self.modes[mode] = radio
        form = QFormLayout()
        self.sensitivity = QDoubleSpinBox()
        self.sensitivity.setRange(-5, 5)
        self.sensitivity.setDecimals(1)
        self.sensitivity.setSingleStep(.1)
        self.sensitivity.setKeyboardTracking(False)
        self.sensitivity.setToolTip('0.0 保持默认速率；-5.0 为 0.5 倍，+5.0 为 2 倍。')
        self.sensitivity.valueChanged.connect(player.set_mouse_sensitivity)
        form.addRow('灵敏度', self.sensitivity)
        mouse_rows.addLayout(form)
        self.rows.addWidget(self.mouse_options)
        self.mouse_options.hide()
        self.controllers = self.check('转发手柄与震动', player.set_controllers, 'gamepad-2')
        self.muted = self.check('本机静音', player.set_muted, 'volume-2')
        self.separator()
        self.statistics = self.check('显示连接统计    F10', player.set_statistics, 'chart-column')
        self.export = self.command('导出性能报告…', player.export_performance, 'file-down')
        self.export.setEnabled(hasattr(player.client, 'performance_report'))
        self.separator()
        self.fullscreen = self.command('全屏    F11', player.toggle_fullscreen, 'maximize')
        self.command('释放键鼠输入', player.release, 'keyboard')
        self.separator()
        self.command('断开连接', player.close, 'power').setObjectName('danger')
        for key, callback in (('F8', player.open_controls), ('F10', self.statistics.click),
                              ('F11', player.toggle_fullscreen)):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(callback)

    def command(self, label, callback, icon_name):
        widget = QPushButton(label)
        widget.setIcon(icon(icon_name, '#d33c51' if icon_name == 'power' else '#172b46'))
        widget.setIconSize(QSize(18, 18))
        widget.clicked.connect(callback)
        self.rows.addWidget(widget)
        return widget

    def check(self, label, callback, icon_name):
        widget = QCheckBox()
        def changed(checked):
            callback(checked)
            self.sync()
        widget.clicked.connect(changed)
        self.rows.addWidget(OptionRow(label, widget, icon_name))
        return widget

    def select_mode(self, mode):
        self.player.set_mouse_mode(mode)
        self.sync()

    def separator(self):
        line = QFrame()
        line.setObjectName('menuSeparator')
        line.setFrameShape(QFrame.Shape.HLine)
        self.rows.addWidget(line)

    def toggle_mouse(self):
        self.mouse_options.setVisible(self.mouse_button.isChecked())
        self.place()

    def sync(self):
        player = self.player
        supported = getattr(player.client, 'cursor_supported', False)
        for mode, radio in self.modes.items():
            radio.setEnabled(supported or mode == 'remote')
            radio.setChecked(player.mouse_mode == mode)
        self.sensitivity.blockSignals(True)
        self.sensitivity.setValue(player.mouse_sensitivity.value)
        self.sensitivity.blockSignals(False)
        self.controllers.setChecked(player.controllers)
        self.muted.setChecked(player.muted)
        self.statistics.setChecked(player.show_statistics)
        self.fullscreen.setText('退出全屏    F11' if player.isFullScreen() else '全屏    F11')

    def place(self):
        player = self.player
        top = player.toolbar.height()
        right = player.controls_button.mapTo(player, player.controls_button.rect().topRight()).x() + 1
        self.setGeometry(max(8, min(right - self.width(), player.width() - self.width() - 8)), top + 8,
                         self.width(), min(self.scroll.widget().sizeHint().height() + 2, player.height() - top - 16))
        self.raise_()

    def mousePressEvent(self, event):
        event.accept()

    def mouseReleaseEvent(self, event):
        event.accept()

    def wheelEvent(self, event):
        event.accept()
