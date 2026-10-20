"""Session controls that stay open while users adjust multiple options."""
from PySide6.QtCore import QEvent, Qt, QPoint, QSize
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QCheckBox, QFrame, QHBoxLayout, QLabel, QPushButton,
                               QRadioButton, QScrollArea, QSlider, QToolButton, QVBoxLayout, QWidget)

from ..core.mouse import MOUSE_MODES
from .controls import OptionRow
from .icons import icon


class LocalPanel(QFrame):
    def mousePressEvent(self, event):
        event.accept()

    def mouseReleaseEvent(self, event):
        event.accept()

    def wheelEvent(self, event):
        event.accept()


class ControlCenter(LocalPanel):
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
        self.mouse_options = LocalPanel(player)
        self.mouse_options.setObjectName('mouseSubmenu')
        self.mouse_options.setFixedWidth(280)
        mouse_rows = QVBoxLayout(self.mouse_options)
        mouse_rows.setContentsMargins(16, 14, 16, 14)
        mouse_rows.setSpacing(8)
        title = QLabel('鼠标设置')
        title.setObjectName('sectionTitle')
        mouse_rows.addWidget(title)
        self.modes = {}
        for label, mode, hint in MOUSE_MODES:
            radio = QRadioButton(label)
            radio.setToolTip(hint)
            radio.clicked.connect(lambda checked=False, mode=mode: self.select_mode(mode))
            mouse_rows.addWidget(radio)
            self.modes[mode] = radio
        mouse_rows.addSpacing(8)
        sensitivity_row = QHBoxLayout()
        sensitivity_row.addWidget(QLabel('灵敏度'))
        sensitivity_row.addStretch()
        self.sensitivity_value = QLabel('0.0')
        self.sensitivity_value.setFixedWidth(40)
        self.sensitivity_value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        sensitivity_row.addWidget(self.sensitivity_value)
        self.reset_sensitivity = QToolButton()
        self.reset_sensitivity.setObjectName('resetSensitivity')
        self.reset_sensitivity.setIcon(icon('refresh-cw'))
        self.reset_sensitivity.setFixedSize(28, 28)
        self.reset_sensitivity.setToolTip('恢复默认灵敏度')
        self.reset_sensitivity.setAccessibleName('恢复默认灵敏度')
        sensitivity_row.addWidget(self.reset_sensitivity)
        mouse_rows.addLayout(sensitivity_row)
        self.sensitivity = QSlider(Qt.Orientation.Horizontal)
        self.sensitivity.setAccessibleName('鼠标灵敏度')
        self.sensitivity.setRange(-50, 50)
        self.sensitivity.setSingleStep(1)
        self.sensitivity.setPageStep(10)
        self.sensitivity.setMinimumHeight(24)
        self.sensitivity.setToolTip('0.0 使用主控端默认速率；-5.0 为 0.5 倍，+5.0 为 2 倍。')
        self.sensitivity.valueChanged.connect(self.change_sensitivity)
        self.reset_sensitivity.clicked.connect(lambda: self.sensitivity.setValue(0))
        mouse_rows.addWidget(self.sensitivity)
        limits = QHBoxLayout()
        for index, text in enumerate(('-5.0', '0.0', '+5.0')):
            label = QLabel(text)
            label.setObjectName('muted')
            limits.addWidget(label)
            if index < 2:
                limits.addStretch()
        mouse_rows.addLayout(limits)
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
        for panel in (self, self.mouse_options):
            for key, callback in (('F8', player.open_controls), ('F10', self.statistics.click),
                                  ('F11', player.toggle_fullscreen)):
                shortcut = QShortcut(QKeySequence(key), panel)
                shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
                shortcut.activated.connect(callback)
        dismiss = QShortcut(QKeySequence('Esc'), self.mouse_options)
        dismiss.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        dismiss.activated.connect(self.dismiss_mouse)
        self.scroll.verticalScrollBar().valueChanged.connect(lambda _: self.place())

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
        self.place()

    def dismiss_mouse(self):
        self.mouse_button.setChecked(False)
        self.place()
        self.mouse_button.setFocus()

    def change_sensitivity(self, value):
        self.sensitivity_value.setText(f'{value / 10:+.1f}' if value else '0.0')
        self.player.set_mouse_sensitivity(value / 10)

    def sync(self):
        player = self.player
        supported = getattr(player.client, 'cursor_supported', False)
        for mode, radio in self.modes.items():
            radio.setEnabled(supported or mode == 'remote')
            radio.setChecked(player.mouse_mode == mode)
        self.sensitivity.blockSignals(True)
        value = round(player.mouse_sensitivity.value * 10)
        self.sensitivity.setValue(value)
        self.sensitivity_value.setText(f'{value / 10:+.1f}' if value else '0.0')
        self.sensitivity.blockSignals(False)
        self.controllers.setChecked(player.controllers)
        self.muted.setChecked(player.muted)
        self.statistics.setChecked(player.show_statistics)
        self.fullscreen.setText('退出全屏    F11' if player.isFullScreen() else '全屏    F11')

    def place(self):
        player = self.player
        top = player.toolbar.height()
        right = player.controls_button.mapTo(player, player.controls_button.rect().topRight()).x() + 1
        expanded = self.mouse_button.isChecked() and self.isVisible()
        left = self.mouse_options.width() + 16 if expanded else 8
        self.setGeometry(max(left, min(right - self.width(), player.width() - self.width() - 8)), top + 8,
                         self.width(), min(self.scroll.widget().sizeHint().height() + 4, player.height() - top - 16))
        self.raise_()
        self.mouse_options.setVisible(expanded)
        if expanded:
            height = min(self.mouse_options.sizeHint().height(), player.height() - top - 16)
            y = min(self.mouse_button.mapTo(player, QPoint()).y(), player.height() - height - 8)
            self.mouse_options.setGeometry(self.x() - self.mouse_options.width() - 8, max(top + 8, y),
                                           self.mouse_options.width(), height)
            self.mouse_options.raise_()

    def hideEvent(self, event):
        QApplication.instance().removeEventFilter(self)
        self.mouse_options.hide()
        super().hideEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        QApplication.instance().installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.MouseButtonPress and isinstance(watched, QWidget):
            player = self.player
            if watched.window() is player and self.isVisible():
                position = event.globalPosition().toPoint()
                for panel in (self, self.mouse_options, player.controls_button):
                    if panel.isVisible() and panel.rect().contains(panel.mapFromGlobal(position)):
                        return False
                self.hide()
                player._resume_on_hover = True
                player.setFocus()
                # Local window controls still act on this click; video dismissal
                # must not also press a button on the remote desktop.
                return watched is player
        return super().eventFilter(watched, event)
