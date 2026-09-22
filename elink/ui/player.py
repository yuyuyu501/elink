from __future__ import annotations

import time

from PySide6.QtCore import QEvent, QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QFont, QImage, QPainter, QActionGroup
from PySide6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton, QMenu, QDialog

from .controls import StreamOptions
from .chrome import TITLE_HEIGHT, StreamTitleBar, make_resize_handles, place_resize_handles

from ..core.statistics import StreamStats, overlay_lines
from ..core.input import XInput
from ..core.mouse import MOUSE_MODES, mouse_mode, relative_mouse


class Player(QWidget):
    ended = Signal()
    reconfigure = Signal(dict)
    settings_changed = Signal(dict)

    def __init__(self, runtime, client, *, game_mouse=None, controllers=True, preferences=None):
        super().__init__()
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.was_maximized = False
        self.runtime, self.client = runtime, client
        self.controllers = controllers
        self.preferences = dict(preferences or {})
        self.mouse_mode = mouse_mode(self.preferences.get('mouse_mode', 'remote' if game_mouse else 'smart'))
        self.game_mouse = False  # Effective input mapping; independent of cursor rendering.
        self.local_cursor = False
        if hasattr(client, 'set_cursor_mode'):
            self.runtime.call(client.set_cursor_mode, self.mouse_mode)
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
        self.toolbar = StreamTitleBar(self)
        self.stream_title = self.toolbar.title
        self.controls_button = self.toolbar.controls
        self.resize_handles = make_resize_handles(self)
        self.error_label = QLabel(self)
        self.error_label.setObjectName("errorNotice")
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.setWordWrap(True)
        self.error_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.error_label.hide()

    def video_viewport(self):
        return self.rect().adjusted(0, 0 if self.isFullScreen() else TITLE_HEIGHT, 0, 0)

    def layout_chrome(self):
        self.toolbar.setGeometry(0, 0, self.width(), TITLE_HEIGHT)
        self.toolbar.setVisible(not self.isFullScreen() or not self.captured)
        self.toolbar.update_state()
        top = TITLE_HEIGHT if self.toolbar.isVisible() else 0
        self.error_label.setGeometry(16, top + 8, self.width() - 32, 72)
        place_resize_handles(self)
        self.update()

    def resizeEvent(self, event):
        self.layout_chrome()
        super().resizeEvent(event)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange and hasattr(self, 'toolbar'):
            self.release()
            self.layout_chrome()

    def showEvent(self, event):
        super().showEvent(event)
        self.layout_chrome()

    def minimize_window(self):
        self.release()
        self.showMinimized()

    def toggle_maximized(self):
        self.release()
        if self.isFullScreen():
            self.toggle_fullscreen()
        elif self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

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
        mouse = self.mouse_menu = menu.addMenu("鼠标模式")
        group = QActionGroup(mouse)
        mouse.setToolTipsVisible(True)
        for label, mode, hint in MOUSE_MODES:
            action = mouse.addAction(label)
            action.setCheckable(True)
            action.setChecked(self.mouse_mode == mode)
            supported = getattr(self.client, 'cursor_supported', False)
            action.setEnabled(supported or mode == 'remote')
            action.setToolTip(hint if supported else '请将被控端更新至 0.4.7 或更高版本以使用完整鼠标模式。')
            group.addAction(action)
            action.triggered.connect(lambda checked=False, mode=mode: self.set_mouse_mode(mode))
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

    def set_mouse_mode(self, mode):
        self.release()
        self.mouse_mode = mouse_mode(mode)
        self.preferences['mouse_mode'] = self.mouse_mode
        if hasattr(self.client, 'set_cursor_mode'):
            self.runtime.call(self.client.set_cursor_mode, self.mouse_mode)
        self.settings_changed.emit({'mouse_mode': self.mouse_mode})

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
        self.release()
        if self.isFullScreen():
            self.showMaximized() if self.was_maximized else self.showNormal()
        else:
            self.was_maximized = self.isMaximized()
            self.showFullScreen()
        self.layout_chrome()

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
        options = StreamOptions(remote_display=True)
        options.restore(self.preferences)
        layout.addWidget(options)
        note = QLabel("正在读取被控端显示设置…")
        note.setTextFormat(Qt.TextFormat.PlainText)
        note.setWordWrap(True)
        note.setObjectName('muted')
        layout.addWidget(note)
        buttons = QHBoxLayout()
        cancel = QPushButton("取消")
        cancel.clicked.connect(dialog.reject)
        apply = QPushButton("应用并重新连接")
        apply.setObjectName('primary')
        apply.setEnabled(False)
        hint = ('分辨率与缩放会实际修改被控电脑，断开后保留；部分程序需重新打开才适应新缩放。'
                '\n传输画面跟随被控端比例，当前最高 1080p。应用后短暂断开并自动重连。')
        def loaded(snapshot):
            if self._closed or self.settings_dialog is not dialog:
                return
            options.set_display(snapshot)
            note.setText(hint + ('\n' + snapshot['scale_error'] if snapshot.get('scale_error') else ''))
            apply.setEnabled(True)
        def failed(message):
            if self._closed or self.settings_dialog is not dialog:
                return
            options.display_unavailable(message)
            note.setText(str(message) + '\n仍可调整帧率、码率等传输设置。')
            apply.setEnabled(True)
        self.runtime.submit(self.client.display_settings(), loaded, failed)
        def accept():
            values = options.values()
            change = options.display_change()
            def finish(_=None):
                if self._closed or self.settings_dialog is not dialog:
                    return
                dialog.setEnabled(True)
                dialog.accept()
                self.reconfigure.emit(values)
            if change is None:
                finish()
                return
            dialog.setEnabled(False)
            note.setText('正在修改被控端显示设置…')
            def rejected(message):
                if self._closed or self.settings_dialog is not dialog:
                    return
                dialog.setEnabled(True)
                options.display_unavailable(str(message))
                options.display_snapshot = None
                note.setText(str(message) + '\n请关闭并重新打开此窗口，读取当前显示状态。')
            self.runtime.submit(self.client.display_settings(change), finish, rejected)
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
        self.setFocus()
        self.captured = True
        self.layout_chrome()
        self.runtime.call(self.client.focus, True)
        self.update_mouse(force=True)

    def update_mouse(self, force=False):
        if not self.captured:
            return
        fresh = time.monotonic() - getattr(self.client, 'cursor_updated', 0) < 2
        visible = getattr(self.client, 'cursor_visible', None) if fresh else None
        supported = getattr(self.client, 'cursor_supported', False)
        relative = relative_mouse(self.mouse_mode, visible)
        if not supported and self.mouse_mode == 'smart':
            relative = False  # Older hosts cannot report whether games hide their cursor.
        local = (supported and self.mouse_mode == 'local'
                 and getattr(self.client, 'cursor_applied', 'remote') == 'local' and fresh)
        if force or relative != self.game_mouse:
            if relative:
                self.grabMouse()
                QCursor.setPos(self.mapToGlobal(self.rect().center()))
            else:
                self.releaseMouse()
            self.game_mouse = relative
        if force or local != self.local_cursor:
            self.setCursor(Qt.CursorShape.ArrowCursor if local else Qt.CursorShape.BlankCursor)
            self.local_cursor = local

    def release(self):
        self.captured = False
        self.runtime.call(self.client.focus, False)
        self.releaseMouse()
        self.unsetCursor()
        if self.xinput:
            for index in self.pad_indices:
                self.xinput.rumble(index)
        self.pad_indices.clear()
        self.layout_chrome()

    def tick(self):
        self.update_mouse()
        pixels = self.client.mailbox.take()
        if pixels is not None:
            self.image = QImage(pixels.data, pixels.shape[1], pixels.shape[0], pixels.strides[0], QImage.Format.Format_RGB888).copy()
            self.new_image = True
        now = time.monotonic()
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
        viewport = self.video_viewport()
        if self.image:
            size = self.image.size().scaled(viewport.size(), Qt.AspectRatioMode.KeepAspectRatio)
            self.rect_image = QRect((self.width() - size.width()) // 2, viewport.top() + (viewport.height() - size.height()) // 2, size.width(), size.height())
            painter.drawImage(self.rect_image, self.image)
            if self.new_image:
                self.drawn += 1
                self.new_image = False
        if self.show_statistics:
            snapshot = getattr(self.client, "stats", StreamStats())
            now = time.monotonic()
            fresh = now - snapshot.sampled_at < 3
            rtt = self.client.rtt_ms if self.client.rtt_ms > 0 and now - self.client.last_pong < 3 else None
            lines = overlay_lines(snapshot, self.fps, rtt, self.client.pc is not None, fresh)
            font = QFont('Consolas')
            font.setPixelSize(12)
            painter.setFont(font)
            line_height = painter.fontMetrics().height() + 2
            width = max(painter.fontMetrics().horizontalAdvance(line) for line in lines) + 14
            stats_top = TITLE_HEIGHT if self.toolbar.isVisible() else 0
            painter.fillRect(4, stats_top + 4, width, len(lines) * line_height + 8, QColor(0, 0, 0, 105))
            painter.setPen(QColor('#ffffff'))
            for index, line in enumerate(lines):
                painter.drawText(10, stats_top + 8 + painter.fontMetrics().ascent() + index * line_height, line)
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
            if self.client.pc and self.video_viewport().contains(event.position().toPoint()) and self.image is not None:
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
