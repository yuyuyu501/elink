from __future__ import annotations

import time
import av
import logging
from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QFont, QImage, QPainter, QPolygon
from PySide6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton, QDialog, QApplication, QFileDialog

from .controls import StreamOptions
from .chrome import TITLE_HEIGHT, StreamTitleBar, make_resize_handles, place_resize_handles
from .control_center import ControlCenter, LocalPanel
from .icons import icon

from ..core.statistics import StreamStats, overlay_lines
from ..core.input import CursorConfinement, RawMouseInput, RemoteKeyboardHook, XInput
from ..core import codecs
from ..core.mouse import MouseSensitivity, mouse_mode, relative_mouse


_DIRECTION_KEYS = {
    Qt.Key.Key_Up: (0x26, 0x48),
    Qt.Key.Key_Down: (0x28, 0x50),
    Qt.Key.Key_Left: (0x25, 0x4B),
    Qt.Key.Key_Right: (0x27, 0x4D),
}


class QualityDialog(QDialog):
    applying = False

    def reject(self):
        if not self.applying:
            super().reject()

    def closeEvent(self, event):
        if self.applying:
            event.ignore()
        else:
            super().closeEvent(event)


def remote_key_event(event, down):
    """Convert Qt's key event into a stable Windows keyboard identity.

    Qt reports NumLock-off keypad arrows as the same semantic arrow keys as
    the navigation cluster. Conversely, NumLock-on keypad digits retain their
    VK_NUMPAD identity. Prefer those semantics over the destination machine's
    NumLock state so a remote keypad does not turn into 2/4/6/8 arrows.
    """
    key = event.key()
    if key in _DIRECTION_KEYS:
        vk, scan = _DIRECTION_KEYS[key]
        return {"type": "key", "vk": vk, "scan": scan, "extended": True, "down": down}
    native_vk = int(event.nativeVirtualKey() or 0)
    if 0x60 <= native_vk <= 0x69 and key not in _DIRECTION_KEYS:
        return {"type": "key", "vk": native_vk, "scan": 0, "extended": False, "down": down}
    scan = int(event.nativeScanCode() or 0)
    return {"type": "key", "vk": native_vk, "scan": scan & 255,
            "extended": bool(scan & 0x100), "down": down}


class Player(QWidget):
    ended = Signal()
    reconfigure = Signal(dict)
    settings_changed = Signal(dict)
    frame_ready = Signal()

    def __init__(self, runtime, client, *, game_mouse=None, controllers=True, preferences=None):
        super().__init__()
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.was_maximized = False
        self.runtime, self.client = runtime, client
        self.controllers = controllers
        self.preferences = dict(preferences or {})
        self.mouse_mode = mouse_mode(self.preferences.get('mouse_mode', 'remote' if game_mouse else 'smart'))
        self.mouse_sensitivity = MouseSensitivity(self.preferences.get('mouse_sensitivity', 0))
        self._absolute_last = self._absolute_pos = None
        self._relative_last = None
        self.game_mouse = False  # Effective input mapping; independent of cursor rendering.
        self.local_cursor = False
        self.software_cursor = False
        self.software_cursor_pos = QPoint()
        if hasattr(client, 'set_cursor_mode'):
            self.runtime.call(client.set_cursor_mode, self.mouse_mode)
        self.muted = False
        self.control_menu = None
        self.settings_dialog = None
        self.quality_applying = False
        self.captured = False
        self._resume_on_hover = False
        self.gpu_surface = None
        self.gpu_overlay = None
        self.renderer_widgets = None
        self.frame_taken_at = None
        self.image = None
        # QImage wraps this buffer without copying. Keep the numpy owner alive
        # until the next frame replaces it; all access happens on the GUI thread.
        self.image_pixels = None
        self._scaled_image = None
        self._scaled_image_size = None
        self._scaled_viewport_size = None
        self.rect_image = QRect()
        self.drawn = 0
        self.fps = 0
        self.stats_at = time.monotonic()
        self.new_image = False
        self.show_statistics = True
        self.pad_indices = set()
        self.xinput = XInput() if controllers else None
        self.keyboard_hook = RemoteKeyboardHook(self.send, lambda: self.captured)
        self.raw_mouse = RawMouseInput()
        self.cursor_confinement = CursorConfinement()
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
        self.setup_renderer()
        self.frame_ready.connect(self.consume_frame, Qt.ConnectionType.QueuedConnection)
        self.client.mailbox.set_notifier(self.frame_ready.emit)

    def setup_renderer(self):
        if self.preferences.get('renderer', 'cpu') != 'gpu':
            return
        if QApplication.platformName() in ('offscreen', 'minimal'):
            self.fallback_renderer('当前窗口平台不支持 GPU 纹理显示')
            return
        try:
            from .gpu_video import VideoOverlay, YuvSurface
            if self.renderer_widgets:
                self.gpu_surface, self.gpu_overlay = self.renderer_widgets
                self.gpu_surface.reconfigure()
            else:
                self.gpu_surface = YuvSurface(self)
                self.gpu_overlay = VideoOverlay(self)
                self.renderer_widgets = self.gpu_surface, self.gpu_overlay
            surface = self.gpu_surface
            if not getattr(surface, 'failure_connected', False):
                surface.failed.connect(lambda message: QTimer.singleShot(
                    0, lambda: self.fallback_renderer(message) if self.gpu_surface is surface else None))
                surface.failure_connected = True
            self.gpu_surface.setGeometry(self.rect())
            self.gpu_surface.lower()
            self.gpu_surface.show()
            self.gpu_overlay.setGeometry(self.rect())
            self.gpu_overlay.stackUnder(self.toolbar)
            self.gpu_overlay.show()
            if hasattr(self.client, 'set_renderer'):
                self.runtime.call(self.client.set_renderer, 'gpu')
            QTimer.singleShot(1000, self.check_renderer)
        except Exception as exc:
            self.fallback_renderer(str(exc))

    def check_renderer(self):
        if not self._closed and self.gpu_surface and not self.gpu_surface.isValid():
            self.fallback_renderer('OpenGL 上下文创建失败')

    def fallback_renderer(self, reason):
        if self._closed:
            return
        self.clear_renderer()
        if hasattr(self.client, 'set_renderer'):
            self.runtime.call(self.client.set_renderer, 'cpu')
        if hasattr(self.client, 'profile'):
            self.client.profile.describe(renderer='qimage-bgr', renderer_fallback=str(reason)[:240])
        self.show_error('已回退兼容显示：' + str(reason)[:180])
        self.refresh_video()

    def clear_renderer(self):
        # Removing/replacing the last QOpenGLWidget can invalidate Qt's window
        # compositor. Keep one context for the entire Player lifetime.
        surface, self.gpu_surface = self.gpu_surface, None
        overlay, self.gpu_overlay = self.gpu_overlay, None
        if overlay:
            overlay.hide()
        if surface:
            # Release interop resources while this surface is still current.
            surface.release_upscaler()
            surface.hide()
            surface.frame = None
            surface.pending = False

    def apply_preferences(self, values):
        rebuild = any(self.preferences.get(key) != values.get(key)
                      for key in ('renderer', 'resolution', 'render_scale', 'upscaler'))
        rebuild = rebuild or (values['renderer'] == 'gpu') != (self.gpu_surface is not None)
        self.preferences.update(values)
        if rebuild:
            self.clear_renderer()
            self.setup_renderer()
            self.layout_chrome()
        if self.controllers != values['controllers']:
            self.set_controllers(values['controllers'])
        self.finish_quality()

    def finish_quality(self, error=None):
        self.quality_applying = False
        dialog = self.settings_dialog
        if not dialog:
            return
        dialog.applying = False
        if error:
            dialog.options.setEnabled(True)
            dialog.apply_button.setEnabled(True)
            dialog.apply_button.setText('应用')
            dialog.cancel_button.setEnabled(True)
            dialog.note.setText(str(error))
            dialog.note.show()
        else:
            dialog.accept()

    def export_performance(self):
        self.release()
        filename, _ = QFileDialog.getSaveFileName(self, '导出性能报告', 'elink-performance.json', 'JSON (*.json)')
        if not filename:
            return
        def saved(report):
            from ..storage import atomic_json
            try:
                atomic_json(Path(filename), report)
                self.show_error('性能报告已保存：' + filename, timeout_ms=3000)
            except OSError as exc:
                self.show_error(str(exc))
        self.runtime.submit(self.client.performance_report(), saved, self.show_error)

    def frame_presented(self):
        self.drawn += 1
        self.new_image = False
        profile = getattr(self.client, 'profile', None)
        if profile:
            profile.mark('present_interval')
            profile.count('presented_frames')
            if self.frame_taken_at is not None:
                profile.elapsed('gui_schedule_to_paint', self.frame_taken_at)
        self.frame_taken_at = None

    def make_toolbar(self):
        self.toolbar = StreamTitleBar(self)
        self.stream_title = self.toolbar.title
        self.controls_button = self.toolbar.controls
        self.resize_handles = make_resize_handles(self)
        self.error_bar = LocalPanel(self)
        self.error_bar.setObjectName('errorNotice')
        row = QHBoxLayout(self.error_bar)
        row.setContentsMargins(12, 10, 8, 10)
        self.error_label = QLabel()
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.setWordWrap(True)
        self.error_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        row.addWidget(self.error_label, 1)
        self.error_close = QPushButton()
        self.error_close.setObjectName('noticeClose')
        self.error_close.setIcon(icon('x', '#a53232'))
        self.error_close.setToolTip('关闭提示')
        self.error_close.setAccessibleName('关闭提示')
        self.error_close.setFixedSize(32, 32)
        self.error_close.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.error_close.clicked.connect(self.dismiss_error)
        row.addWidget(self.error_close, 0, Qt.AlignmentFlag.AlignTop)
        self.error_bar.hide()
        self.error_timer = QTimer(self)
        self.error_timer.setSingleShot(True)
        self.error_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.error_timer.timeout.connect(self.dismiss_error)

    def video_viewport(self):
        return self.rect().adjusted(0, 0 if self.isFullScreen() else TITLE_HEIGHT, 0, 0)

    def layout_chrome(self):
        self.toolbar.setGeometry(0, 0, self.width(), TITLE_HEIGHT)
        self.toolbar.setVisible(not self.isFullScreen() or not self.captured)
        self.toolbar.update_state()
        top = TITLE_HEIGHT if self.toolbar.isVisible() else 0
        self.error_bar.setGeometry(16, top + 8, self.width() - 32, 72)
        if self.gpu_surface:
            self.gpu_surface.setGeometry(self.rect())
            self.gpu_surface.lower()
        if self.gpu_overlay:
            self.gpu_overlay.setGeometry(self.rect())
        place_resize_handles(self)
        if self.control_menu and self.control_menu.isVisible():
            self.control_menu.sync()
            self.control_menu.place()
        self.refresh_video()

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

    def dismiss_error(self):
        self.error_timer.stop()
        self.error_bar.hide()

    def show_error(self, message, *, timeout_ms=3000):
        message = str(message)
        if message == self.error_label.text():
            return
        logging.getLogger(__name__).warning('%s', message)
        self.error_timer.stop()
        self.error_label.setText(message)
        self.error_bar.show()
        self.error_bar.raise_()
        if timeout_ms > 0:
            self.error_timer.start(timeout_ms)

    def rtx_unavailable(self, reason):
        logging.getLogger(__name__).info('RTX VSR fell back to FSR 1: %s', reason)
        self.preferences['upscaler'] = 'fsr'
        if self.settings_dialog:
            self.settings_dialog.options.set_rtx_available(False)
            self.settings_dialog.options.restore({'upscaler': 'fsr'})
        self.settings_changed.emit({'upscaler': 'fsr'})

    def open_controls(self):
        if self._closed:
            return
        if self.control_menu and self.control_menu.isVisible():
            self.control_menu.hide()
            self._resume_on_hover = True
            self.setFocus()
            return
        self.release()
        if not self.control_menu:
            self.control_menu = ControlCenter(self)
        self.control_menu.sync()
        self.control_menu.show()
        self.control_menu.place()

    def set_mouse_sensitivity(self, value):
        self.mouse_sensitivity = MouseSensitivity(value)
        self.preferences['mouse_sensitivity'] = self.mouse_sensitivity.value
        self._absolute_last = self._absolute_pos = None
        self.settings_changed.emit({'mouse_sensitivity': self.mouse_sensitivity.value})

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
        self.refresh_video()

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
        dialog = self.settings_dialog = QualityDialog(self)
        dialog.setWindowTitle("画质与传输设置")
        dialog.setWindowIcon(icon('sliders-horizontal', '#2855eb'))
        dialog.setMinimumWidth(500)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(20)
        options = StreamOptions()
        dialog.options = options
        options.restore(self.preferences)
        options.refresh_upscalers(self.runtime)
        layout.addWidget(options)
        note = QLabel()
        dialog.note = note
        note.setTextFormat(Qt.TextFormat.PlainText)
        note.setWordWrap(True)
        note.setObjectName('muted')
        note.hide()
        layout.addWidget(note)
        buttons = QHBoxLayout()
        cancel = QPushButton("取消")
        dialog.cancel_button = cancel
        cancel.clicked.connect(dialog.reject)
        apply = QPushButton("应用")
        dialog.apply_button = apply
        apply.setObjectName('primary')
        def accept():
            if self.quality_applying:
                return
            from ..core.transport import settings
            from ..models import ValidationError
            width, height = options.resolution.currentData()
            try:
                settings(dict(width=width, height=height, fps=options.fps.currentData(),
                              render_scale=options.render_scale.currentData()))
            except ValidationError as exc:
                note.setText(str(exc))
                note.show()
                return
            values = options.values()
            self.quality_applying = True
            dialog.applying = True
            options.setEnabled(False)
            apply.setEnabled(False)
            apply.setText('应用中…')
            cancel.setEnabled(False)
            note.hide()
            self.reconfigure.emit(values)
        apply.clicked.connect(accept)
        buttons.addWidget(cancel)
        buttons.addWidget(apply)
        layout.addLayout(buttons)
        def finished(_):
            self.settings_dialog = None
            dialog.deleteLater()
        dialog.finished.connect(finished)
        dialog.setMinimumWidth(max(500, dialog.minimumSizeHint().width()))
        dialog.open()

    def send(self, event, reliable=False):
        if reliable:
            self.runtime.call(self.client.send, event, True)
        else:
            self.runtime.call(self.client.send, event)

    def capture(self):
        if self.captured or (self.control_menu and self.control_menu.isVisible()):
            return
        self._resume_on_hover = False
        self.setFocus()
        self.captured = True
        self.layout_chrome()
        self.runtime.call(self.client.focus, True)
        try:
            self.keyboard_hook.start()
        except OSError as exc:
            # Qt input remains usable if another process prevents a low-level
            # hook. The notice makes the limitation visible during testing.
            self.show_error(f"无法拦截 Windows 系统快捷键：{exc}")
        self.software_cursor_pos = self.mapFromGlobal(QCursor.pos())
        if not self.rect_image.contains(self.software_cursor_pos):
            self.software_cursor_pos = self.rect_image.center()
        self._absolute_last = self._absolute_pos = QPointF(self.software_cursor_pos)
        self.mouse_sensitivity.remainder = [0.0, 0.0]
        self.update_mouse(force=True)

    def update_mouse(self, force=False):
        if not self.captured:
            return
        if self.error_bar.isVisible() and self.error_bar.rect().contains(self.error_bar.mapFromGlobal(QCursor.pos())):
            self.release(resume_on_hover=True)
            return
        if not self.isFullScreen() and QApplication.platformName() == 'windows':
            if self.release_outside_video(QPointF(self.mapFromGlobal(QCursor.pos()))):
                return
        fresh = time.monotonic() - getattr(self.client, 'cursor_updated', 0) < 2
        visible = getattr(self.client, 'cursor_visible', None) if fresh else None
        supported = getattr(self.client, 'cursor_supported', False)
        relative = relative_mouse(self.mouse_mode, visible)
        if not supported and self.mouse_mode == 'smart':
            relative = False  # Older hosts cannot report whether games hide their cursor.
        # The host omits its system cursor in local mode. Keep the controller's
        # OS cursor hidden and paint a software arrow over the received frame;
        # this avoids a duplicate cursor while still making the pointer visible.
        local = False
        software = self.captured and self.mouse_mode == 'local' and not relative
        if force or software != self.software_cursor:
            self.software_cursor = software
            self.refresh_video()
        if force or relative != self.game_mouse:
            self._absolute_last = self._absolute_pos = None
            self._relative_last = QPointF(self.mapFromGlobal(QCursor.pos()))
            self.mouse_sensitivity.remainder = [0.0, 0.0]
            if relative:
                self.grabMouse(QCursor(Qt.CursorShape.BlankCursor))
                if self.isFullScreen():
                    QCursor.setPos(self.mapToGlobal(self.rect_image.center()))
                try:
                    self.raw_mouse.start(int(self.winId()))
                except OSError as exc:
                    self.show_error(f"无法读取主控端原始鼠标输入：{exc}")
            else:
                self.cursor_confinement.release()
                self.releaseMouse()
                self.raw_mouse.stop()
            self.game_mouse = relative
            if not relative:
                position = (QPointF(self.software_cursor_pos) if force else
                            QPointF(self.mapFromGlobal(QCursor.pos())))
                self._absolute_last = self._absolute_pos = QPointF(position)
                self.send_absolute_motion(position, reliable=True, adjust=False)
        if force or local != self.local_cursor:
            self.setCursor(Qt.CursorShape.ArrowCursor if local else Qt.CursorShape.BlankCursor)
            self.local_cursor = local
        # Windowed sessions release at the video edge. Only fullscreen games
        # need a confined pointer for continuous relative movement.
        if relative and self.isFullScreen() and QApplication.platformName() == 'windows':
            try:
                rect = self.rect_image.intersected(self.video_viewport())
                self.cursor_confinement.confine(int(self.winId()), rect.getRect(), (self.width(), self.height()))
            except OSError as exc:
                self.release()
                self.show_error(f'无法约束主控端鼠标：{exc}')
        else:
            self.cursor_confinement.release()

    def release(self, *, resume_on_hover=False):
        self.captured = False
        self._resume_on_hover = resume_on_hover
        self.keyboard_hook.stop()
        self.raw_mouse.stop()
        self._relative_last = None
        self.cursor_confinement.release()
        self.runtime.call(self.client.focus, False)
        self.releaseMouse()
        self.unsetCursor()
        self.software_cursor = False
        self.refresh_video()
        if self.xinput:
            for index in self.pad_indices:
                self.xinput.rumble(index)
        self.pad_indices.clear()
        self.layout_chrome()

    def consume_frame(self):
        if self._closed:
            return
        pixels = self.client.mailbox.take()
        if pixels is not None:
            self.frame_taken_at = time.perf_counter()
            if isinstance(pixels, av.VideoFrame):
                if self.gpu_surface:
                    try:
                        old_rect = QRect(self.rect_image)
                        self.gpu_surface.set_frame(pixels)
                        if self.rect_image != old_rect:
                            self.refresh_video()
                    except Exception as exc:
                        self.fallback_renderer(str(exc))
                # A frame queued before a fallback is discarded, never converted
                # on the GUI thread. The receive worker prepares the next BGR frame.
            else:
                self.set_frame_pixels(pixels)
                self.refresh_video()

    def tick(self):
        self.resume_hover_input()
        self.update_mouse()
        now = time.monotonic()
        stats_due = now - self.stats_at >= 1
        if stats_due:
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
        # Video is notified as soon as a frame arrives; the timer only handles
        # input/statistics. A slow GUI retains one newest frame and one wakeup.
        if stats_due:
            self.refresh_video()

    def set_frame_pixels(self, pixels):
        """Present a BGR ndarray while retaining its owner for QImage."""
        self.image_pixels = pixels
        self.image = QImage(pixels.data, pixels.shape[1], pixels.shape[0],
                            pixels.strides[0], QImage.Format.Format_BGR888)
        self._scaled_image = None
        self._scaled_image_size = None
        self._scaled_viewport_size = None
        self.new_image = True

    def rumble(self, event):
        if self.captured and self.xinput and not self._closed:
            self.xinput.rumble(event["index"], event["left"], event["right"])

    def refresh_video(self):
        if self.gpu_surface:
            if self.gpu_overlay:
                self.gpu_overlay.update()
        else:
            self.update()

    def paintEvent(self, event):
        if self.gpu_surface:
            return
        paint_started = time.perf_counter()
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#101719"))
        viewport = self.video_viewport()
        if self.image:
            size = self.image.size().scaled(viewport.size(), Qt.AspectRatioMode.KeepAspectRatio)
            self.rect_image = QRect((self.width() - size.width()) // 2, viewport.top() + (viewport.height() - size.height()) // 2, size.width(), size.height())
            scale_started = time.perf_counter()
            viewport_size = viewport.size()
            if (self._scaled_image is None or self._scaled_image_size != size
                    or self._scaled_viewport_size != viewport_size):
                # Scale once per received frame/viewport change. Previously
                # QPainter repeated the conversion on every 8 ms repaint.
                # A 1:1 frame needs no resampling; reusing the original QImage
                # saves a full-frame CPU pass and keeps desktop text pixel sharp.
                self._scaled_image = (self.image if self.image.size() == size else
                                      self.image.scaled(size, Qt.AspectRatioMode.KeepAspectRatio,
                                                        Qt.TransformationMode.SmoothTransformation))
                self._scaled_image_size = size
                self._scaled_viewport_size = viewport_size
            profile = getattr(self.client, 'profile', None)
            if profile and self.new_image:
                profile.elapsed('qt_scale', scale_started)
            started = time.perf_counter()
            painter.drawImage(self.rect_image.topLeft(), self._scaled_image)
            codecs.metrics["present_ms"] = (time.perf_counter() - started) * 1000
            if self.new_image:
                self.frame_presented()
        self.paint_overlay(painter)
        painter.end()
        profile = getattr(self.client, 'profile', None)
        if profile:
            profile.elapsed('cpu_paint_total', paint_started)

    def paint_overlay(self, painter):
        if self.software_cursor and not self.rect_image.isEmpty():
            self.draw_software_cursor(painter)
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
    def draw_software_cursor(self, painter):
        point = self.software_cursor_pos
        if not self.rect_image.contains(point):
            return
        x, y = point.x(), point.y()
        arrow = QPolygon([QPoint(x, y), QPoint(x + 2, y + 20),
                          QPoint(x + 7, y + 15), QPoint(x + 14, y + 24),
                          QPoint(x + 18, y + 21), QPoint(x + 11, y + 12),
                          QPoint(x + 19, y + 11)])
        painter.save()
        painter.setPen(QColor('#000000'))
        painter.setBrush(QColor('#ffffff'))
        painter.drawPolygon(arrow)
        painter.restore()

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
            self.refresh_video()
        elif event.key() == Qt.Key.Key_Z and event.modifiers() & modifiers == modifiers:
            self.release()
        elif self.captured and not event.isAutoRepeat():
            self.key(event, True)
        event.accept()

    def keyReleaseEvent(self, event):
        if self.captured and not event.isAutoRepeat() and event.key() not in (Qt.Key.Key_F8, Qt.Key.Key_F10, Qt.Key.Key_F11):
            self.key(event, False)
        event.accept()

    def focusNextPrevChild(self, next_child):
        # Qt normally consumes Tab for widget focus traversal before it reaches
        # keyPressEvent. A captured session must forward it to the host game.
        if self.captured:
            return False
        return super().focusNextPrevChild(next_child)

    def key(self, event, down):
        self.send(remote_key_event(event, down))

    def mouseMoveEvent(self, event):
        if not self.captured:
            self.resume_hover_input(event.position())
            if not self.captured:
                return
        if self.release_outside_video(event.position()):
            return
        if self.game_mouse:
            # Relative input comes from Raw Input, preserving controller-side
            # device counts instead of Qt's accelerated pointer coordinates.
            if not self.raw_mouse.active:
                position = event.position()
                origin = QPointF(self.rect_image.center()) if self.isFullScreen() else self._relative_last
                delta = position - (origin if origin is not None else position)
                self._relative_last = QPointF(position)
                if not delta.isNull():
                    self.send_relative_motion(delta.x(), delta.y())
                    if self.isFullScreen():
                        QCursor.setPos(self.mapToGlobal(self.rect_image.center()))
            event.accept()
            return
        else:
            self.send_absolute_motion(event.position())

    def send_absolute_motion(self, position, reliable=False, adjust=True):
        rect = self.rect_image
        if rect.isEmpty():
            return
        position = self.absolute_position(position) if adjust else position
        position = QPointF(max(rect.left(), min(rect.right(), position.x())),
                           max(rect.top(), min(rect.bottom(), position.y())))
        self.software_cursor_pos = position.toPoint()
        if self.software_cursor:
            self.refresh_video()
        x = int((position.x() - rect.x()) * 65535 / max(1, rect.width() - 1))
        y = int((position.y() - rect.y()) * 65535 / max(1, rect.height() - 1))
        self.send({'type': 'move', 'absolute': True, 'x': x, 'y': y}, reliable)

    def absolute_position(self, position):
        factor = self.mouse_sensitivity.factor
        if factor == 1 or self._absolute_last is None:
            self._absolute_last = self._absolute_pos = QPointF(position)
            return position
        delta = position - self._absolute_last
        rect = self.rect_image
        point = self._absolute_pos + delta * factor
        point = QPointF(max(rect.left(), min(rect.right(), point.x())),
                        max(rect.top(), min(rect.bottom(), point.y())))
        self._absolute_pos = point
        # Keep the hidden OS pointer aligned with the adjusted desktop pointer
        # so slow movement can still reach both edges of the remote desktop.
        self._absolute_last = QPointF(point.toPoint())
        if point.toPoint() != position.toPoint():
            QCursor.setPos(self.mapToGlobal(point.toPoint()))
        return point

    def send_relative_motion(self, x, y):
        x, y = self.mouse_sensitivity.relative(x, y)
        if x or y:
            self.send({'type': 'move', 'absolute': False, 'x': x, 'y': y})

    def nativeEvent(self, event_type, message):
        if self.captured and self.game_mouse:
            delta = self.raw_mouse.delta(message)
            if delta and (delta[0] or delta[1]):
                # WM_INPUT can arrive before Qt reports the pointer leaving.
                # Stop remote input before the same movement reaches local UI.
                if self.release_outside_video(QPointF(self.mapFromGlobal(QCursor.pos()))):
                    return False, 0
                self.send_relative_motion(*delta)
                return True, 0
        return super().nativeEvent(event_type, message)

    def mousePressEvent(self, event):
        if not self.captured:
            has_frame = self.image is not None or (self.gpu_surface is not None and self.gpu_surface.frame is not None)
            if self.client.pc and self.video_contains(event.position()) and has_frame:
                self.capture()
            return
        self.mouse_button(event, True)

    def mouseReleaseEvent(self, event):
        if self.captured:
            self.mouse_button(event, False)

    def mouse_button(self, event, down):
        if self.release_outside_video(event.position()):
            return
        name = {Qt.MouseButton.LeftButton: "left", Qt.MouseButton.RightButton: "right", Qt.MouseButton.MiddleButton: "middle"}.get(event.button())
        if name:
            if not self.game_mouse:
                self.send_absolute_motion(event.position(), reliable=True)
            self.send({"type": "button", "button": name, "down": down})

    def wheelEvent(self, event):
        if self.captured and not self.release_outside_video(event.position()):
            self.send({"type": "wheel", "delta": event.angleDelta().y()})

    def video_contains(self, position):
        rect = self.rect_image
        # Avoid rounding a fractional position across the edge on scaled windows.
        return (rect.x() <= position.x() < rect.x() + rect.width()
                and rect.y() <= position.y() < rect.y() + rect.height())

    def resume_hover_input(self, position=None):
        if (not self._resume_on_hover or self.captured or self._closed
                or not self.isVisible() or self.isMinimized() or self.client.pc is None
                or QApplication.mouseButtons() != Qt.MouseButton.NoButton
                or QApplication.activeModalWidget() is not None
                or QApplication.activePopupWidget() is not None
                or (self.control_menu and self.control_menu.isVisible())
                or (self.settings_dialog and self.settings_dialog.isVisible())):
            return
        has_frame = self.image is not None or (self.gpu_surface is not None and self.gpu_surface.frame is not None)
        position = QPointF(self.mapFromGlobal(QCursor.pos())) if position is None else position
        if not has_frame or not self.video_contains(position):
            return
        # The video can be covered by another window or by local title/resize
        # controls. Geometry alone would resume input through those surfaces.
        if QApplication.widgetAt(self.mapToGlobal(position.toPoint())) is not self:
            return
        if not self.isActiveWindow():
            self.activateWindow()
            if not self.isActiveWindow():
                return
        self.capture()

    def release_outside_video(self, position):
        if not (self.game_mouse and self.isFullScreen()) and not self.video_contains(position):
            if not self.game_mouse:
                # The last move can jump over an edge or arrive after the poll.
                # Order the final desktop position before the focus epoch ends.
                self.send_absolute_motion(position, reliable=True, adjust=False)
            self.release(resume_on_hover=True)
            return True
        return False

    def leaveEvent(self, event):
        if self.captured:
            self.release_outside_video(QPointF(self.mapFromGlobal(QCursor.pos())))
        super().leaveEvent(event)

    def enterEvent(self, event):
        self.resume_hover_input()
        super().enterEvent(event)

    def event(self, event):
        if getattr(self, 'captured', False) and event.type() in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            if event.key() == Qt.Key.Key_Tab and not event.isAutoRepeat():
                self.key(event, event.type() == QEvent.Type.KeyPress)
                event.accept()
                return True
        if event.type() in (QEvent.Type.WindowDeactivate, QEvent.Type.FocusOut) and getattr(self, "captured", False):
            outside = not self.video_contains(QPointF(self.mapFromGlobal(QCursor.pos())))
            self.release(resume_on_hover=outside)
        return super().event(event)

    def closeEvent(self, event):
        if not self._closed:
            self.client.mailbox.set_notifier(None)
            if self.control_menu:
                self.control_menu.close()
            if self.settings_dialog:
                self.settings_dialog.reject()
            self.release()
            self.timer.stop()
            self.runtime.feedback.disconnect(self.rumble)
            if self.renderer_widgets:
                self.renderer_widgets[0].cleanup()
            self._closed = True
            self.ended.emit()
        event.accept()
