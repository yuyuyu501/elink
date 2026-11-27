"""Independent remote desktop window and local session controls."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, QSignalBlocker, QSize, Qt, Signal
from PySide6.QtGui import QActionGroup, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFormLayout, QFrame,
    QHBoxLayout, QMenu, QSizePolicy, QSlider, QToolButton,
    QVBoxLayout, QWidget, QWidgetAction,
)

from backend.input_capture.mouse import MOUSE_MODES
from backend.media.video.codec.probe import codec_label, compatible_choices
from .. import icons
from .preview import PreviewWidget
from .widgets import NoticeBar, StatusPill, label

TITLE_HEIGHT = 44
RESOLUTIONS = ((1280, 720), (1920, 1080), (2560, 1440), (3840, 2160))
FRAME_RATES = (30, 60, 90, 120)
CODECS = (("AV1", "av1"), ("H.264", "h264"), ("H.265", "hevc"))
DISPLAY_MODES = (("自动", "auto"), ("GPU 纹理显示", "gpu"), ("CPU 兼容显示", "cpu"))


class StreamMenu(QMenu):
    def __init__(self, title, parent):
        super().__init__(title, parent)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

    def submenu(self, title, icon=None):
        child = StreamMenu(title, self)
        if icon is not None:
            child.setIcon(icon)
        self.addMenu(child)
        return child

    def _activate_choice(self, action):
        if action is not None and action.isEnabled() and action.property("keepSettingsOpen"):
            action.trigger()
            return True
        return False

    def mouseReleaseEvent(self, event):
        if (event.button() == Qt.MouseButton.LeftButton
                and self._activate_choice(self.actionAt(event.position().toPoint()))):
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if (event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space)
                and self._activate_choice(self.activeAction())):
            event.accept()
            return
        super().keyPressEvent(event)


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


def bitrate_control(parent):
    container = QWidget(parent)
    row = QHBoxLayout(container)
    row.setContentsMargins(0, 0, 0, 0)
    slider = QSlider(Qt.Orientation.Horizontal)
    slider.setRange(1, 50)
    slider.setSingleStep(1)
    value = label(str(slider.value()))
    value.setFixedWidth(24)
    slider.valueChanged.connect(lambda number: value.setText(str(number)))
    row.addWidget(slider, 1)
    row.addWidget(value)
    return container, slider, value


def available_codecs(connection, **options):
    remote, local = connection.codec_capabilities
    if remote is None or local is None:
        return set(), "正在检测编解码能力…"
    try:
        names = {choice["codec"] for choice in compatible_choices(remote, local, options)}
    except ValueError:
        names = set()
    return names, "" if names else "双方没有共同可用的视频格式"


def codec_implementations(connection, codec):
    remote, local = connection.codec_capabilities
    if remote is not None and local is not None:
        try:
            choices = compatible_choices(remote, local, {"codec": codec})
            return {role: sorted({choice[role] for choice in choices})
                    for role in ("encoder", "decoder")}
        except ValueError:
            pass
    return {"encoder": [], "decoder": []}


class PictureDialog(QDialog):
    def __init__(self, picture: dict, parent=None, *, connection=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("画面设置")
        self.setMinimumWidth(300)
        form = QFormLayout(self)
        self.resolution = QComboBox()
        self.resolution.addItems([f"{w}×{h}" for w, h in RESOLUTIONS])
        current = f"{picture['width']}×{picture['height']}"
        if self.resolution.findText(current) < 0:
            self.resolution.addItem(current)
        self.resolution.setCurrentText(current)
        self.fps = QComboBox()
        for rate in FRAME_RATES:
            self.fps.addItem(f"{rate} FPS", rate)
        nearest = min(range(self.fps.count()),
                      key=lambda index: abs(self.fps.itemData(index) - picture["fps"]))
        self.fps.setCurrentIndex(nearest)
        bitrate, self.bitrate, self.bitrate_value = bitrate_control(self)
        self.bitrate.setValue(round(picture["bitrate_kbps"] / 1000))
        form.addRow("分辨率", self.resolution)
        form.addRow("帧率", self.fps)
        form.addRow("码率（Mbps）", bitrate)
        self.connection = connection
        self.codec = QComboBox()
        for name, value in CODECS:
            self.codec.addItem(name, value)
        self.codec.setCurrentIndex(1)
        self.codec_status = label("")
        self.codec_implementations = {}
        if connection is not None and connection._codec_enabled:
            form.addRow("视频格式", self.codec)
            form.addRow("", self.codec_status)
            options = connection._preferences if connection.phase == "selection" else connection._applied_options
            index = self.codec.findData(options["codec"])
            if index >= 0:
                self.codec.setCurrentIndex(index)
            for key, title in (("encoder", "被控端编码器"), ("decoder", "主控端解码器")):
                combo = self.codec_implementations[key] = QComboBox()
                form.addRow(title, combo)
            self._update_codecs(options)
            self.codec.currentIndexChanged.connect(lambda _: self._update_codecs())
            connection.capabilities_changed.connect(self._update_codecs)
            connection.changed.connect(self._connection_state)
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

    def _update_codecs(self, options=None):
        names, status = available_codecs(self.connection)
        self.codec_status.setText(status)
        for i in range(self.codec.count()):
            self.codec.model().item(i).setEnabled(self.codec.itemData(i) in names)
        implementations = codec_implementations(self.connection, self.codec.currentData())
        for key, combo in self.codec_implementations.items():
            selected = (options or {}).get(key, combo.currentData())
            combo.clear()
            combo.addItem("自动", "auto")
            for name in implementations[key]:
                combo.addItem(codec_label(name), name)
            combo.setCurrentIndex(max(0, combo.findData(selected)))
        self._connection_state()

    def _connection_state(self, *_):
        if self.connection is not None and hasattr(self, "apply_button"):
            active = self.connection.phase in {"active", "selection"}
            codec = self.codec.currentData()
            for combo in self.codec_implementations.values():
                combo.setEnabled(active)
            names, _ = available_codecs(self.connection, codec=codec)
            available = codec in names
            self.apply_button.setEnabled(active and (available or not self.connection._codec_enabled))
            self.codec.setEnabled(active)

    def picture(self) -> dict:
        width, height = map(int, self.resolution.currentText().split("×"))
        result = {"width": width, "height": height, "fps": self.fps.currentData(),
                  "bitrate_kbps": self.bitrate.value() * 1000}
        if self.connection is not None and self.connection._codec_enabled:
            result["codec"] = self.codec.currentData()
            result.update({key: combo.currentData() for key, combo in self.codec_implementations.items()})
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
    picture_submitted = Signal(dict)
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
        self.preview = PreviewWidget(self, show_frame_info=False)
        self.preview.set_edge_padding(0)
        self.preview.capture_changed.connect(self._capture_changed)
        self.toolbar = StreamTitleBar(self)
        self.title = self.toolbar.title
        self.pill = self.toolbar.pill
        self.notice = NoticeBar(self)
        self.panel = StreamMenu("", self)
        self.panel.setMinimumWidth(200)
        self._picture_ready = False
        self.picture_menu = self.panel.submenu("画面设置", icons.icon("settings"))
        self.picture_button = self.picture_menu.menuAction()
        self.resolution_menu, self.resolution_choices = self._picture_choices("分辨率")
        _, self.fps_choices = self._picture_choices("帧率", ((f"{rate} FPS", rate) for rate in FRAME_RATES))
        self.bitrate_menu = self.picture_menu.submenu("码率（Mbps）")
        bitrate, self.bitrate, self.bitrate_value = bitrate_control(self.bitrate_menu)
        self.bitrate.setTracking(False)
        self.bitrate.sliderMoved.connect(lambda value: self.bitrate_value.setText(str(value)))
        self.bitrate.valueChanged.connect(self._bitrate_changed)
        self.bitrate.setObjectName("streamSlider")
        self._menu_widget(self.bitrate_menu, bitrate, min_width=200)
        self.codec_menu, self.codec_choices = self._picture_choices("视频格式", CODECS)
        self.codec_status = self.codec_menu.addAction("")
        self.codec_status.setEnabled(False)
        self.codec_impl_menu = self.picture_menu.submenu("视频编解码")
        self.codec_impl_menus = {}
        self.codec_impl_groups = {}
        for role, title in (("encoder", "编码器"), ("decoder", "解码器")):
            menu = self.codec_impl_menu.submenu(title)
            self.codec_impl_menus[role] = menu
            group = self.codec_impl_groups[role] = QActionGroup(menu)
            group.triggered.connect(self._submit_picture)
        _, self.display_choices = self._picture_choices("显示方式", DISPLAY_MODES)
        self.display_choices.triggered.connect(
            lambda action: self.preview.set_display_mode(action.data()))
        self.picture_menu.aboutToShow.connect(self.picture_requested.emit)
        for group in (self.resolution_choices, self.fps_choices):
            group.triggered.connect(self._submit_picture)
        self.codec_choices.triggered.connect(self._codec_format_selected)
        self.mouse_menu = self.panel.submenu("鼠标", icons.icon("mouse"))
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
        self.sensitivity.setObjectName("streamSlider")
        self.sensitivity.setRange(-50, 50)
        self.sensitivity.setSingleStep(1)
        self.sensitivity_value = label("0.0")
        self.sensitivity_value.setMinimumWidth(32)
        self.sensitivity.valueChanged.connect(
            lambda value: self.sensitivity_value.setText(f"{value / 10:.1f}"))
        self.sensitivity.valueChanged.connect(
            lambda value: self.preference_changed.emit("mouse_sensitivity", value / 10))
        sensitivity.addWidget(label("灵敏度"))
        sensitivity.addWidget(self.sensitivity, 1)
        sensitivity.addWidget(self.sensitivity_value)
        controls.addLayout(sensitivity)
        self._menu_widget(self.mouse_menu, mouse)
        self.panel.addSeparator()
        self.volume_menu = self.panel.submenu("音量", icons.icon("volume-2"))
        volume_controls = QWidget(self.volume_menu)
        volume_row = QHBoxLayout(volume_controls)
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setObjectName("streamSlider")
        self.volume.setRange(0, 100)
        self.volume_value = label("100%")
        self.volume_value.setFixedWidth(38)
        self.volume.valueChanged.connect(self._volume_changed)
        volume_row.addWidget(self.volume, 1)
        volume_row.addWidget(self.volume_value)
        self._menu_widget(self.volume_menu, volume_controls, min_width=200)
        self.stats_button = self.panel.addAction(icons.icon("activity"), "性能叠加")
        self.stats_button.setCheckable(True)
        self.stats_button.setProperty("keepSettingsOpen", True)
        self.stats_button.toggled.connect(self.stats_toggled.emit)
        self.panel.addSeparator()
        self.panel.addAction(icons.icon("maximize"), "全屏").triggered.connect(self.toggle_fullscreen)
        self.panel.addAction(icons.icon("power"), "断开连接").triggered.connect(self.close)
        self._session_actions = [self.picture_button, self.mouse_menu.menuAction(), self.volume_menu.menuAction()]
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

    def _menu_widget(self, menu, content, *, min_width=320):
        content.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        content.setMinimumWidth(min_width)
        action = QWidgetAction(menu)
        action.setDefaultWidget(content)
        menu.addAction(action)
        for widget in (content, *content.findChildren(QWidget)):
            widget.installEventFilter(self)
        return action

    def _picture_choices(self, title, choices=(), *, parent=None):
        menu = (parent if parent is not None else self.picture_menu).submenu(title)
        group = QActionGroup(menu)
        for text, value in choices:
            self._add_picture_choice(menu, group, text, value)
        return menu, group

    @staticmethod
    def _add_picture_choice(menu, group, text, value):
        action = menu.addAction(text)
        action.setCheckable(True)
        action.setProperty("keepSettingsOpen", True)
        action.setData(value)
        group.addAction(action)

    def load_picture(self, picture, connection, options=None):
        resolution = (picture["width"], picture["height"])
        resolutions = sorted({*RESOLUTIONS, resolution})
        if resolutions != [action.data() for action in self.resolution_choices.actions()]:
            self.resolution_menu.clear()
            for w, h in resolutions:
                self._add_picture_choice(self.resolution_menu, self.resolution_choices, f"{w} × {h}", (w, h))
        fps = min(FRAME_RATES, key=lambda rate: abs(rate - picture["fps"]))
        options = connection._applied_options if options is None else options
        codec = options["codec"]
        if codec == "auto":
            codec = (connection._selection or {}).get("codec", "h264")
        for group, value in ((self.resolution_choices, resolution), (self.fps_choices, fps),
                             (self.codec_choices, codec), (self.display_choices, self.preview.display_mode)):
            next(action for action in group.actions() if action.data() == value).setChecked(True)
        self._connection = connection
        preferred = {
            key: options.get(key, "auto")
            for key in ("encoder", "decoder")
        }
        with QSignalBlocker(self.bitrate):
            self.bitrate.setValue(round(picture["bitrate_kbps"] / 1000))
        self.bitrate_value.setText(str(self.bitrate.value()))
        self.refresh_picture_codecs(connection, preferred)

    def refresh_picture_codecs(self, connection, preferred=None):
        show_codecs = connection._codec_enabled or getattr(connection, "phase", "") == "connecting"
        self.codec_menu.menuAction().setVisible(show_codecs)
        self.codec_impl_menu.menuAction().setVisible(show_codecs)
        if self.codec_menu.menuAction().isVisible() and self.codec_choices.checkedAction():
            codec = self.codec_choices.checkedAction().data()
            names, status = available_codecs(connection)
            for action in self.codec_choices.actions():
                action.setEnabled(action.data() in names or None in connection.codec_capabilities)
            implementations = codec_implementations(connection, codec)
            if preferred is None:
                _, preferred = connection.picture_preferences
            for role, group in self.codec_impl_groups.items():
                value = preferred.get(role, "auto")
                values = ["auto", *implementations[role]]
                menu = self.codec_impl_menus[role]
                if values != [action.data() for action in group.actions()]:
                    menu.clear()
                    for name in values:
                        self._add_picture_choice(menu, group, "自动" if name == "auto" else codec_label(name), name)
                for action in group.actions():
                    action.setChecked(action.data() == value)
                menu.menuAction().setEnabled(codec in names)
            self.codec_status.setText(status)
            self.codec_status.setVisible(bool(status))

    def _codec_format_selected(self, _action):
        self.refresh_picture_codecs(self._connection, {"encoder": "auto", "decoder": "auto"})
        self._submit_picture()

    def _bitrate_changed(self):
        if self.bitrate_menu.isVisible():
            self._submit_picture()

    def _submit_picture(self, *_):
        resolution = self.resolution_choices.checkedAction()
        if resolution is None or not self._picture_ready:
            return
        width, height = resolution.data()
        picture = {"width": width, "height": height, "fps": self.fps_choices.checkedAction().data(),
                   "bitrate_kbps": self.bitrate.value() * 1000}
        codec = self.codec_choices.checkedAction()
        if self.codec_menu.menuAction().isVisible() and codec is not None:
            _, preferred = self._connection.picture_preferences
            source = self.sender()
            if source is self.codec_choices:
                picture.update(codec=codec.data(), encoder="auto", decoder="auto")
            for role, group in self.codec_impl_groups.items():
                if source is group:
                    picture.update(preferred)
                    picture["codec"] = codec.data()
                    picture[role] = group.checkedAction().data()
                    picture.pop(f"{role}_device", None)
        self.picture_submitted.emit(picture)

    def _volume_changed(self, value):
        self.volume_value.setText(f"{value}%")
        self.preference_changed.emit("volume", value)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
            self.panel.close()
            return True
        return super().eventFilter(watched, event)

    def apply_settings(self, settings) -> None:
        widgets = [self.mouse_mode, self.sensitivity, self.volume]
        for widget in widgets:
            widget.blockSignals(True)
        self._control_enabled = settings.control_enabled
        self.mouse_mode.setCurrentIndex(max(0, self.mouse_mode.findData(settings.mouse_mode)))
        self.preview.set_mouse_mode(self.mouse_mode.currentData())
        self.sensitivity.setValue(round(settings.mouse_sensitivity * 10))
        self.sensitivity_value.setText(f"{settings.mouse_sensitivity:.1f}")
        self.preview.set_sensitivity(settings.mouse_sensitivity)
        self.volume.setValue(settings.volume)
        self.volume_value.setText(f"{settings.volume}%")
        for widget in widgets:
            widget.blockSignals(False)
        self._sync_input()

    def set_session_active(self, active: bool, *, settings_ready: bool | None = None) -> None:
        self._active = active
        self._picture_ready = active if settings_ready is None else settings_ready
        for action in self._session_actions:
            action.setEnabled(active)
        self.mouse_mode.parentWidget().setEnabled(active)
        self.picture_button.setEnabled(self._picture_ready)
        for group in (self.resolution_choices, self.fps_choices, self.codec_choices,
                      *self.codec_impl_groups.values()):
            group.setEnabled(self._picture_ready)
        self.bitrate.setEnabled(self._picture_ready)
        if not active and not self._picture_ready:
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
        self._layout()

    def toggle_controls(self) -> None:
        self.release_input()
        if self.panel.isVisible():
            self.panel.close()
        else:
            anchor = self.toolbar.controls.mapToGlobal(QPoint(self.toolbar.controls.width(), TITLE_HEIGHT))
            screen = self.screen().availableGeometry()
            direction = (Qt.LayoutDirection.RightToLeft
                         if anchor.x() - screen.left() > screen.right() - anchor.x()
                         else Qt.LayoutDirection.LeftToRight)
            self.panel.setLayoutDirection(direction)
            self.panel.popup(anchor - QPoint(self.panel.sizeHint().width(), 0))

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
        if not self._closed:
            self._closed = True
            self.release_input()
            self.closed.emit()
        event.accept()
