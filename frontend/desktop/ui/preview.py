"""画面预览控件：把后端 ``Frame``（BGRA）画成等比缩放的视频区。

输入坐标与绘制共用画面矩形，会话收发通过回调交给服务层。
"""

from __future__ import annotations

import sys

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QImage,
    QCursor,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)
from PySide6.QtWidgets import QApplication, QFrame, QSizePolicy, QVBoxLayout, QWidget

from backend.control_channel.protocol import EventType, InputEvent
from backend.input_capture.windows.raw_mouse import CursorConfinement, RawMouseInput
from backend.input_capture.mouse import MouseSensitivity, mouse_mode, relative_mouse
from backend.media.frame import Frame

PLACEHOLDER_TITLE = "等待画面"
PLACEHOLDER_HINT = "正在等待远端画面"
HUD_MARGIN = 14


class PreviewWidget(QWidget):
    """等比缩放显示帧，并叠加状态与 HUD。"""

    capture_changed = Signal(bool)
    input_error = Signal(object)
    presented = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(QSize(320, 180))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.captured = False
        self.control_enabled = False
        self.mouse_mode = "smart"
        self._cursor_visible = None
        self._relative = False
        self._mouse_confirmed = True
        self._sensitivity = MouseSensitivity()
        self._absolute_last = self._absolute_position = None
        self._software_position = None
        self._warp_position = None
        self._pixels = None
        self._paint_pending = False
        self._frame_pts = 0
        self._cursor_boundary = None
        self._send_input = None
        self._set_input_focus = None
        self._keyboard_hook = None
        self._raw_mouse = RawMouseInput()
        self._confinement = CursorConfinement()
        self._input_timer = QTimer(self)
        self._input_timer.setInterval(8)
        self._input_timer.timeout.connect(self._poll_keyboard)

        self._image: QImage | None = None
        self._info = ""
        self._badge = ""
        self._stats_lines: list[str] = []
        self._stats_visible = False
        self._edge_padding = 12
        self._frames = 0
        self._source_size = QSize(0, 0)

    # ---- 数据 ----
    def set_frame(self, frame: Frame) -> None:
        if frame.width <= 0 or frame.height <= 0:
            return
        payload = bytes(frame.data)
        image = QImage(
            payload,
            frame.width,
            frame.height,
            frame.linesize or frame.width * 4,
            QImage.Format.Format_ARGB32,
        )
        self._image = image
        self._pixels = payload
        self._frame_pts = frame.pts
        if self._cursor_boundary is not None and frame.pts >= self._cursor_boundary:
            self._cursor_boundary = None
        self._paint_pending = True
        self._source_size = QSize(frame.width, frame.height)
        self._frames += 1
        self._info = f"{frame.width}×{frame.height} · 第 {self._frames} 帧"
        self.update()

    def clear(self) -> None:
        self.release_input()
        self._image = None
        self._pixels = None
        self._paint_pending = False
        self._cursor_boundary = None
        self._info = ""
        self._frames = 0
        self._source_size = QSize(0, 0)
        self.update()

    def set_info(self, text: str) -> None:
        self._info = text
        self.update()

    def set_badge(self, text: str) -> None:
        self._badge = text
        self.update()

    def set_stats_lines(self, lines: list[str]) -> None:
        self._stats_lines = [str(line) for line in lines if line]
        self.update()

    def set_stats_visible(self, visible: bool) -> None:
        self._stats_visible = bool(visible)
        self.update()

    def set_edge_padding(self, padding: int) -> None:
        self._edge_padding = max(0, int(padding))
        self.update()

    @property
    def stats_visible(self) -> bool:
        return self._stats_visible

    @property
    def has_frame(self) -> bool:
        return self._image is not None

    @property
    def source_size(self) -> QSize:
        return self._source_size

    def save_snapshot(self, path: str) -> bool:
        """保存当前画面（截图按钮用）。"""
        if self._image is None:
            return False
        return bool(self._image.save(path, "PNG"))

    def bind_input(self, send, focus) -> None:
        self._send_input, self._set_input_focus = send, focus

    def set_control_enabled(self, enabled: bool) -> None:
        self.control_enabled = enabled
        if not enabled:
            self.release_input()

    def set_mouse_mode(self, mode: str) -> None:
        mode = mouse_mode(mode)
        if mode == self.mouse_mode:
            return
        self.release_input()
        self.mouse_mode = mode
        self._relative = relative_mouse(mode, self._cursor_visible)

    def set_sensitivity(self, value) -> None:
        self._sensitivity = MouseSensitivity(value)
        self._absolute_last = self._absolute_position = None

    def request_mouse_mode(self) -> None:
        self.release_input()
        self._mouse_confirmed = False
        self._cursor_boundary = None

    def set_cursor_state(self, state: dict) -> None:
        if state.get("mode") != self.mouse_mode:
            return
        visible = state.get("visible")
        if isinstance(visible, bool):
            self._cursor_visible = visible
        if not self._mouse_confirmed:
            boundary = state.get("pts")
            if isinstance(boundary, int) and boundary > self._frame_pts:
                self._cursor_boundary = boundary
        self._mouse_confirmed = True
        relative = relative_mouse(self.mouse_mode, self._cursor_visible)
        if relative != self._relative:
            captured = self.captured
            self.release_input()
            self._relative = relative
            if captured:
                self.capture_input()
        self.update()

    def video_contains(self, position) -> bool:
        rect = self._target_rect()
        return (not rect.isEmpty() and rect.x() <= position.x() < rect.x() + rect.width()
                and rect.y() <= position.y() < rect.y() + rect.height())

    def normalized_position(self, position) -> tuple[int, int] | None:
        rect = self._target_rect()
        if rect.isEmpty():
            return None
        x = max(0.0, min(rect.width() - 1, position.x() - rect.x()))
        y = max(0.0, min(rect.height() - 1, position.y() - rect.y()))
        return round(x * 65535 / max(1, rect.width() - 1)), round(y * 65535 / max(1, rect.height() - 1))

    def capture_input(self) -> bool:
        if self.captured:
            return True
        if (self._cursor_boundary is not None or not self._mouse_confirmed or not self.control_enabled or not self.has_frame or not self.window().isActiveWindow()
                or QApplication.activeModalWidget() or QApplication.activePopupWidget()
                or self._set_input_focus is None):
            return False
        self.setFocus()
        if not self._set_input_focus(True):
            return False
        self.captured = True
        self._absolute_last = self._absolute_position = None
        self._software_position = self.mapFromGlobal(QCursor.pos())
        self.setCursor(Qt.CursorShape.BlankCursor)
        self._input_timer.start()
        try:
            if sys.platform == "win32" and QApplication.platformName() == "windows":
                from backend.input_capture.windows.send_input_hook import SendInputHookCapture
                hook = SendInputHookCapture(keyboard_only=True, active=lambda: self.captured)
                try:
                    hook.open()
                    self._keyboard_hook = hook
                    self._input_timer.start()
                except Exception as exc:
                    # Qt continues to supply ordinary keys if the shell hook is unavailable.
                    self.input_error.emit(exc)
                if self._relative:
                    self.grabMouse(QCursor(Qt.CursorShape.BlankCursor))
                    self._raw_mouse.start(int(self.winId()))
                    self._confine_cursor()
        except Exception as exc:
            self.release_input()
            self.input_error.emit(exc)
            return False
        self.capture_changed.emit(True)
        return True

    def release_input(self) -> None:
        was_captured = self.captured
        self.captured = False
        self._input_timer.stop()
        hook, self._keyboard_hook = self._keyboard_hook, None
        if hook is not None:
            hook.close()
        self._raw_mouse.stop()
        self._confinement.release()
        if QWidget.mouseGrabber() is self:
            self.releaseMouse()
        self.unsetCursor()
        self._sensitivity.remainder = [0.0, 0.0]
        self._absolute_last = self._absolute_position = None
        self._software_position = self._warp_position = None
        self.update()
        if was_captured:
            if self._set_input_focus is not None:
                self._set_input_focus(False)
            self.capture_changed.emit(False)

    def _poll_keyboard(self) -> None:
        if self._keyboard_hook is not None:
            for event in self._keyboard_hook.read_all():
                self._send(event)

    def _send(self, event: InputEvent) -> None:
        if self.captured and self._send_input is not None:
            if not self._send_input(event):
                self.release_input()

    def _absolute_motion(self, position) -> None:
        position = QPointF(position)
        if self._warp_position is not None and (position - self._warp_position).manhattanLength() < 1.5:
            self._warp_position = None
            return
        if self._absolute_last is not None:
            position = self._absolute_position + (position - self._absolute_last) * self._sensitivity.factor
        rect = self._target_rect()
        position = QPointF(max(rect.left(), min(rect.right(), position.x())),
                          max(rect.top(), min(rect.bottom(), position.y())))
        self._absolute_position = position
        self._absolute_last = QPointF(position.toPoint()) if self._sensitivity.factor != 1 else position
        self._software_position = position
        if self._sensitivity.factor != 1 and QApplication.platformName() == "windows":
            self._warp_position = QPointF(position.toPoint())
            QCursor.setPos(self.mapToGlobal(position.toPoint()))
        point = self.normalized_position(position)
        if point is not None:
            self._send(InputEvent(EventType.MOUSE_MOVE, x=point[0], y=point[1],
                                  extra={"coordinates": "normalized"}))
        self.update()

    def mouseMoveEvent(self, event) -> None:
        if self.captured and not self._relative:
            self._absolute_motion(event.position())
            if not self.video_contains(event.position()):
                self.release_input()
        event.accept()

    def mousePressEvent(self, event) -> None:
        if self.video_contains(event.position()) and self.capture_input():
            self._mouse_button(event, True)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if self.captured:
            self._mouse_button(event, False)
        event.accept()

    def _mouse_button(self, event, pressed: bool) -> None:
        button = {Qt.MouseButton.LeftButton: 1, Qt.MouseButton.RightButton: 2,
                  Qt.MouseButton.MiddleButton: 3}.get(event.button())
        if button is not None:
            if not self._relative:
                self._absolute_motion(event.position())
            self._send(InputEvent(EventType.MOUSE_BUTTON, button=button, pressed=pressed,
                                  extra={"positioned": True}))

    def wheelEvent(self, event) -> None:
        if self.captured and self.video_contains(event.position()):
            if not self._relative:
                self._absolute_motion(event.position())
            self._send(InputEvent(EventType.MOUSE_WHEEL, delta=event.angleDelta().y()))
        event.accept()

    def _key(self, event, pressed: bool) -> None:
        if self.captured and not event.isAutoRepeat():
            vk = int(event.nativeVirtualKey() or 0)
            if not vk:
                vk = {Qt.Key.Key_Tab: 9, Qt.Key.Key_Return: 13, Qt.Key.Key_Escape: 27,
                      Qt.Key.Key_Backspace: 8, Qt.Key.Key_Shift: 16, Qt.Key.Key_Control: 17,
                      Qt.Key.Key_Alt: 18, Qt.Key.Key_Left: 37, Qt.Key.Key_Up: 38,
                      Qt.Key.Key_Right: 39, Qt.Key.Key_Down: 40}.get(event.key(), int(event.key()))
            scan = int(event.nativeScanCode() or 0)
            if 0 < vk <= 255 and vk not in (0x77, 0x79, 0x7A):
                self._send(InputEvent(EventType.KEY, key_code=vk, scan_code=scan & 255,
                                      pressed=pressed, extra={"extended": bool(scan & 0x100)}))
        event.accept()

    def keyPressEvent(self, event) -> None:
        self._key(event, True)

    def keyReleaseEvent(self, event) -> None:
        self._key(event, False)

    def event(self, event) -> bool:
        if getattr(self, "captured", False):
            if event.type() in (QEvent.Type.FocusOut, QEvent.Type.WindowDeactivate):
                self.release_input()
            elif event.type() in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease) and event.key() == Qt.Key.Key_Tab:
                self._key(event, event.type() == QEvent.Type.KeyPress)
                return True
        return super().event(event)

    def leaveEvent(self, event) -> None:
        if not self._relative:
            self.release_input()
        super().leaveEvent(event)

    def _confine_cursor(self) -> None:
        self._confinement.confine(int(self.winId()), self._target_rect().getRect(),
                                 (self.width(), self.height()))

    def resizeEvent(self, event) -> None:
        if self.captured and self._relative:
            try:
                self._confine_cursor()
            except Exception as exc:
                self.release_input()
                self.input_error.emit(exc)
        super().resizeEvent(event)

    def nativeEvent(self, event_type, message):
        if self.captured and self._relative:
            delta = self._raw_mouse.delta(message)
            if delta:
                delta = self._sensitivity.relative(*delta)
                self._send(InputEvent(EventType.MOUSE_MOVE,
                                      x=delta[0], y=delta[1],
                                      extra={"coordinates": "relative"}))
        return super().nativeEvent(event_type, message)

    # ---- 绘制 ----
    def _target_rect(self) -> QRect:
        if self._image is None:
            return QRect()
        pad = self._edge_padding
        area = self.rect().adjusted(pad, pad, -pad, -pad)
        image_ratio = self._image.width() / max(1, self._image.height())
        area_ratio = area.width() / max(1, area.height())
        if image_ratio > area_ratio:
            width = area.width()
            height = int(width / image_ratio)
        else:
            height = area.height()
            width = int(height * image_ratio)
        left = area.left() + (area.width() - width) // 2
        top = area.top() + (area.height() - height) // 2
        return QRect(left, top, width, height)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), QColor("#0B1220"))

        if self._image is None:
            self._paint_placeholder(painter)
            self._paint_badges(painter, self.rect().adjusted(6, 6, -6, -6), with_info=False)
            return

        target = self._target_rect()
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        path = QPainterPath()
        path.addRoundedRect(QRectF(target), 8, 8)
        painter.save()
        painter.setClipPath(path)
        painter.drawImage(target, self._image)
        painter.restore()
        if self._paint_pending:
            self._paint_pending = False
            self.presented.emit()
        if self.captured and self.mouse_mode == "local" and not self._relative and self._software_position is not None:
            pointer = QPainterPath()
            point = self._software_position
            pointer.moveTo(point)
            pointer.lineTo(point + QPointF(0, 18))
            pointer.lineTo(point + QPointF(5, 13))
            pointer.lineTo(point + QPointF(10, 21))
            pointer.lineTo(point + QPointF(13, 19))
            pointer.lineTo(point + QPointF(8, 11))
            pointer.lineTo(point + QPointF(15, 11))
            pointer.closeSubpath()
            painter.setPen(QPen(QColor("black"), 1))
            painter.fillPath(pointer, QColor("white"))
            painter.drawPath(pointer)

        self._paint_badges(painter, target, with_info=True)
        if self._stats_visible and self._stats_lines:
            self._paint_stats(painter, target)

    def _paint_stats(self, painter: QPainter, target: QRect) -> None:
        target = target.adjusted(0, max(0, getattr(self, "stats_top", 0) - target.top()),
                                 -getattr(self, "stats_right", 0), 0)
        font = QFont("Cascadia Mono")
        font.setPointSizeF(9)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        available = max(0, target.width() - 24)
        parts = []
        for part in self._stats_lines:
            if len(part) > 28:
                part = metrics.elidedText(part, Qt.TextElideMode.ElideRight, 180)
            text = " | ".join([*parts, part])
            if metrics.horizontalAdvance(text) + 16 > available:
                break
            parts.append(part)
        if not parts:
            return
        text = " | ".join(parts)
        box = QRect(target.left() + 4, target.top() + 4,
                    metrics.horizontalAdvance(text) + 16, metrics.height() + 12)
        painter.fillRect(box, QColor(0, 0, 0, 105))
        painter.setPen(QColor("#FFFFFF"))
        painter.drawText(box.adjusted(8, 0, -8, 0),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)

    def _paint_placeholder(self, painter: QPainter) -> None:
        rect = QRectF(self.rect())
        base = QLinearGradient(rect.topLeft(), rect.bottomRight())
        base.setColorAt(0.0, QColor("#0B1220"))
        base.setColorAt(1.0, QColor("#101B31"))
        painter.fillRect(rect, base)

        glow = QRadialGradient(
            QPointF(rect.width() * 0.28, rect.height() * 0.18), max(160.0, rect.width() * 0.42)
        )
        glow.setColorAt(0.0, QColor(34, 195, 232, 26))
        glow.setColorAt(1.0, QColor(34, 195, 232, 0))
        painter.fillRect(rect, glow)

        painter.setPen(QPen(QColor(255, 255, 255, 10), 1))
        step = 32
        for x in range(0, int(rect.width()), step):
            painter.drawLine(x, 0, x, int(rect.height()))
        for y in range(0, int(rect.height()), step):
            painter.drawLine(0, y, int(rect.width()), y)

        # 中心显示器轮廓
        center = rect.center()
        width = 88
        height = 58
        box = QRectF(center.x() - width / 2, center.y() - height / 2 - 20, width, height)
        painter.setPen(QPen(QColor("#3A4C69"), 1.4))
        painter.setBrush(QColor(255, 255, 255, 8))
        painter.drawRoundedRect(box, 8, 8)
        painter.drawLine(
            QPointF(box.center().x(), box.bottom()), QPointF(box.center().x(), box.bottom() + 12)
        )
        painter.drawLine(
            QPointF(box.center().x() - 20, box.bottom() + 12),
            QPointF(box.center().x() + 20, box.bottom() + 12),
        )

        font = QFont("Microsoft YaHei UI")
        font.setPointSizeF(11)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QPen(QColor("#D6E1F0")))
        title_rect = QRectF(rect.left(), box.bottom() + 30, rect.width(), 26)
        painter.drawText(title_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, PLACEHOLDER_TITLE)

        font.setPointSizeF(9)
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(QPen(QColor("#7387A3")))
        hint_rect = QRectF(rect.left(), box.bottom() + 56, rect.width(), 22)
        painter.drawText(hint_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, PLACEHOLDER_HINT)

    def _paint_badges(self, painter: QPainter, target: QRect, *, with_info: bool = True) -> None:
        font = QFont("Microsoft YaHei UI")
        font.setPointSizeF(8.5)
        painter.setFont(font)
        metrics = painter.fontMetrics()

        if self._badge and not self._stats_visible:
            text_width = metrics.horizontalAdvance(self._badge)
            box = QRect(
                target.left() + HUD_MARGIN,
                target.top() + HUD_MARGIN,
                text_width + 26,
                24,
            )
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(11, 18, 32, 190))
            painter.drawRoundedRect(box, 6, 6)
            painter.setPen(QPen(QColor("#5EE0B0")))
            painter.drawText(box, Qt.AlignmentFlag.AlignCenter, "● " + self._badge)

        if with_info and self._info:
            text_width = metrics.horizontalAdvance(self._info)
            box = QRect(
                target.left() + HUD_MARGIN,
                target.bottom() - HUD_MARGIN - 24,
                text_width + 22,
                24,
            )
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(11, 18, 32, 190))
            painter.drawRoundedRect(box, 6, 6)
            painter.setPen(QPen(QColor("#C7D5E8")))
            painter.drawText(box, Qt.AlignmentFlag.AlignCenter, self._info)


class PreviewFrame(QFrame):
    """带圆角黑底的预览容器（把 PreviewWidget 塞进去）。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("previewFrame")
        self.setMinimumHeight(240)
        self.preview = PreviewWidget(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(self.preview)


def hud_point(widget: QWidget) -> QPoint:
    """HUD 定位辅助（保留给外部叠加控件用）。"""
    return QPoint(widget.width() // 2, widget.height() // 2)
