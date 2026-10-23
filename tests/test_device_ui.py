import asyncio
import collections
import os
import threading

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QCheckBox, QPushButton

from elink.core.input import RecordingInput
from elink.core.media import AudioOutput
from elink.core.transport import HostServer
from elink.discovery import DiscoveredHost
from elink.ui.controls import OptionRow, StreamOptions
from elink.ui.window import MainWindow
from elink.ui.theme import apply_theme
from test_window import pump


def close(window, app):
    window.close()
    pump(app, lambda: window._closed)


def test_option_row_label_indicator_keyboard_and_disabled():
    app = QApplication.instance() or QApplication([])
    apply_theme(app)
    checkbox = QCheckBox()
    row = OptionRow('显示连接统计', checkbox, 'chart-column')
    row.resize(320, 50)
    row.show()
    app.processEvents()
    try:
        changes = []
        checkbox.toggled.connect(changes.append)
        QTest.mouseClick(row.label, Qt.MouseButton.LeftButton)
        assert checkbox.isChecked() and changes == [True]
        QTest.mouseClick(checkbox, Qt.MouseButton.LeftButton)
        assert not checkbox.isChecked() and changes == [True, False]
        checkbox.setFocus()
        QTest.keyClick(checkbox, Qt.Key.Key_Space)
        assert checkbox.isChecked() and changes == [True, False, True]
        checkbox.setEnabled(False)
        QTest.mouseClick(row.label, Qt.MouseButton.LeftButton)
        assert changes == [True, False, True]
        assert checkbox.accessibleName() == '显示连接统计'
    finally:
        row.close()


def test_stream_dropdown_opens_below_field_and_commits_keyboard_selection():
    app = QApplication.instance() or QApplication([])
    apply_theme(app)
    options = StreamOptions()
    options.resize(500, 540)
    options.move(40, 40)
    options.show()
    app.processEvents()
    combo = options.decoder
    try:
        combo.showPopup()
        app.processEvents()
        popup = combo.view().window()
        bottom = combo.mapToGlobal(combo.rect().bottomLeft()).y()
        assert popup.geometry().top() >= bottom
        assert popup.width() == combo.width()
        QTest.keyClick(combo.view(), Qt.Key.Key_Down)
        QTest.keyClick(combo.view(), Qt.Key.Key_Return)
        app.processEvents()
        assert combo.currentData() == 'software' and not popup.isVisible()
        combo.showPopup()
        QTest.keyClick(combo.view(), Qt.Key.Key_Down)
        QTest.keyClick(combo.view(), Qt.Key.Key_Escape)
        assert combo.currentData() == 'software'
    finally:
        combo.hidePopup()
        options.close()


def test_error_notice_dismiss_and_redisplay_preserves_log(tmp_path):
    app = QApplication.instance() or QApplication([])
    apply_theme(app)
    window = MainWindow(tmp_path, auto_refresh=False, auto_host=False)
    window.show()
    try:
        window.report_error('connection failed <plain text>\n' * 4)
        app.processEvents()
        assert window.notice.isVisible() and window.notice_close.isVisible()
        assert window.notice.textFormat() == Qt.TextFormat.PlainText
        log = window.log.toPlainText()
        window.notice_close.click()
        assert not window.notice.isVisible() and not window.notice_bar.isVisible()
        assert window.log.toPlainText() == log
        window.report_error('another failure')
        app.processEvents()
        assert window.notice.isVisible() and window.notice.text() == 'another failure'
        assert window.notice.geometry().right() < window.notice_close.geometry().left()
    finally:
        close(window, app)


def test_device_single_click_failure_and_refresh_preserves_busy(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    apply_theme(app)
    window = MainWindow(tmp_path, auto_refresh=False, auto_host=False)
    attempts = []
    async def failed(address, *_):
        attempts.append(address)
        await asyncio.sleep(.05)
        raise OSError('connection refused')
    monkeypatch.setattr(window.client, 'connect', failed)
    host = DiscoveredHost('a' * 64, '游戏电脑', {'127.0.0.1:49200': '局域网'})
    window.show()
    try:
        window.display_hosts([host])
        app.processEvents()
        assert window.device_buttons[0].height() >= 88
        assert not window.log.isVisible()
        window.tabs.setCurrentIndex(1)
        app.processEvents()
        assert not window.options.isVisible()
        window.tabs.setCurrentIndex(0)
        assert all(window.tabs.tabText(i) != '本机被控' for i in range(window.tabs.count()))
        QTest.mouseClick(window.device_buttons[0], Qt.MouseButton.LeftButton)
        assert window.busy
        window.display_hosts([host])
        assert not window.device_buttons[0].isEnabled()
        window.select_host(host)  # Reentry cannot schedule a second connection.
        pump(app, lambda: not window.busy)
        assert len(attempts) == 1
        assert window.notice.isVisible() and 'connection refused' in window.notice.text()
        assert '连接失败' in window.notice.text()
        assert window.device_buttons[0].isEnabled()
        window.display_hosts([])
        assert not window.device_buttons
    finally:
        close(window, app)


def test_controller_only_card_stays_visible_and_disabled(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(tmp_path, auto_refresh=False, auto_host=False)
    attempts = []
    monkeypatch.setattr(window, 'connect_remote', lambda: attempts.append(True))
    phone = DiscoveredHost('c' * 64, 'Phone', {'192.168.0.50': '局域网'}, 'android', ('controller',))
    host = DiscoveredHost('d' * 64, 'Linux', {'192.168.0.129': '局域网'}, 'linux')
    try:
        window.display_hosts([phone, host])
        assert len(window.device_buttons) == 2
        assert '2 台在线设备' in window.discovery_state.text()
        for busy in (True, False):
            window.set_busy(busy)
            assert not window.device_buttons[0].isEnabled()
            assert window.device_buttons[1].isEnabled() is not busy
        window.device_buttons[0].click()
        window.select_host(phone)
        assert attempts == []
        window.device_buttons[1].click()
        assert attempts == [True]
    finally:
        close(window, app)


def test_controls_apply_in_same_session_and_cancel_does_not_change_options(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(tmp_path / 'ui', auto_refresh=False, auto_host=False)
    window.client.play_audio = False
    host = HostServer(tmp_path / 'host', synthetic=True, backend_factory=RecordingInput)
    async def start():
        return await host.start('127.0.0.1', 0)
    future = window.runtime.submit(start())
    pump(app, future.done)
    port = future.result()
    window.controllers.setChecked(False)
    window.audio.setChecked(False)
    window.mouse_mode.setCurrentIndex(window.mouse_mode.findData('smart'))
    window.show()
    try:
        window.display_hosts([DiscoveredHost('b' * 64, '测试主机', {f'127.0.0.1:{port}': '局域网'})])
        window.device_buttons[0].click()
        pump(app, lambda: window.player is not None and window.player.image is not None, timeout=20)
        player = window.player
        player.capture()
        player.open_controls()
        assert not player.captured
        player.control_menu.close()
        mode = 'local' if 'local' in window.client.cursor_modes else 'remote'
        player.set_mouse_mode(mode)
        assert window.mouse_mode.currentData() == mode
        pump(app, lambda: window.client.cursor_applied == mode)
        assert host.tracks[0].cursor_mode == mode
        player.set_muted(True)
        pump(app, lambda: window.client.muted)
        player.set_statistics(False)
        assert not player.show_statistics
        player.open_quality()
        dialog = player.settings_dialog
        options = dialog.findChild(StreamOptions)
        pump(app, lambda: any(b.isEnabled() and b.text() == '应用' for b in dialog.findChildren(QPushButton)))
        options.bitrate.setValue(17)
        assert options.resolution.currentData() == [1920, 1080]
        options.resolution.setCurrentIndex(options.resolution.findData([2560, 1440]))
        dialog.reject()
        assert window.bitrate.value() == 20
        assert window.resolution.currentData() == [1920, 1080]
        player.open_quality()
        dialog = player.settings_dialog
        options = dialog.findChild(StreamOptions)
        pump(app, lambda: any(b.isEnabled() and b.text() == '应用' for b in dialog.findChildren(QPushButton)))
        options.bitrate.setValue(12)
        options.resolution.setCurrentIndex(options.resolution.findData([3840, 2160]))
        options.fps.setCurrentIndex(options.fps.findData(120))
        dialog.apply_button.click()
        assert player.settings_dialog is dialog and window.player is player
        assert window.bitrate.value() == 20  # Invalid settings don't disconnect or persist.
        options.fps.setCurrentIndex(options.fps.findData(144))
        assert options.fps.currentData() == 144
        options.resolution.setCurrentIndex(options.resolution.findData([1280, 720]))
        options.fps.setCurrentIndex(options.fps.findData(90))
        options.decoder.setCurrentIndex(options.decoder.findData('software'))
        options.audio.setChecked(True)
        session = window.client.session_id
        client_pc, host_pc = window.client.pc, host.pc
        control, motion = window.client.control, window.client.motion
        input_session = host.input
        video, audio = host.tracks
        previous_pts = video.last_pts
        dialog.apply_button.click()
        assert not dialog.apply_button.isEnabled()
        dialog.reject()  # Escape cannot dismiss a pending operation.
        assert player.settings_dialog is dialog
        pump(app, lambda: player.settings_dialog is None and player.image.width() == 1280, timeout=25)
        assert window.player is player
        assert window.client.session_id == session == host.session_id
        assert window.client.pc is client_pc and host.pc is host_pc
        assert window.client.control is control and window.client.motion is motion
        assert host.input is input_session and host.tracks == [video, audio]
        assert video.last_pts > previous_pts
        assert video.fps == 90 and video.network_budget.target_bps == 12_000_000
        encoder = next(sender for sender in host.pc.getSenders() if sender.kind == 'video')._RTCRtpSender__encoder
        assert encoder.policy.fps == encoder.codec.framerate == 90
        assert window.client.decoder == 'software' and audio.enabled.is_set()
        assert window.player.image.width() == 1280 and window.player.image.height() == 720
        assert window.resolution.currentData() == [1280, 720]
        assert window.client.stats.target_mbps == 12
        assert window.fps.currentData() == 90
        assert window.player.preferences['bitrate'] == 12
        assert window.player.muted and window.client.muted
        assert not window.player.show_statistics
        assert window.session_address == f'127.0.0.1:{port}'
        window.player.open_quality()
        dialog = window.player.settings_dialog
        options = dialog.findChild(StreamOptions)
        assert options.resolution.currentData() == [1280, 720]
        assert options.fps.currentData() == 90
        assert not hasattr(options, 'scale')
        # A host-side rejection leaves the dialog, preferences and session usable.
        configure = video.reconfigure
        async def rejected(*args):
            raise RuntimeError('test capture failure')
        monkeypatch.setattr(video, 'reconfigure', rejected)
        options.resolution.setCurrentIndex(options.resolution.findData([1600, 900]))
        dialog.apply_button.click()
        pump(app, lambda: not player.quality_applying)
        assert player.settings_dialog is dialog and dialog.note.isVisible()
        assert 'test capture failure' in dialog.note.text()
        assert player.preferences['resolution'] == [1280, 720]
        assert window.resolution.currentData() == [1280, 720]
        monkeypatch.setattr(video, 'reconfigure', configure)
        options.resolution.setCurrentIndex(options.resolution.findData([1280, 720]))
        options.fps.setCurrentIndex(options.fps.findData(30))
        options.decoder.setCurrentIndex(options.decoder.findData('auto'))
        options.audio.setChecked(False)
        options.bitrate.setValue(8)
        dialog.apply_button.click()
        pump(app, lambda: player.settings_dialog is None)
        assert window.client.decoder == 'auto' and not audio.enabled.is_set()
        assert video.fps == 30 and video.network_budget.target_bps == 8_000_000
        pump(app, lambda: encoder.codec is not None and encoder.codec.framerate == 30)
        assert window.client.session_id == session and window.player is player
        assert window.client.pc is client_pc and host.pc is host_pc
        window.player.close()
        pump(app, lambda: window.player is None and not window.busy)
        assert window.client.pc is None
        import json
        assert json.loads(window.config_path.read_text(encoding='utf-8'))['stream_resolution'] == [1280, 720]
        window.resolution.setCurrentIndex(0)
        window.load_config()
        assert window.resolution.currentData() == [1280, 720]
    finally:
        stopped = window.runtime.submit(host.stop())
        pump(app, stopped.done)
        stopped.result()
        close(window, app)


def test_audio_mute_consumes_samples_without_replaying_them():
    output = AudioOutput.__new__(AudioOutput)
    output.lock = threading.Lock()
    output.frames = collections.deque([np.ones((4, 2), dtype=np.float32)])
    output.remainder = np.empty((0, 2), dtype=np.float32)
    output.muted = True
    samples = np.empty((4, 2), dtype=np.float32)
    output._callback(samples, 4, None, None)
    assert not samples.any() and not output.frames and not len(output.remainder)
    output.muted = False
    output.frames.append(np.full((4, 2), .5, dtype=np.float32))
    output._callback(samples, 4, None, None)
    assert np.all(samples == .5)
