"""Independent remote desktop window and local session controls."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, QSize, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFormLayout, QFrame,
    QHBoxLayout, QMenu, QScrollArea, QSizePolicy, QSlider, QToolButton,
    QVBoxLayout, QWidget, QWidgetAction,
)

from backend.input_capture.mouse import MOUSE_MODES
from backend.media.video.codec.probe import compatible_choices
from .. import icons
from .preview import PreviewWidget
from .widgets import NoticeBar, StatusPill, label

TITLE_HEIGHT = 44


def fps_control(slider: QSlider) -> QWidget:
    slider.setRange(5, 120)
    slider.setSingleStep(1)
    slider.setTickInterval(30)
    slider.setTickPosition(QSlider.TickPosition.TicksBelow)
    container = QWidget()
    layout = QVBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(2)
    row = QHBoxLayout()
    value = label(f"{slider.value()} FPS")
    value.setMinimumWidth(62)
    slider.valueChanged.connect(lambda number: value.setText(f"{number} FPS"))
    row.addWidget(slider, 1)
    row.addWidget(value)
    layout.addLayout(row)
    marks = QHBoxLayout()
    for number in (30, 60, 90, 120):
        marks.addWidget(label(str(number)), 1, Qt.AlignmentFlag.AlignRight)
    marks.addSpacing(66)
    layout.addLayout(marks)
    return container


class PictureDialog(QDialog):
    def __init__(self, picture: dict, parent=None, *, connection=None, embedded=False) -> None:
        super().__init__(parent)
        if embedded:
            self.setWindowFlags(Qt.WindowType.Widget)
        self.setWindowTitle("画面设置")
        self.setMinimumWidth(300)
        form = QFormLayout(self)
        self.resolution = QComboBox()
        self.resolution.addItems(["1280×720", "1920×1080", "2560×1440", "3840×2160"])
        current = f"{picture['width']}×{picture['height']}"
        if self.resolution.findText(current) < 0:
            self.resolution.addItem(current)
        self.resolution.setCurrentText(current)
        self.fps = QComboBox()
        for rate in (30, 60, 90, 120):
            self.fps.addItem(f"{rate} FPS", rate)
        nearest = min(range(self.fps.count()),
                      key=lambda index: abs(self.fps.itemData(index) - picture["fps"]))
        self.fps.setCurrentIndex(nearest)
        self.bitrate = QSlider(Qt.Orientation.Horizontal)
        self.bitrate.setRange(1, 50)
        self.bitrate.setSingleStep(1)
        self.bitrate.setValue(round(picture["bitrate_kbps"] / 1000))
        self.bitrate_value = label(str(self.bitrate.value()))
        self.bitrate_value.setFixedWidth(24)
        self.bitrate.valueChanged.connect(lambda value: self.bitrate_value.setText(str(value)))
        bitrate = QWidget()
        bitrate_row = QHBoxLayout(bitrate)
        bitrate_row.setContentsMargins(0, 0, 0, 0)
        bitrate_row.addWidget(self.bitrate, 1)
        bitrate_row.addWidget(self.bitrate_value)
        form.addRow("分辨率", self.resolution)
        form.addRow("帧率", self.fps)
        form.addRow("码率（Mbps）", bitrate)
        self.connection = connection
        self.codec = QComboBox()
        for name, value in (("AV1", "av1"), ("H.264", "h264"), ("H.265", "hevc")):
            self.codec.addItem(name, value)
        self.codec.setCurrentIndex(1)
        self.codec_status = label("")
        if connection is not None and connection._codec_enabled:
            form.addRow("视频格式", self.codec)
            form.addRow("", self.codec_status)
            self.codec.currentIndexChanged.connect(self._connection_state)
            connection.capabilities_changed.connect(self._update_codecs)
            connection.changed.connect(self._connection_state)
            options = connection._preferences if connection.phase == "selection" else connection._applied_options
            index = self.codec.findData(options["codec"])
            if index >= 0:
                self.codec.setCurrentIndex(index)
            self._update_codecs()
        self.apply_button = QToolButton()
        self.apply_button.setIcon(icons.icon("check"))
        self.apply_button.setToolTip("应用画面设置")
        self.apply_button.clicked.connect(self.accept)
        controls = QWidget()
        row = QHBoxLayout(controls)
        row.setContentsMargins(0, 0, 0, 0)
        row.addStretch()
        cancel = QToolButton()
        cancel.setIcon(icons.icon("x"))
        cancel.setToolTip("取消画面设置")
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        row.addWidget(self.apply_button)
        form.addRow("", controls)
        self._connection_state()

    def _update_codecs(self):
        remote, local = self.connection.codec_capabilities
        if remote is None or local is None:
            self.codec_status.setText("正在检测编解码能力…")
            choices = []
        else:
            try:
                choices = compatible_choices(remote, local, {})
            except ValueError:
                choices = []
            self.codec_status.setText("" if choices else "双方没有共同可用的视频格式")
        names = {choice["codec"] for choice in choices}
        for i in range(self.codec.count()):
            self.codec.model().item(i).setEnabled(self.codec.itemData(i) in names)
        self._connection_state()

    def _connection_state(self, *_):
        if self.connection is not None and hasattr(self, "apply_button"):
            active = self.connection.phase in {"active", "selection"}
            available = self.codec.model().item(self.codec.currentIndex()).isEnabled()
            self.apply_button.setEnabled(active and (available or not self.connection._codec_enabled))
            self.codec.setEnabled(active)

    def picture(self) -> dict:
        width, height = map(int, self.resolution.currentText().split("×"))
        result = {"width": width, "height": height, "fps": self.fps.currentData(),
                  "bitrate_kbps": self.bitrate.value() * 1000}
        if self.connection is not None and self.connection._codec_enabled:
            result["codec"] = self.codec.currentData()
        return result


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
    preference_changed = Signal(str, object)
    upscale_cancel = Signal()

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
        self.panel = QMenu(self)
        self.panel.setMinimumWidth(200)
        self.picture_menu = self.panel.addMenu(icons.icon("settings"), "画面设置")
        self.picture_button = self.picture_menu.menuAction()
        self.picture_menu.addAction("画面设置").setEnabled(False)
        self.picture_menu.aboutToShow.connect(self.picture_requested.emit)
        self.picture_menu.aboutToHide.connect(self._discard_picture)
        self._picture_editor = None
        self.mouse_menu = self.panel.addMenu(icons.icon("mouse"), "鼠标")
        mouse = QWidget()
        controls = QVBoxLayout(mouse)
        form = QFormLayout()
        self.mouse_mode = QComboBox()
        for text, mode in MOUSE_MODES:
            self.mouse_mode.addItem(text, mode)
        form.addRow("鼠标", self.mouse_mode)
        self.mouse_mode.currentIndexChanged.connect(
            lambda _: self.preference_changed.emit("mouse_mode", self.mouse_mode.currentData()))
        controls.addLayout(form)
        sensitivity = QHBoxLayout()
        self.sensitivity = QSlider(Qt.Orientation.Horizontal)
        self.sensitivity.setRange(-50, 50)
        self.sensitivity.setSingleStep(1)
        self.sensitivity_value = label("0.0")
        self.sensitivity_value.setMinimumWidth(32)
        self.sensitivity.valueChanged.connect(
            lambda value: self.sensitivity_value.setText(f"{value / 10:.1f}"))
        self.sensitivity.valueChanged.connect(
            lambda value: self.preference_changed.emit("mouse_sensitivity", value / 10))
        reset = QToolButton()
        reset.setIcon(icons.icon("refresh-cw"))
        reset.setFixedSize(28, 28)
        reset.setToolTip("恢复默认灵敏度")
        reset.clicked.connect(lambda: self.sensitivity.setValue(0))
        sensitivity.addWidget(label("灵敏度"))
        sensitivity.addWidget(self.sensitivity, 1)
        sensitivity.addWidget(self.sensitivity_value)
        sensitivity.addWidget(reset)
        controls.addLayout(sensitivity)
        self._menu_widget(self.mouse_menu, mouse)
        self.sr_menu = self.panel.addMenu(icons.icon("monitor"), "超分")
        sr_content = QWidget()
        controls = QVBoxLayout(sr_content)
        self.preferences = {}
        for name, text in (("sr_enabled", "超分辨率"),):
            check = QCheckBox(text)
            check.toggled.connect(lambda checked, name=name: self.preference_changed.emit(name, checked))
            self.preferences[name] = check
            controls.addWidget(check)
        sr = QHBoxLayout()
        self.sr_mode = QComboBox()
        self.sr_mode.addItem("FSR", "fsr")
        self.sr_mode.addItem("RTX", "rtx")
        sr.addWidget(self.sr_mode, 1)
        controls.addLayout(sr)
        self.sr_mode.currentIndexChanged.connect(
            lambda _: self.preference_changed.emit("sr_mode", self.sr_mode.currentData()))
        self.sr_percent = QSlider(Qt.Orientation.Horizontal)
        self.sr_percent.setRange(33, 100)
        self.sr_percent.setValue(100)
        self.sr_percent_value = label("100%")
        self.sr_percent_value.setFixedWidth(40)
        self.sr_percent.valueChanged.connect(lambda value: self.sr_percent_value.setText(f"{value}%"))
        self.sr_percent.valueChanged.connect(
            lambda value: self.preference_changed.emit("sr_percent", value)
            if not self.sr_percent.isSliderDown() else None)
        self.sr_percent.sliderReleased.connect(
            lambda: self.preference_changed.emit("sr_percent", self.sr_percent.value()))
        ratio = QHBoxLayout()
        ratio.addWidget(label("压缩比"))
        ratio.addWidget(self.sr_percent, 1)
        ratio.addWidget(self.sr_percent_value)
        controls.addLayout(ratio)
        self.sr_sharpen = QCheckBox("锐化")
        self.sr_sharpen.toggled.connect(lambda value: self.preference_changed.emit("sr_sharpen", value))
        self.sr_sharpness = QSlider(Qt.Orientation.Horizontal)
        self.sr_sharpness.setRange(0, 100)
        self.sr_sharpness_value = label("50")
        self.sr_sharpness_value.setFixedWidth(28)
        self.sr_sharpness.valueChanged.connect(lambda value: self.sr_sharpness_value.setText(str(value)))
        self.sr_sharpness.valueChanged.connect(
            lambda value: self.preference_changed.emit("sr_sharpness", value)
            if not self.sr_sharpness.isSliderDown() else None)
        self.sr_sharpness.sliderReleased.connect(
            lambda: self.preference_changed.emit("sr_sharpness", self.sr_sharpness.value()))
        sharp = QHBoxLayout()
        sharp.addWidget(self.sr_sharpen)
        sharp.addWidget(self.sr_sharpness, 1)
        sharp.addWidget(self.sr_sharpness_value)
        controls.addLayout(sharp)
        self.sr_status = label("")
        self.sr_status.setWordWrap(True)
        controls.addWidget(self.sr_status)
        self._menu_widget(self.sr_menu, sr_content)
        self.panel.addSeparator()
        muted = self.panel.addAction(icons.icon("volume-2"), "静音")
        muted.setCheckable(True)
        muted.toggled.connect(lambda checked: self.preference_changed.emit("muted", checked))
        self.preferences["muted"] = muted
        self.stats_button = self.panel.addAction(icons.icon("activity"), "性能叠加")
        self.stats_button.setCheckable(True)
        self.stats_button.toggled.connect(self.stats_toggled.emit)
        self.panel.addSeparator()
        self.panel.addAction(icons.icon("maximize"), "全屏").triggered.connect(self.toggle_fullscreen)
        self.panel.addAction(icons.icon("power"), "断开连接").triggered.connect(self.close)
        self._session_actions = [self.picture_button, self.mouse_menu.menuAction(),
                                 self.sr_menu.menuAction(), muted]
        for menu in (self.picture_menu, self.mouse_menu, self.sr_menu):
            menu.aboutToShow.connect(self._fit_submenu)
        for action in self._session_actions:
            action.setEnabled(False)
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

    def _menu_widget(self, menu, content):
        scroll = QScrollArea(menu)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(content)
        content.adjustSize()
        scroll.setFixedWidth(max(320, content.sizeHint().width() + 18))
        scroll.setFixedHeight(min(content.sizeHint().height() + 16,
                                  self.screen().availableGeometry().height() - 80))
        action = QWidgetAction(menu)
        action.setDefaultWidget(scroll)
        menu.addAction(action)
        for widget in (content, *content.findChildren(QWidget)):
            widget.installEventFilter(self)
        return action

    def set_picture_editor(self, editor):
        self._picture_editor = editor
        for action in self.picture_menu.actions():
            if isinstance(action, QWidgetAction):
                self.picture_menu.removeAction(action)
                action.deleteLater()
        display = QComboBox(editor)
        for title, mode in (("自动", "auto"), ("GPU 纹理显示", "gpu"), ("CPU 兼容显示", "cpu")):
            display.addItem(title, mode)
        display.setCurrentIndex(display.findData(self.preview.display_mode))
        display.currentIndexChanged.connect(lambda _: self.preview.set_display_mode(display.currentData()))
        editor.layout().insertRow(3, "显示方式", display)
        self._menu_widget(self.picture_menu, editor)
        editor.finished.connect(self._picture_done)

    def _fit_submenu(self):
        for action in self.sender().actions():
            if isinstance(action, QWidgetAction):
                scroll = action.defaultWidget()
                content = scroll.widget()
                height = max(content.sizeHint().height(), content.heightForWidth(scroll.width() - 4))
                scroll.setFixedHeight(min(height + 4, self.screen().availableGeometry().height() - 80))

    def _picture_done(self, _result):
        self._picture_editor = None
        self.panel.close()

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
            self.panel.close()
            return True
        return super().eventFilter(watched, event)

    def _discard_picture(self):
        editor, self._picture_editor = self._picture_editor, None
        if editor is not None:
            editor.reject()

    def apply_settings(self, settings) -> None:
        widgets = [self.mouse_mode, self.sensitivity, self.sr_mode,
                   self.sr_percent, self.sr_sharpen, self.sr_sharpness,
                   *self.preferences.values()]
        for widget in widgets:
            widget.blockSignals(True)
        self._control_enabled = settings.control_enabled
        self.mouse_mode.setCurrentIndex(max(0, self.mouse_mode.findData(settings.mouse_mode)))
        self.preview.set_mouse_mode(self.mouse_mode.currentData())
        self.sensitivity.setValue(round(settings.mouse_sensitivity * 10))
        self.sensitivity_value.setText(f"{settings.mouse_sensitivity:.1f}")
        self.preview.set_sensitivity(settings.mouse_sensitivity)
        for name, check in self.preferences.items():
            check.setChecked(getattr(settings, name))
        self.sr_mode.setCurrentIndex(max(0, self.sr_mode.findData(settings.sr_mode)))
        self.sr_percent.setValue(settings.sr_percent)
        self.sr_percent_value.setText(f"{settings.sr_percent}%")
        self.sr_sharpen.setChecked(settings.sr_sharpen)
        self.sr_sharpness.setValue(settings.sr_sharpness)
        self.sr_sharpness_value.setText(str(settings.sr_sharpness))
        self.sr_mode.setEnabled(settings.sr_enabled)
        self.sr_percent.setEnabled(settings.sr_enabled)
        self.sr_sharpen.setEnabled(settings.sr_enabled)
        self.sr_sharpness.setEnabled(settings.sr_enabled and settings.sr_sharpen)
        for widget in widgets:
            widget.blockSignals(False)
        self._sync_input()

    def set_upscale_capabilities(self, capabilities, status=""):
        details = []
        for index in range(self.sr_mode.count()):
            name = self.sr_mode.itemData(index)
            item = (capabilities or {}).get(name, {})
            state = item.get("state")
            self.sr_mode.model().item(index).setEnabled(state in {"available", "unobserved"})
            message = item.get("message", "待检测")
            self.sr_mode.setItemData(index, message, Qt.ItemDataRole.ToolTipRole)
            details.append(f"{name.upper()}：{message}")
        self.sr_status.setText(status or "\n".join(details))

    def set_session_active(self, active: bool, *, settings_ready: bool = True) -> None:
        self._active = active
        for action in self._session_actions:
            action.setEnabled(active)
        self.mouse_mode.parentWidget().setEnabled(active)
        self.sr_mode.parentWidget().setEnabled(active and settings_ready)
        self.picture_button.setEnabled(active and settings_ready)
        if not active:
            self.panel.close()
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
            self.upscale_cancel.emit()
        self._layout()

    def toggle_controls(self) -> None:
        self.release_input()
        if self.panel.isVisible():
            self.panel.close()
        else:
            self.panel.popup(self.toolbar.controls.mapToGlobal(
                QPoint(self.toolbar.controls.width() - self.panel.sizeHint().width(), TITLE_HEIGHT)))

    def toggle_fullscreen(self) -> None:
        self.release_input()
        self.panel.hide()
        self.upscale_cancel.emit()
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
        self.preview.stats_top = TITLE_HEIGHT if self.isFullScreen() and show_title else 0
        self.preview.stats_right = 0
        self.preview.update()
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
        self.panel.close()
        self.upscale_cancel.emit()
        if not self._closed:
            self._closed = True
            self.release_input()
            self.closed.emit()
        event.accept()
