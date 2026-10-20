import json
import math

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QCursor
from PySide6.QtTest import QTest

from elink.core.mouse import MouseSensitivity, sensitivity_value
from elink.ui.window import MainWindow
from test_device_ui import close
from test_player_chrome import make_player


def test_sensitivity_counts_preserve_slow_motion_and_direction():
    normal = MouseSensitivity()
    assert normal.relative(17, -13) == (17, -13)
    slow = MouseSensitivity(-5)
    counts = [slow.relative(1, -1) for _ in range(100)]
    assert tuple(map(sum, zip(*counts))) == (50, -50)
    assert MouseSensitivity(5).relative(17, -13) == (34, -26)
    for offset in (-4.9, -1.2, 0.1, 3.7):
        mapping = MouseSensitivity(offset)
        counts = [mapping.relative(3, -2) for _ in range(100)]
        totals = tuple(map(sum, zip(*counts)))
        assert abs(totals[0] - 300 * mapping.factor) < 1
        assert abs(totals[1] + 200 * mapping.factor) < 1
    assert sensitivity_value(float('nan')) == 0
    assert sensitivity_value(None) == 0
    assert sensitivity_value(8) == 5
    assert sensitivity_value(-8) == -5
    assert sensitivity_value(1.24) == 1.2
    assert MouseSensitivity(5).relative(30000, -30000) == (32767, -32767)


def test_relative_native_and_fallback_input_use_same_sensitivity(monkeypatch):
    app, player, events = make_player()
    try:
        player.set_mouse_sensitivity(-5)
        player.send_relative_motion(1, -1)
        player.send_relative_motion(1, -1)
        moves = [value for kind, value in events if kind == 'input']
        assert moves == [dict(type='move', absolute=False, x=1, y=-1)]
        events.clear()
        player.captured = player.game_mouse = True
        monkeypatch.setattr(player.raw_mouse, 'delta', lambda _: (10, -4))
        assert player.nativeEvent(b'windows_generic_MSG', 1) == (True, 0)
        assert events[-1] == ('input', dict(type='move', absolute=False, x=5, y=-2))
    finally:
        player.close()


def test_desktop_sensitivity_tracks_fractional_motion_and_reaches_edges(monkeypatch):
    app, player, _ = make_player()
    warps = []
    monkeypatch.setattr(QCursor, 'setPos', lambda point: warps.append(point))
    try:
        origin = QPointF(player.rect_image.center())
        player.set_mouse_sensitivity(-5)
        assert player.absolute_position(origin) == origin
        first = player.absolute_position(origin + QPointF(1, 0))
        assert first.x() == origin.x() + .5
        # A synthetic warp event must not be scaled for a second time.
        assert player.absolute_position(QPointF(first.toPoint())) == first
        next_sample = QPointF(first.toPoint()) + QPointF(1, 0)
        second = player.absolute_position(next_sample)
        assert second.x() == origin.x() + 1
        player.set_mouse_sensitivity(5)
        player.absolute_position(origin)
        assert player.absolute_position(origin + QPointF(10, -4)) == origin + QPointF(20, -8)
        point = player.absolute_position(origin + QPointF(100000, -100000))
        assert point == QPointF(player.rect_image.topRight())
        assert warps
        player.set_mouse_sensitivity(0)
        assert player.absolute_position(origin + QPointF(5, 3)) == origin + QPointF(5, 3)
    finally:
        player.close()


def test_control_center_keeps_choices_visible_and_f8_works_in_spinbox():
    app, player, events = make_player()
    player.client.cursor_supported = True
    player.client.set_muted = lambda value: None
    try:
        player.controls_button.click()
        center = player.control_menu
        center.mouse_button.click()
        app.processEvents()
        assert center.mouse_options.isVisible()
        assert (center.sensitivity.minimum(), center.sensitivity.maximum()) == (-5, 5)
        assert center.sensitivity.singleStep() == .1 and center.sensitivity.decimals() == 1
        center.modes['local'].click()
        assert player.mouse_mode == 'local' and center.modes['local'].isChecked()
        center.muted.click()
        assert player.muted and center.muted.isChecked()
        center.statistics.click()
        assert not player.show_statistics and not center.statistics.isChecked()
        center.sensitivity.setValue(1.7)
        assert player.preferences['mouse_sensitivity'] == 1.7
        assert center.isVisible()
        QTest.mouseClick(player, Qt.MouseButton.LeftButton, pos=QPoint(10, 100))
        assert center.isVisible() and not player.captured
        center.sensitivity.setFocus()
        QTest.keyClick(center.sensitivity, Qt.Key.Key_F8)
        app.processEvents()
        assert not center.isVisible() and not player.captured
        player.controls_button.click()
        assert player.control_menu is center and center.isVisible()
        player.controls_button.click()
        assert not center.isVisible()
        player.open_controls()
        center.quality.click()
        app.processEvents()
        assert center.isVisible() and player.settings_dialog.isVisible()
        player.settings_dialog.reject()
        app.processEvents()
        assert center.isVisible()
        player.resize(640, 400)
        app.processEvents()
        assert player.rect().contains(center.geometry())
        assert center.scroll.verticalScrollBar().maximum() > 0
        assert not any(kind == 'input' for kind, _ in events)
    finally:
        player.close()


def test_sensitivity_persists_without_changing_stream_settings(tmp_path):
    app, player, _ = make_player()
    player.close()
    window = MainWindow(tmp_path, auto_host=False, auto_refresh=False)
    try:
        window.update_session_preferences({'mouse_sensitivity': -2.3})
        assert window.options.values()['mouse_sensitivity'] == -2.3
        config = json.loads(window.config_path.read_text(encoding='utf-8'))
        assert config['mouse_sensitivity'] == -2.3
        assert config['stream_resolution'] == [1920, 1080]
    finally:
        close(window, app)
    restored = MainWindow(tmp_path, auto_host=False, auto_refresh=False)
    try:
        assert restored.mouse_sensitivity.value() == -2.3
        assert math.isclose(MouseSensitivity(-2.3).factor, 2 ** (-2.3 / 5))
    finally:
        close(restored, app)
