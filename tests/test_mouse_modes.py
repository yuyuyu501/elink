import json
import time

from PySide6.QtCore import Qt, QPoint, QPointF, QEvent
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from test_player_chrome import make_player
from test_device_ui import close
from elink.ui.window import MainWindow
from elink.ui.player import remote_key_event


def test_smart_switch_local_ack_and_stale_state():
    app, player, events = make_player()
    client = player.client
    client.cursor_supported = True
    client.cursor_applied = 'smart'
    client.cursor_visible = True
    client.cursor_updated = time.monotonic()
    try:
        player.set_mouse_mode('smart')
        player.capture()
        assert not player.game_mouse
        client.cursor_visible = False
        player.update_mouse()
        assert player.game_mouse and player.cursor().shape() == Qt.CursorShape.BlankCursor
        client.cursor_visible = True
        player.update_mouse()
        assert not player.game_mouse
        player.set_mouse_mode('remote')
        player.capture()
        assert player.game_mouse
        player.set_mouse_mode('local')
        player.capture()
        assert not player.game_mouse and player.cursor().shape() == Qt.CursorShape.BlankCursor
        assert player.software_cursor
        client.cursor_applied = 'local'
        player.update_mouse()
        assert player.cursor().shape() == Qt.CursorShape.BlankCursor
        client.cursor_visible = False
        client.cursor_updated = time.monotonic()
        player.update_mouse()
        assert player.game_mouse and not player.software_cursor
        # Offscreen runners do not reliably translate OS pointer warps into Qt
        # move events. Deliver the same widget event without moving the OS cursor.
        point = player.rect_image.topLeft() + QPoint(5, 5)
        QApplication.sendEvent(player, QMouseEvent(
            QEvent.Type.MouseMove, QPointF(point), QPointF(player.mapToGlobal(point)),
            Qt.MouseButton.NoButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))
        assert any(e[0] == 'input' and e[1].get('absolute') is False for e in events)
        client.cursor_visible = True
        player.update_mouse()
        QApplication.sendEvent(player, QMouseEvent(
            QEvent.Type.MouseMove, QPointF(point), QPointF(player.mapToGlobal(point)),
            Qt.MouseButton.NoButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))
        assert any(e[0] == 'input' and e[1].get('absolute') is True for e in events)
        client.cursor_updated = time.monotonic() - 3
        player.update_mouse()
        assert player.cursor().shape() == Qt.CursorShape.BlankCursor
        player.release()
        assert player.cursor().shape() != Qt.CursorShape.BlankCursor and not player.software_cursor
        client.cursor_supported = False
        player.open_controls()
        modes = list(player.control_menu.modes.values())
        assert [a.text() for a in modes] == ['智能鼠标', '使用被控端鼠标', '使用主控端鼠标']
        assert [a.isEnabled() for a in modes] == [False, True, False]
    finally:
        player.close()


def test_old_preference_migrates_and_new_mode_persists(tmp_path):
    app, player, _ = make_player()
    player.close()
    # Obtain the application's config filename without touching user settings.
    window = MainWindow(tmp_path, auto_host=False, auto_refresh=False)
    path = window.config_path
    close(window, app)
    path.write_text(json.dumps({'game_mouse': True}), encoding='utf-8')
    window = MainWindow(tmp_path, auto_host=False, auto_refresh=False)
    assert window.mouse_mode.currentData() == 'smart'
    window.update_session_preferences({'mouse_mode': 'local'})
    close(window, app)
    window = MainWindow(tmp_path, auto_host=False, auto_refresh=False)
    try:
        assert window.mouse_mode.currentData() == 'local'
    finally:
        close(window, app)


class FakeKey:
    def __init__(self, key, native_vk, scan=0, modifiers=Qt.KeyboardModifier.NoModifier):
        self._key, self._native_vk, self._scan, self._modifiers = key, native_vk, scan, modifiers

    def key(self):
        return self._key

    def nativeVirtualKey(self):
        return self._native_vk

    def nativeScanCode(self):
        return self._scan

    def modifiers(self):
        return self._modifiers


def test_remote_key_event_separates_navigation_and_keypad_digits():
    assert remote_key_event(FakeKey(Qt.Key.Key_Up, 0x26, 0x48), True) == {
        'type': 'key', 'vk': 0x26, 'scan': 0x48, 'extended': True, 'down': True}
    assert remote_key_event(FakeKey(Qt.Key.Key_2, 0x62, 0x50,
                                    Qt.KeyboardModifier.KeypadModifier), True) == {
        'type': 'key', 'vk': 0x62, 'scan': 0, 'extended': False, 'down': True}
    assert remote_key_event(FakeKey(Qt.Key.Key_Down, 0x28, 0x50,
                                    Qt.KeyboardModifier.KeypadModifier), True) == {
        'type': 'key', 'vk': 0x28, 'scan': 0x50, 'extended': True, 'down': True}


def mouse_event(player, point, kind=QEvent.Type.MouseMove):
    button = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseMove else Qt.MouseButton.LeftButton
    return QMouseEvent(kind, QPointF(point), QPointF(player.mapToGlobal(point)), button, button,
                       Qt.KeyboardModifier.NoModifier)


def test_desktop_pointer_only_releases_outside_video_and_cannot_click_both_sides():
    app, player, events = make_player()
    try:
        player.set_mouse_mode('local')
        player.capture()
        rect = player.rect_image
        QApplication.sendEvent(player, mouse_event(player, rect.bottomRight()))
        assert player.captured and player.software_cursor
        assert player.cursor().shape() == Qt.CursorShape.BlankCursor
        outside = rect.bottomRight() + QPoint(0, 1)
        QApplication.sendEvent(player, mouse_event(player, outside))
        assert not player.captured and not player.software_cursor
        assert events[-1] == ('focus', False)
        events.clear()
        QApplication.sendEvent(player, mouse_event(player, outside, QEvent.Type.MouseButtonPress))
        assert not player.captured and not events, 'black bars must not start remote input'

        player.capture()
        events.clear()
        QApplication.sendEvent(player, mouse_event(player, outside, QEvent.Type.MouseButtonPress))
        assert events == [('focus', False)], 'release input instead of forwarding an outside click'
    finally:
        player.close()


def test_relative_pointer_at_edge_stays_captured_and_releases_native_constraint(monkeypatch):
    app, player, events = make_player()
    released = []
    monkeypatch.setattr(player.cursor_confinement, 'release', lambda: released.append(True))
    try:
        player.set_mouse_mode('remote')
        player.capture()
        QApplication.sendEvent(player, mouse_event(player, player.rect_image.bottomRight()))
        assert player.captured and player.game_mouse
        player.open_controls()
        assert released and not player.captured and not player.raw_mouse.active
    finally:
        player.close()
