"""Independent remote desktop window and local session controls."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QFrame,
    QHBoxLayout, QPushButton, QScrollArea, QSizePolicy, QToolButton, QVBoxLayout, QWidget,
)

from .. import icons
from .preview import PreviewWidget
from .widgets import NoticeBar, StatusPill, label

TITLE_HEIGHT = 44


class PictureDialog(QDialog):
    def __init__(self, picture: dict, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("画面设置")
        self.setMinimumWidth(300)
        form = QFormLayout(self)
        self.resolution = QComboBox()
        self.resolution.addItems(["1280×720", "1920×1080", "2560×1440", "3840×2160"])
        current = f"{picture['width']}×{picture['height']}"
        if self.resolution.findText(current) < 0:
            self.resolution.addItem(current)
        self.resolution.setCurrentText(current)
        self.fps = QDoubleSpinBox()
        self.fps.setRange(5, 60)
        self.fps.setValue(picture["fps"])
        self.bitrate = QDoubleSpinBox()
        self.bitrate.setDecimals(3)
        self.bitrate.setRange(0.3, 20)
        self.bitrate.setValue(picture["bitrate_kbps"] / 1000)
        form.addRow("分辨率", self.resolution)
        form.addRow("帧率", self.fps)
        form.addRow("码率（Mbps）", self.bitrate)
        apply = QToolButton()
        apply.setIcon(icons.icon("check"))
        apply.setToolTip("应用画面设置")
        apply.clicked.connect(self.accept)
        form.addRow("", apply)

    def picture(self) -> dict:
        width, height = map(int, self.resolution.currentText().split("×"))
        return {"width": width, "height": height, "fps": self.fps.value(),
                "bitrate_kbps": round(self.bitrate.value() * 1000)}


class StreamTitleBar(QFrame):
    def __init__(self, window) -> None:
        super().__init__(window)
        self.window = window
        self.setObjectName("streamToolbar")
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(icons.icon_label("monitor", 18))
        self.title = label("Elink / 远程桌面", "streamTitle")
        self.title.setMinimumWidth(0)
        self.title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        row.addWidget(self.title, 1)
        self.pill = StatusPill("连接中", "busy")
        self.pill.setFixedHeight(28)
        row.addWidget(self.pill, 0, Qt.AlignmentFlag.AlignVCenter)
        self.controls = self.button("settings", "控制中心（F8）", window.toggle_controls)
        row.addWidget(self.controls)
        row.addWidget(self.button("minus", "最小化", window.minimize_window))
        self.maximize = self.button("maximize", "最大化", window.toggle_maximized)
        row.addWidget(self.maximize)
        close = self.button("x", "关闭远控", window.close)
        close.setObjectName("streamClose")
        row.addWidget(close)

    def button(self, icon: str, tooltip: str, callback) -> QToolButton:
        button = QToolButton()
        button.setObjectName("streamTool")
        button.setIcon(icons.icon(icon))
        button.setIconSize(QSize(17, 17))
        button.setToolTip(tooltip)
        button.setAccessibleName(tooltip)
        button.setFixedSize(42, TITLE_HEIGHT)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.clicked.connect(callback)
        return button

    def mousePressEvent(self, event) -> None:
        self.window.release_input()
        if event.button() == Qt.MouseButton.LeftButton and not self.window.isFullScreen():
            handle = self.window.windowHandle()
            if handle is not None:
                handle.startSystemMove()
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.window.toggle_maximized()
        event.accept()


class ResizeHandle(QWidget):
    def __init__(self, window, edges, cursor) -> None:
        super().__init__(window)
        self.window, self.edges = window, edges
        self.setCursor(cursor)

    def mousePressEvent(self, event) -> None:
        self.window.release_input()
        if event.button() == Qt.MouseButton.LeftButton and self.window.windowHandle():
            self.window.windowHandle().startSystemResize(self.edges)
        event.accept()


class StreamWindow(QWidget):
    closed = Signal()
    stats_toggled = Signal(bool)
    picture_requested = Signal()
    watch_toggled = Signal(bool)
    snapshot_requested = Signal()
    report_requested = Signal()
    preference_changed = Signal(str, object)

    def __init__(self, parent=None) -> None:
        super().__init__(None)
        self.setWindowTitle("Elink · 远程桌面")
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowMinMaxButtonsHint)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setObjectName("streamWindow")
        self.setMinimumSize(640, 400)
        self.resize(1280, 760)
        self._closed = False
        self._active = False
        self._input_available = False
        self._control_enabled = False
        self._was_maximized = False
        self.preview = PreviewWidget(self)
        self.preview.set_edge_padding(0)
        self.preview.capture_changed.connect(self._capture_changed)
        self.toolbar = StreamTitleBar(self)
        self.title = self.toolbar.title
        self.pill = self.toolbar.pill
        self.notice = NoticeBar(self)
        self.panel = QScrollArea(self)
        self.panel.setObjectName("streamPanel")
        self.panel.setWidgetResizable(True)
        self.panel.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        rows = QVBoxLayout(content)
        rows.setContentsMargins(12, 12, 12, 12)
        rows.setSpacing(8)
        self.session_controls = QWidget()
        controls = QVBoxLayout(self.session_controls)
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(8)
        self.picture_button = self._command("画面设置", "settings", self.picture_requested.emit)
        controls.addWidget(self.picture_button)
        self.watch_button = QCheckBox("观看模式")
        self.watch_button.toggled.connect(self.watch_toggled.emit)
        controls.addWidget(self.watch_button)
        form = QFormLayout()
        self.mouse_mode = QComboBox()
        self.mouse_mode.addItem("桌面鼠标", "absolute")
        self.mouse_mode.addItem("游戏鼠标", "relative")
        form.addRow("鼠标", self.mouse_mode)
        self.mouse_mode.currentIndexChanged.connect(
            lambda _: self.preference_changed.emit("mouse_mode", self.mouse_mode.currentData()))
        controls.addLayout(form)
        self.preferences = {}
        for name, text in (("clipboard_enabled", "同步剪贴板"), ("gamepad_enabled", "转发手柄"),
                           ("sr_enabled", "超分辨率")):
            check = QCheckBox(text)
            check.toggled.connect(lambda checked, name=name: self.preference_changed.emit(name, checked))
            self.preferences[name] = check
            controls.addWidget(check)
        sr = QHBoxLayout()
        self.sr_mode = QComboBox()
        self.sr_mode.addItems(["FSR", "RTX"])
        self.sr_output = QComboBox()
        self.sr_output.addItems(["1080p", "1440p", "4K"])
        sr.addWidget(self.sr_mode, 1)
        sr.addWidget(self.sr_output, 1)
        controls.addLayout(sr)
        self.sr_mode.currentTextChanged.connect(
            lambda text: self.preference_changed.emit("sr_mode", text.lower()))
        self.sr_output.currentTextChanged.connect(
            lambda text: self.preference_changed.emit("sr_output", text))
        rows.addWidget(self.session_controls)
        self.stats_button = QCheckBox("连接统计")
        self.stats_button.toggled.connect(self.stats_toggled.emit)
        rows.addWidget(self.stats_button)
        rows.addWidget(self._command("截图", "camera", self.snapshot_requested.emit))
        rows.addWidget(self._command("导出性能报告", "file-down", self.report_requested.emit))
        rows.addWidget(self._command("全屏", "maximize", self.toggle_fullscreen))
        rows.addWidget(self._command("释放键鼠输入", "keyboard", self.release_input))
        disconnect = self._command("断开连接", "power", self.close)
        disconnect.setObjectName("danger")
        rows.addWidget(disconnect)
        rows.addStretch()
        self.panel.setWidget(content)
        self.panel.hide()
        self.session_controls.setEnabled(False)
        self._handles = []
        left, right, top, bottom = Qt.Edge.LeftEdge, Qt.Edge.RightEdge, Qt.Edge.TopEdge, Qt.Edge.BottomEdge
        for edges, cursor in (
            (left, Qt.CursorShape.SizeHorCursor), (right, Qt.CursorShape.SizeHorCursor),
            (top, Qt.CursorShape.SizeVerCursor), (bottom, Qt.CursorShape.SizeVerCursor),
            (left | top, Qt.CursorShape.SizeFDiagCursor), (right | bottom, Qt.CursorShape.SizeFDiagCursor),
            (right | top, Qt.CursorShape.SizeBDiagCursor), (left | bottom, Qt.CursorShape.SizeBDiagCursor),
        ):
            self._handles.append(ResizeHandle(self, edges, cursor))
        self._shortcuts = []
        for keys, callback in (("F8", self.toggle_controls), ("F10", self.stats_button.toggle),
                               ("F11", self.toggle_fullscreen), ("Ctrl+Alt+Shift+Q", self.close),
                               ("Ctrl+Alt+Shift+Z", self.release_input)):
            shortcut = QShortcut(QKeySequence(keys), self)
            shortcut.activated.connect(callback)
            self._shortcuts.append(shortcut)

    def _command(self, text, icon, callback) -> QPushButton:
        button = QPushButton(text)
        button.setIcon(icons.icon(icon))
        button.clicked.connect(callback)
        return button

    def apply_settings(self, settings) -> None:
        widgets = [self.watch_button, self.mouse_mode, self.sr_mode, self.sr_output,
                   *self.preferences.values()]
        for widget in widgets:
            widget.blockSignals(True)
        self._control_enabled = settings.control_enabled
        self.watch_button.setChecked(not settings.control_enabled)
        self.mouse_mode.setCurrentIndex(max(0, self.mouse_mode.findData(settings.mouse_mode)))
        self.preview.set_mouse_mode(self.mouse_mode.currentData())
        for name, check in self.preferences.items():
            check.setChecked(getattr(settings, name))
        self.sr_mode.setCurrentText(settings.sr_mode.upper())
        self.sr_output.setCurrentText(settings.sr_output)
        self.sr_mode.setEnabled(settings.sr_enabled)
        self.sr_output.setEnabled(settings.sr_enabled)
        for widget in widgets:
            widget.blockSignals(False)
        self._sync_input()

    def set_session_active(self, active: bool) -> None:
        self._active = active
        self.session_controls.setEnabled(active)
        self._sync_input()

    def set_input_available(self, available: bool) -> None:
        self._input_available = available
        self._sync_input()

    def _sync_input(self) -> None:
        self.preview.set_control_enabled(self._active and self._input_available and self._control_enabled)

    def set_frame(self, frame) -> None:
        self.preview.set_frame(frame)

    def set_stats_lines(self, lines: list[str]) -> None:
        self.preview.set_stats_lines(lines)

    def set_stats_visible(self, visible: bool) -> None:
        self.preview.set_stats_visible(visible)
        self.stats_button.blockSignals(True)
        self.stats_button.setChecked(visible)
        self.stats_button.blockSignals(False)

    def set_status(self, text: str, tone: str = "ok") -> None:
        self.pill.set(text, tone)

    def release_input(self) -> None:
        self.preview.release_input()
        self._layout()

    def _capture_changed(self, captured: bool) -> None:
        if captured:
            self.panel.hide()
        self._layout()

    def toggle_controls(self) -> None:
        self.release_input()
        self.panel.setVisible(not self.panel.isVisible())
        self._layout()

    def toggle_fullscreen(self) -> None:
        self.release_input()
        self.panel.hide()
        if self.isFullScreen():
            self.showMaximized() if self._was_maximized else self.showNormal()
        else:
            self._was_maximized = self.isMaximized()
            self.showFullScreen()
        self._layout()

    def toggle_maximized(self) -> None:
        self.release_input()
        if self.isFullScreen():
            self.toggle_fullscreen()
        elif self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def minimize_window(self) -> None:
        self.release_input()
        self.showMinimized()

    def _layout(self) -> None:
        show_title = not self.isFullScreen() or not self.preview.captured
        self.toolbar.setVisible(show_title)
        self.toolbar.setGeometry(0, 0, self.width(), TITLE_HEIGHT)
        self.toolbar.maximize.setIcon(icons.icon("square" if self.isMaximized() or self.isFullScreen() else "maximize"))
        self.toolbar.maximize.setToolTip("还原" if self.isMaximized() or self.isFullScreen() else "最大化")
        top = TITLE_HEIGHT if show_title else 0
        video_top = 0 if self.isFullScreen() else TITLE_HEIGHT
        self.preview.setGeometry(0, video_top, self.width(), max(0, self.height() - video_top))
        self.panel.setGeometry(max(0, self.width() - 324), top + 8, 312,
                               max(0, min(self.panel.widget().sizeHint().height() + 2,
                                          self.height() - top - 20)))
        self.panel.raise_()
        self.notice.setGeometry(16, top + 8, max(0, self.width() - 32), self.notice.sizeHint().height())
        self.notice.raise_()
        w, h, edge = self.width(), self.height(), 5
        rects = [(0, edge, edge, h - edge * 2), (w - edge, edge, edge, h - edge * 2),
                 (edge, 0, w - edge * 2, edge), (edge, h - edge, w - edge * 2, edge),
                 (0, 0, edge, edge), (w - edge, h - edge, edge, edge),
                 (w - edge, 0, edge, edge), (0, h - edge, edge, edge)]
        for handle, rect in zip(self._handles, rects):
            handle.setVisible(not self.isFullScreen() and not self.isMaximized())
            handle.setGeometry(*rect)
            handle.raise_()

    def resizeEvent(self, event) -> None:
        self._layout()
        super().resizeEvent(event)

    def showEvent(self, event) -> None:
        self._layout()
        super().showEvent(event)

    def changeEvent(self, event) -> None:
        if event.type() in (QEvent.Type.WindowStateChange, QEvent.Type.ActivationChange):
            if self.isMinimized() or not self.isActiveWindow():
                self.release_input()
        super().changeEvent(event)

    def closeEvent(self, event) -> None:
        if not self._closed:
            self._closed = True
            self.release_input()
            self.closed.emit()
        event.accept()
