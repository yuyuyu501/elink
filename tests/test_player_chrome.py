import os
import time
from types import SimpleNamespace

import numpy as np
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, Signal
from PySide6.QtGui import QImage, QKeyEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from elink.core.media import FrameMailbox
from elink.core.profiling import PipelineProfile
from elink.ui.chrome import TITLE_HEIGHT
from elink.ui.player import Player
from elink.ui.theme import apply_theme


class Runtime(QObject):
    feedback = Signal(object)

    def call(self, fn, *args):
        fn(*args)


def make_player():
    app = QApplication.instance() or QApplication([])
    apply_theme(app)
    events = []
    client = SimpleNamespace(pc=object(), mailbox=FrameMailbox(), rtt_ms=0, last_pong=time.monotonic(),
                             focus=lambda active: events.append(('focus', active)),
                             send=lambda event: events.append(('input', event)))
    player = Player(Runtime(), client, game_mouse=False, controllers=False)
    player.timer.stop()
    player.image = QImage(1280, 720, QImage.Format.Format_RGB32)
    player.image.fill(Qt.GlobalColor.darkBlue)
    player.show()
    app.processEvents()
    return app, player, events


def test_titlebar_buttons_and_double_click_stay_local():
    app, player, events = make_player()
    ended = []
    player.ended.connect(lambda: ended.append(True))
    try:
        assert player.windowFlags() & Qt.WindowType.FramelessWindowHint
        if app.platformName() == 'windows':
            assert player.frameGeometry() == player.geometry()
        assert player.toolbar.height() == TITLE_HEIGHT
        assert player.video_viewport().top() == TITLE_HEIGHT
        assert all(handle.isVisible() for handle in player.resize_handles)
        QTest.mouseDClick(player.toolbar, Qt.MouseButton.LeftButton, pos=QPoint(250, 20))
        app.processEvents()
        assert player.isMaximized()
        if app.platformName() == 'windows':
            assert player.screen().availableGeometry().contains(player.frameGeometry())
        assert all(not handle.isVisible() for handle in player.resize_handles)
        player.toolbar.maximize.click()
        app.processEvents()
        assert not player.isMaximized()
        player.toolbar.minimize.click()
        app.processEvents()
        assert player.isMinimized() and not player.captured
        player.showNormal()
        app.processEvents()
        player.controls_button.click()
        app.processEvents()
        assert player.control_menu.isVisible()
        player.control_menu.close()
        assert not any(kind == 'input' for kind, _ in events)
        player.toolbar.close_button.click()
        assert ended == [True] and player._closed
    finally:
        player.close()


def test_bgr_frame_is_presented_without_a_second_qimage_copy():
    app, player, events = make_player()
    pixels = np.zeros((8, 12, 3), dtype=np.uint8)
    # The receive path now keeps decoder output in BGR888 so QImage can wrap
    # it directly without a channel-shuffling copy.
    pixels[2, 3] = [30, 20, 10]
    try:
        player.set_frame_pixels(pixels)
        assert player.image_pixels is pixels
        assert player.image.pixelColor(3, 2).getRgb()[:3] == (10, 20, 30)
        pixels[2, 3] = [40, 50, 60]
        assert player.image.pixelColor(3, 2).getRgb()[:3] == (60, 50, 40)
    finally:
        player.close()


def test_gpu_unavailable_falls_back_without_losing_cpu_display(monkeypatch):
    app, player, events = make_player()
    modes = []
    player.client.set_renderer = modes.append
    player.client.profile = PipelineProfile()
    player.preferences['renderer'] = 'gpu'
    monkeypatch.setattr(QApplication, 'platformName', lambda: 'offscreen')
    try:
        player.setup_renderer()
        assert player.gpu_surface is None and modes == ['cpu']
        assert '回退' in player.error_label.text()
        pixels = np.zeros((10, 20, 3), dtype=np.uint8)
        player.client.mailbox.put(pixels)
        player.tick()
        app.processEvents()
        assert player.image_pixels is pixels
        assert player.client.profile.snapshot()['counters']['presented_frames'] > 0
        # Late failure notifications after shutdown must not touch GUI objects.
        player.close()
        player.fallback_renderer('late callback')
        assert modes == ['cpu']
    finally:
        player.close()


def test_gpu_frame_allows_click_to_capture_without_qimage(monkeypatch):
    app, player, events = make_player()
    captured = []
    player.image = None
    player.gpu_surface = SimpleNamespace(frame=object())
    monkeypatch.setattr(player, 'capture', lambda: captured.append(True))
    try:
        QTest.mouseClick(player, Qt.MouseButton.LeftButton, pos=player.video_viewport().center())
        assert captured == [True]
    finally:
        player.gpu_surface = None
        player.close()


def test_gpu_overlay_refresh_keeps_video_and_raster_drawing_separate():
    from elink.ui.gpu_video import VideoOverlay
    app, player, events = make_player()
    video_updates = []
    modes = []
    player.client.set_renderer = modes.append
    surface = SimpleNamespace(update=lambda: video_updates.append(True),
                              hide=lambda: None, cleanup=lambda: None, deleteLater=lambda: None)
    player.gpu_surface = surface
    overlay = player.gpu_overlay = VideoOverlay(player)
    overlay.setGeometry(player.rect())
    overlay.stackUnder(player.toolbar)
    overlay.show()
    try:
        assert overlay.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        assert overlay.focusPolicy() == Qt.FocusPolicy.NoFocus
        player.refresh_video()
        player.paintEvent(None)
        app.processEvents()
        # Overlay changes must not schedule video inside the parent's paint
        # event; that creates a self-sustaining composition/repaint loop.
        assert video_updates == []
        player.fallback_renderer('injected draw failure')
        assert player.gpu_surface is player.gpu_overlay is None
        assert not overlay.isVisible()
        assert modes == ['cpu']
        pixels = np.full((10, 20, 3), 120, dtype=np.uint8)
        player.client.mailbox.put(pixels)
        player.tick()
        app.processEvents()
        assert player.image_pixels is pixels
        assert player.toolbar.isVisible() and player.error_label.isVisible()
    finally:
        player.close()


def test_fullscreen_uses_entire_video_area_and_f8_remains_accessible():
    app, player, events = make_player()
    try:
        player.showMaximized()
        app.processEvents()
        player.toggle_fullscreen()
        app.processEvents()
        assert player.isFullScreen() and player.was_maximized
        assert player.video_viewport() == player.rect()
        player.capture()
        app.processEvents()
        assert not player.toolbar.isVisible()
        assert all(not handle.isVisible() for handle in player.resize_handles)
        QApplication.sendEvent(player, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Tab, Qt.KeyboardModifier.NoModifier))
        QApplication.sendEvent(player, QKeyEvent(QEvent.Type.KeyRelease, Qt.Key.Key_Tab, Qt.KeyboardModifier.NoModifier))
        tab_events = [value for kind, value in events if kind == 'input' and value.get('type') == 'key']
        assert [event['down'] for event in tab_events] == [True, False]
        events.clear()
        QTest.keyClick(player, Qt.Key.Key_F8)
        app.processEvents()
        assert not player.captured and player.toolbar.isVisible()
        assert player.control_menu.isVisible()
        assert not any(kind == 'input' for kind, _ in events)
        player.control_menu.close()
        player.toggle_fullscreen()
        app.processEvents()
        assert player.isMaximized() and not player.isFullScreen()
        assert player.video_viewport().top() == TITLE_HEIGHT
    finally:
        player.close()


def test_mouse_coordinates_account_for_single_titlebar():
    app, player, events = make_player()
    try:
        player.resize(1000, 650)
        app.processEvents()
        player.capture()
        events.clear()
        QTest.mouseMove(player, player.rect_image.topLeft())
        QTest.mouseMove(player, player.rect_image.bottomRight())
        moves = [value for kind, value in events if kind == 'input' and value['type'] == 'move']
        assert moves[0]['x'] == moves[0]['y'] == 0
        assert moves[-1]['x'] == moves[-1]['y'] == 65535
    finally:
        player.close()


def test_drag_and_resize_use_window_operations_not_remote_input(monkeypatch):
    app, player, events = make_player()
    operations = []
    handle = SimpleNamespace(startSystemMove=lambda: operations.append('move'),
                             startSystemResize=lambda edges: operations.append(edges))
    monkeypatch.setattr(player, 'windowHandle', lambda: handle)
    try:
        QTest.mouseClick(player.toolbar, Qt.MouseButton.LeftButton, pos=QPoint(250, 20))
        QTest.mouseClick(player.resize_handles[0], Qt.MouseButton.LeftButton)
        QTest.mouseClick(player.resize_handles[-1], Qt.MouseButton.LeftButton)
        assert operations == ['move', Qt.Edge.TopEdge, Qt.Edge.RightEdge | Qt.Edge.BottomEdge]
        assert not any(kind == 'input' for kind, _ in events)
    finally:
        player.close()
