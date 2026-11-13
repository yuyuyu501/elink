"""Independent remote desktop window and local session controls."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFormLayout, QFrame,
    QHBoxLayout, QPushButton, QScrollArea, QSizePolicy, QSlider, QToolButton, QVBoxLayout, QWidget,
)

from backend.input_capture.mouse import MOUSE_MODES
from backend.media.video.codec.probe import VIDEO_ENCODERS, codec_label, compatible_choices
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
    def __init__(self, picture: dict, parent=None, *, connection=None) -> None:
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
        self.encoder = QComboBox()
        self.decoder = QComboBox()
        for box in (self.codec, self.encoder, self.decoder):
            box.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.codec_status = label("")
        if connection is not None and connection._codec_enabled:
            form.addRow("视频格式", self.codec)
            form.addRow("被控端编码器", self.encoder)
            form.addRow("本机解码器", self.decoder)
            form.addRow("", self.codec_status)
            self.codec.currentIndexChanged.connect(self._backend_options)
            self.encoder.currentIndexChanged.connect(self._decoder_options)
            connection.capabilities_changed.connect(self._update_codecs)
            connection.changed.connect(self._connection_state)
            self._update_codecs()
            options = connection._preferences if connection.phase == "selection" else connection._applied_options
            for key in ("codec", "encoder", "decoder"):
                box = getattr(self, key)
                index = box.findData(options[key])
                if index >= 0:
                    box.setCurrentIndex(index)
            self.codec_status.setText(
                "" if all(getattr(self, k).findData(v) >= 0 for k, v in options.items())
                else "保存的选项当前不可用，请重新选择")
        self.apply_button = QToolButton()
        self.apply_button.setIcon(icons.icon("check"))
        self.apply_button.setToolTip("应用画面设置")
        self.apply_button.clicked.connect(self.accept)
        controls = QWidget()
        row = QHBoxLayout(controls)
        row.setContentsMargins(0, 0, 0, 0)
        if connection is not None and connection._codec_enabled:
            self.refresh_button = QToolButton()
            self.refresh_button.setIcon(icons.icon("refresh-cw"))
            self.refresh_button.setToolTip("重新检测双方编解码能力")
            self.refresh_button.clicked.connect(connection.refresh_codecs)
            row.addWidget(self.refresh_button)
        row.addStretch()
        row.addWidget(self.apply_button)
        form.addRow("", controls)
        self._connection_state()

    @staticmethod
    def _fill(box, names, labels=None):
        selected = box.currentData()
        box.blockSignals(True)
        box.clear()
        box.addItem("自动", "auto")
        for name in names:
            box.addItem(labels[name] if labels else codec_label(name), name)
        index = box.findData(selected)
        box.setCurrentIndex(max(0, index))
        box.blockSignals(False)

    def _update_codecs(self):
        remote, local = self.connection.codec_capabilities
        if remote is None or local is None:
            self.codec_status.setText("正在检测编解码能力…")
            return
        try:
            choices = compatible_choices(remote, local, {})
        except ValueError:
            choices = []
        names = list(dict.fromkeys(choice["codec"] for choice in choices))
        self._fill(self.codec, names, {"h264": "H.264", "hevc": "H.265", "av1": "AV1"})
        self._backend_options()
        self.codec_status.setText("" if names else "双方没有共同可用的视频格式")
        self._connection_state()

    def _backend_options(self, *_):
        if self.connection is None:
            return
        remote, local = self.connection.codec_capabilities
        if remote is None or local is None:
            return
        codec = self.codec.currentData()
        dec_formats = {v["codec"] for v in local["decoders"].values()}
        self._fill(self.encoder, [n for n, v in remote["encoders"].items()
                                 if v["codec"] in dec_formats and codec in ("auto", v["codec"])])
        self._decoder_options()

    def _decoder_options(self, *_):
        if self.connection is None:
            return
        remote, local = self.connection.codec_capabilities
        if remote is None or local is None:
            return
        codec = self.codec.currentData()
        if codec == "auto" and self.encoder.currentData() != "auto":
            codec = VIDEO_ENCODERS.get(self.encoder.currentData(), "auto")
        enc_formats = {v["codec"] for v in remote["encoders"].values()}
        self._fill(self.decoder, [n for n, v in local["decoders"].items()
                                 if v["codec"] in enc_formats and codec in ("auto", v["codec"])])

    def _connection_state(self):
        if self.connection is not None and hasattr(self, "apply_button"):
            active = self.connection.phase in {"active", "selection"}
            self.apply_button.setEnabled(active)
            for box in (self.codec, self.encoder, self.decoder):
                box.setEnabled(active)
            if hasattr(self, "refresh_button"):
                self.refresh_button.setEnabled(self.connection.phase == "active")

    def picture(self) -> dict:
        width, height = map(int, self.resolution.currentText().split("×"))
        result = {"width": width, "height": height, "fps": self.fps.currentData(),
                  "bitrate_kbps": self.bitrate.value() * 1000}
        if self.connection is not None and self.connection._codec_enabled:
            result.update({name: getattr(self, name).currentData() for name in ("codec", "encoder", "decoder")})
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
    watch_toggled = Signal(bool)
    snapshot_requested = Signal()
    report_requested = Signal()
    preference_changed = Signal(str, object)
    upscale_refresh = Signal(bool)
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
        self.preferences = {}
        for name, text in (("clipboard_enabled", "同步剪贴板"), ("gamepad_enabled", "转发手柄"),
                           ("sr_enabled", "超分辨率")):
            check = QCheckBox(text)
            check.toggled.connect(lambda checked, name=name: self.preference_changed.emit(name, checked))
            self.preferences[name] = check
            controls.addWidget(check)
        sr = QHBoxLayout()
        self.sr_mode = QComboBox()
        self.sr_mode.addItem("FSR", "fsr")
        self.sr_mode.addItem("RTX", "rtx")
        self.sr_refresh = QToolButton()
        self.sr_refresh.setIcon(icons.icon("refresh-cw"))
        self.sr_refresh.setToolTip("重新检测本机超分能力")
        self.sr_refresh.clicked.connect(lambda: self.upscale_refresh.emit(True))
        sr.addWidget(self.sr_mode, 1)
        sr.addWidget(self.sr_refresh)
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
        self._sr_timer = QTimer(self)
        self._sr_timer.setInterval(30000)
        self._sr_timer.timeout.connect(
            lambda: self.upscale_refresh.emit(False) if self.panel.isVisible() else None)
        self._sr_timer.start()
        rows.addWidget(self.session_controls)
        self.stats_button = QCheckBox("性能叠加")
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
        widgets = [self.watch_button, self.mouse_mode, self.sensitivity, self.sr_mode,
                   self.sr_percent, self.sr_sharpen, self.sr_sharpness,
                   *self.preferences.values()]
        for widget in widgets:
            widget.blockSignals(True)
        self._control_enabled = settings.control_enabled
        self.watch_button.setChecked(not settings.control_enabled)
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
        self.sr_refresh.setEnabled(not (settings.sr_enabled and self._active))
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

    def set_session_active(self, active: bool) -> None:
        self._active = active
        self.session_controls.setEnabled(active)
        self.sr_refresh.setEnabled(not (active and self.preferences["sr_enabled"].isChecked()))
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
        self.panel.setVisible(not self.panel.isVisible())
        if self.panel.isVisible():
            self.upscale_refresh.emit(False)
        else:
            self.upscale_cancel.emit()
        self._layout()

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
        self.preview.stats_right = 324 if self.panel.isVisible() else 0
        self.preview.update()
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
        self._sr_timer.stop()
        self.upscale_cancel.emit()
        if not self._closed:
            self._closed = True
            self.release_input()
            self.closed.emit()
        event.accept()
