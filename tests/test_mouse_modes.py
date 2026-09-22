import json
import time

from PySide6.QtCore import Qt, QPoint
from PySide6.QtTest import QTest

from test_player_chrome import make_player
from test_device_ui import close
from elink.ui.window import MainWindow


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
        client.cursor_applied = 'local'
        player.update_mouse()
        assert player.cursor().shape() == Qt.CursorShape.ArrowCursor
        QTest.mouseMove(player, player.rect_image.topLeft() + QPoint(5, 5))
        assert any(e[0] == 'input' and e[1].get('absolute') is True for e in events)
        client.cursor_updated = time.monotonic() - 3
        player.update_mouse()
        assert player.cursor().shape() == Qt.CursorShape.BlankCursor
        player.release()
        assert player.cursor().shape() != Qt.CursorShape.BlankCursor
        client.cursor_supported = False
        player.open_controls()
        actions = player.control_menu.actions()
        menu = next(a.menu() for a in actions if a.text() == '鼠标模式')
        assert [a.text() for a in menu.actions()] == ['智能鼠标', '使用被控端鼠标', '使用主控端鼠标']
        assert [a.isEnabled() for a in menu.actions()] == [False, True, False]
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
