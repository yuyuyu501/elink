from __future__ import annotations

import time

from PySide6.QtCore import QEvent, QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QFont, QImage, QPainter
from PySide6.QtWidgets import QWidget

from ..core.statistics import StreamStats, overlay_lines
from ..core.input import XInput


class Player(QWidget):
    ended = Signal()

    def __init__(self, runtime, client, *, game_mouse=True, controllers=True):
        super().__init__()
        self.runtime, self.client = runtime, client
        self.game_mouse, self.controllers = game_mouse, controllers
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
        self.setWindowTitle("Elink · F10 统计 · F11 全屏 · Ctrl+Alt+Shift+Q 断开 · Ctrl+Alt+Shift+Z 释放输入")
        self.setStyleSheet("background: #101719")
        self.resize(1280, 760)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.runtime.feedback.connect(self.rumble)
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.timeout.connect(self.tick)
        self.timer.start(8)
        self._closed = False

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
        if self.image:
            size = self.image.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
            self.rect_image = QRect((self.width() - size.width()) // 2, (self.height() - size.height()) // 2, size.width(), size.height())
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
            painter.fillRect(4, 4, width, len(lines) * line_height + 8, QColor(0, 0, 0, 105))
            painter.setPen(QColor('#ffffff'))
            for index, line in enumerate(lines):
                painter.drawText(10, 8 + painter.fontMetrics().ascent() + index * line_height, line)
        status = "输入已捕获 · Ctrl+Alt+Shift+Z 释放 · F11 全屏" if self.captured else "点击画面控制远端 · Ctrl+Alt+Shift+Q 断开"
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
        elif event.key() == Qt.Key.Key_F11:
            self.showNormal() if self.isFullScreen() else self.showFullScreen()
        elif event.key() == Qt.Key.Key_F10:
            self.show_statistics = not self.show_statistics
            self.update()
        elif event.key() == Qt.Key.Key_Z and event.modifiers() & modifiers == modifiers:
            self.release()
        elif self.captured and not event.isAutoRepeat():
            self.key(event, True)
        event.accept()

    def keyReleaseEvent(self, event):
        if self.captured and not event.isAutoRepeat() and event.key() not in (Qt.Key.Key_F10, Qt.Key.Key_F11):
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
            if self.client.pc:
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
            self.release()
            self.timer.stop()
            self.runtime.feedback.disconnect(self.rumble)
            self._closed = True
            self.ended.emit()
        event.accept()
