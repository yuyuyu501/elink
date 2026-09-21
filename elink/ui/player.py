from __future__ import annotations

import time

from PySide6.QtCore import QEvent, QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QFont, QImage, QPainter, QActionGroup
from PySide6.QtWidgets import QWidget, QFrame, QHBoxLayout, QVBoxLayout, QLabel, QPushButton, QMenu, QDialog

from .controls import StreamOptions

from ..core.statistics import StreamStats, overlay_lines
from ..core.input import XInput


class Player(QWidget):
    ended = Signal()
    reconfigure = Signal(dict)
    settings_changed = Signal(dict)

    def __init__(self, runtime, client, *, game_mouse=True, controllers=True, preferences=None):
        super().__init__()
        self.runtime, self.client = runtime, client
        self.game_mouse, self.controllers = game_mouse, controllers
        self.preferences = dict(preferences or {})
        self.muted = False
        self.control_menu = None
        self.settings_dialog = None
        self.captured = False
        self.image = None
        self.rect_image = QRect()
        self.drawn = 0
        self.fps = 0
        self.stats_at = time.monotonic()
        self.new_image = False
        self.started_at = self.stats_at
        self.elapsed = 0.0
        self.show_statistics = True
        self.pad_indices = set()
        self.xinput = XInput() if controllers else None
        self.setWindowTitle("Elink · 远程桌面")
        self.setMinimumSize(640, 400)
        self.make_toolbar()
        self.resize(1280, 760)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.runtime.feedback.connect(self.rumble)
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.timeout.connect(self.tick)
        self.timer.start(8)
        self._closed = False

    def make_toolbar(self):
        self.toolbar = QFrame(self)
        self.toolbar.setObjectName("streamToolbar")
        row = QHBoxLayout(self.toolbar)
        row.setContentsMargins(16, 4, 12, 4)
        self.stream_title = QLabel("ELINK  /  远程桌面")
        self.stream_title.setObjectName("streamTitle")
        row.addWidget(self.stream_title)
        row.addStretch()
        self.controls_button = QPushButton("控制中心  F8  ▾")
        self.controls_button.setToolTip("F8 打开控制中心并释放远端输入")
        self.controls_button.clicked.connect(self.open_controls)
        row.addWidget(self.controls_button)
        self.error_label = QLabel(self)
        self.error_label.setObjectName("errorNotice")
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.setWordWrap(True)
        self.error_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.error_label.hide()

    def resizeEvent(self, event):
        self.toolbar.setGeometry(0, 0, self.width(), 48)
        self.error_label.setGeometry(16, 56, self.width() - 32, 72)
        super().resizeEvent(event)

    def show_error(self, message):
        self.error_label.setText(str(message))
        self.error_label.show()

    def open_controls(self):
        if self._closed:
            return
        self.release()
        if self.control_menu:
            self.control_menu.close()
            self.control_menu.deleteLater()
        menu = self.control_menu = QMenu(self)
        menu.setMinimumWidth(250)
        quality = menu.addAction("画质与传输设置…")
        quality.triggered.connect(self.open_quality)
        quality.setEnabled(self.client.pc is not None)
        mouse = menu.addMenu("鼠标模式")
        group = QActionGroup(mouse)
        for label, relative in [("桌面鼠标", False), ("游戏相对鼠标", True)]:
            action = mouse.addAction(label)
            action.setCheckable(True)
            action.setChecked(self.game_mouse == relative)
            group.addAction(action)
            action.triggered.connect(lambda checked=False, relative=relative: self.set_mouse_mode(relative))
        pad = menu.addAction("转发手柄与震动")
        pad.setCheckable(True)
        pad.setChecked(self.controllers)
        pad.triggered.connect(self.set_controllers)
        sound = menu.addAction("本机静音")
        sound.setCheckable(True)
        sound.setChecked(self.muted)
        sound.triggered.connect(self.set_muted)
        menu.addSeparator()
        stats = menu.addAction("显示连接统计    F10")
        stats.setCheckable(True)
        stats.setChecked(self.show_statistics)
        stats.triggered.connect(self.set_statistics)
        menu.addAction("退出全屏    F11" if self.isFullScreen() else "全屏    F11").triggered.connect(self.toggle_fullscreen)
        menu.addAction("释放键鼠输入").triggered.connect(self.release)
        menu.addSeparator()
        menu.addAction("断开连接").triggered.connect(self.close)
        menu.popup(self.controls_button.mapToGlobal(QPoint(0, self.controls_button.height())))

    def set_mouse_mode(self, relative):
        self.release()
        self.game_mouse = relative
        self.preferences['game_mouse'] = relative
        self.settings_changed.emit({'game_mouse': relative})

    def set_controllers(self, enabled):
        self.release()  # Release remote buttons and local rumble before changing backends.
        self.controllers = enabled
        self.xinput = XInput() if enabled else None
        self.preferences['controllers'] = enabled
        self.settings_changed.emit({'controllers': enabled})

    def set_muted(self, muted):
        self.muted = muted
        self.runtime.call(self.client.set_muted, muted)

    def set_statistics(self, enabled):
        self.show_statistics = enabled
        self.update()

    def toggle_fullscreen(self):
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    def open_quality(self):
        self.release()
        if self.settings_dialog:
            self.settings_dialog.raise_()
            return
        dialog = self.settings_dialog = QDialog(self)
        dialog.setWindowTitle("画质与传输设置")
        dialog.setMinimumWidth(460)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(24, 24, 24, 24)
        options = StreamOptions()
        options.restore(self.preferences)
        layout.addWidget(options)
        note = QLabel("应用后会短暂断开并自动重连。设置同时保存为下次连接的默认值。")
        note.setWordWrap(True)
        note.setObjectName('muted')
        layout.addWidget(note)
        buttons = QHBoxLayout()
        cancel = QPushButton("取消")
        cancel.clicked.connect(dialog.reject)
        apply = QPushButton("应用并重新连接")
        apply.setObjectName('primary')
        def accept():
            values = options.values()
            dialog.accept()
            self.reconfigure.emit(values)
        apply.clicked.connect(accept)
        buttons.addWidget(cancel)
        buttons.addWidget(apply)
        layout.addLayout(buttons)
        def finished(_):
            self.settings_dialog = None
            dialog.deleteLater()
        dialog.finished.connect(finished)
        dialog.open()

    def send(self, event):
        self.runtime.call(self.client.send, event)

    def capture(self):
        self.captured = True
        self.setFocus()
        self.runtime.call(self.client.focus, True)
        self.setCursor(Qt.CursorShape.BlankCursor)
        if self.game_mouse:
            self.grabMouse()
            QCursor.setPos(self.mapToGlobal(self.rect().center()))

    def release(self):
        self.captured = False
        self.runtime.call(self.client.focus, False)
        self.releaseMouse()
        self.unsetCursor()
        if self.xinput:
            for index in self.pad_indices:
                self.xinput.rumble(index)
        self.pad_indices.clear()

    def tick(self):
        pixels = self.client.mailbox.take()
        if pixels is not None:
            self.image = QImage(pixels.data, pixels.shape[1], pixels.shape[0], pixels.strides[0], QImage.Format.Format_RGB888).copy()
            self.new_image = True
        now = time.monotonic()
        if self.client.pc is not None:
            self.elapsed = now - self.started_at
        if now - self.stats_at >= 1:
            self.fps = self.drawn / (now - self.stats_at)
            self.drawn, self.stats_at = 0, now
        if self.captured and self.xinput and self.isActiveWindow():
            for index in range(4):
                values = self.xinput.read(index)
                if values is not None:
                    self.pad_indices.add(index)
                    self.send({"type": "pad", "index": index, "values": values})
                elif index in self.pad_indices:
                    self.send({"type": "pad", "index": index, "values": [0] * 7})
                    self.pad_indices.discard(index)
        self.update()

    def rumble(self, event):
        if self.captured and self.xinput and not self._closed:
            self.xinput.rumble(event["index"], event["left"], event["right"])

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#101719"))
        viewport = self.rect().adjusted(0, 48, 0, 0)
        if self.image:
            size = self.image.size().scaled(viewport.size(), Qt.AspectRatioMode.KeepAspectRatio)
            self.rect_image = QRect((self.width() - size.width()) // 2, 48 + (viewport.height() - size.height()) // 2, size.width(), size.height())
            painter.drawImage(self.rect_image, self.image)
            if self.new_image:
                self.drawn += 1
                self.new_image = False
        if self.show_statistics:
            snapshot = getattr(self.client, "stats", StreamStats())
            now = time.monotonic()
            fresh = now - snapshot.sampled_at < 3
            rtt = self.client.rtt_ms if self.client.rtt_ms > 0 and now - self.client.last_pong < 3 else None
            lines = overlay_lines(snapshot, self.fps, self.elapsed, rtt, self.client.pc is not None, fresh)
            font = QFont('Consolas')
            font.setPixelSize(12)
            painter.setFont(font)
            line_height = painter.fontMetrics().height() + 2
            width = max(painter.fontMetrics().horizontalAdvance(line) for line in lines) + 14
            painter.fillRect(4, 52, width, len(lines) * line_height + 8, QColor(0, 0, 0, 105))
            painter.setPen(QColor('#ffffff'))
            for index, line in enumerate(lines):
                painter.drawText(10, 56 + painter.fontMetrics().ascent() + index * line_height, line)
        status = "输入已捕获 · Ctrl+Alt+Shift+Z 释放 · F11 全屏" if self.captured else "点击画面控制 · F8 控制中心 / 释放输入 · Ctrl+Alt+Shift+Q 断开"
        if self.client.pc is None:
            status = "会话已断开，请关闭窗口后重新连接。"
        if not self.captured or self.client.pc is None:
            painter.fillRect(8, self.height() - 36, min(630, self.width() - 16), 28, QColor(0, 0, 0, 160))
            painter.setPen(QColor('#ffffff'))
            painter.drawText(18, self.height() - 17, status)

    def keyPressEvent(self, event):
        if event.isAutoRepeat():
            event.accept()
            return
        modifiers = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.ShiftModifier
        if event.key() == Qt.Key.Key_Q and event.modifiers() & modifiers == modifiers:
            self.close()
        elif event.key() == Qt.Key.Key_F8:
            self.open_controls()
        elif event.key() == Qt.Key.Key_F11:
            self.toggle_fullscreen()
        elif event.key() == Qt.Key.Key_F10:
            self.show_statistics = not self.show_statistics
            self.update()
        elif event.key() == Qt.Key.Key_Z and event.modifiers() & modifiers == modifiers:
            self.release()
        elif self.captured and not event.isAutoRepeat():
            self.key(event, True)
        event.accept()

    def keyReleaseEvent(self, event):
        if self.captured and not event.isAutoRepeat() and event.key() not in (Qt.Key.Key_F8, Qt.Key.Key_F10, Qt.Key.Key_F11):
            self.key(event, False)
        event.accept()

    def key(self, event, down):
        scan = event.nativeScanCode()
        self.send({"type": "key", "vk": event.nativeVirtualKey(), "scan": scan & 255,
                   "extended": bool(scan & 0x100), "down": down})

    def mouseMoveEvent(self, event):
        if not self.captured:
            return
        if self.game_mouse:
            center = self.rect().center()
            delta = event.position().toPoint() - center
            if not delta.isNull():
                self.send({"type": "move", "absolute": False, "x": delta.x(), "y": delta.y()})
                QCursor.setPos(self.mapToGlobal(center))
        elif not self.rect_image.isEmpty():
            rect = self.rect_image
            x = max(0, min(65535, int((event.position().x() - rect.x()) * 65535 / max(1, rect.width() - 1))))
            y = max(0, min(65535, int((event.position().y() - rect.y()) * 65535 / max(1, rect.height() - 1))))
            self.send({"type": "move", "absolute": True, "x": x, "y": y})

    def mousePressEvent(self, event):
        if not self.captured:
            if self.client.pc and event.position().y() >= 48 and self.image is not None:
                self.capture()
            return
        self.mouse_button(event, True)

    def mouseReleaseEvent(self, event):
        if self.captured:
            self.mouse_button(event, False)

    def mouse_button(self, event, down):
        name = {Qt.MouseButton.LeftButton: "left", Qt.MouseButton.RightButton: "right", Qt.MouseButton.MiddleButton: "middle"}.get(event.button())
        if name:
            self.send({"type": "button", "button": name, "down": down})

    def wheelEvent(self, event):
        if self.captured:
            self.send({"type": "wheel", "delta": event.angleDelta().y()})

    def event(self, event):
        if event.type() in (QEvent.Type.WindowDeactivate, QEvent.Type.FocusOut) and getattr(self, "captured", False):
            self.release()
        return super().event(event)

    def closeEvent(self, event):
        if not self._closed:
            if self.control_menu:
                self.control_menu.close()
            if self.settings_dialog:
                self.settings_dialog.reject()
            self.release()
            self.timer.stop()
            self.runtime.feedback.disconnect(self.rumble)
            self._closed = True
            self.ended.emit()
        event.accept()
