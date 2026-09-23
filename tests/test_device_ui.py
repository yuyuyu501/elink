import asyncio
import collections
import os
import threading

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton

from elink.core.input import RecordingInput
from elink.core.media import AudioOutput
from elink.core.transport import HostServer
from elink.discovery import DiscoveredHost
from elink.ui.controls import StreamOptions
from elink.ui.window import MainWindow
from elink.ui.theme import apply_theme
from test_window import pump
from test_display import FakeDisplay


def close(window, app):
    window.close()
    pump(app, lambda: window._closed)


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


def test_controls_reconnect_real_session_and_cancel_does_not_change_options(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(tmp_path / 'ui', auto_refresh=False, auto_host=False)
    display = FakeDisplay()
    host = HostServer(tmp_path / 'host', synthetic=True, backend_factory=RecordingInput, display_backend=display)
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
        player.set_mouse_mode('local')
        assert window.mouse_mode.currentData() == 'local'
        pump(app, lambda: window.client.cursor_applied == 'local')
        assert host.tracks[0].cursor_mode == 'local'
        player.set_muted(True)
        pump(app, lambda: window.client.muted)
        player.set_statistics(False)
        assert not player.show_statistics
        player.open_quality()
        dialog = player.settings_dialog
        options = dialog.findChild(StreamOptions)
        pump(app, lambda: any(b.isEnabled() and b.text() == '应用并重新连接' for b in dialog.findChildren(QPushButton)))
        options.bitrate.setValue(17)
        assert options.display_resolution.currentData() == [1920, 1080]
        options.display_resolution.setCurrentIndex(options.display_resolution.findData([2560, 1440]))
        dialog.reject()
        assert window.bitrate.value() == 20
        assert not display.changes
        player.open_quality()
        dialog = player.settings_dialog
        options = dialog.findChild(StreamOptions)
        pump(app, lambda: any(b.isEnabled() and b.text() == '应用并重新连接' for b in dialog.findChildren(QPushButton)))
        options.bitrate.setValue(12)
        options.display_refresh.setCurrentIndex(options.display_refresh.findData(144))
        assert options.fps.currentData() == 144
        options.display_refresh.setCurrentIndex(options.display_refresh.findData(60))
        options.display_resolution.setCurrentIndex(options.display_resolution.findData([1280, 720]))
        options.display_refresh.setCurrentIndex(options.display_refresh.findData(60))
        options.scale.setCurrentIndex(options.scale.findData(125))
        session = window.client.session_id
        next(b for b in dialog.findChildren(QPushButton) if b.text() == '应用并重新连接').click()
        pump(app, lambda: window.player is not None and window.player is not player
             and window.player.image is not None, timeout=25)
        assert window.client.session_id != session
        assert display.current == [1280, 720] and display.scale == 125
        assert window.client.stats.target_mbps == 12
        assert window.fps.currentData() == 60
        assert window.player.preferences['bitrate'] == 12
        assert window.player.muted and window.client.muted
        assert not window.player.show_statistics
        assert window.session_address == f'127.0.0.1:{port}'
        window.player.open_quality()
        dialog = window.player.settings_dialog
        options = dialog.findChild(StreamOptions)
        pump(app, lambda: options.display_snapshot is not None)
        assert options.display_resolution.currentData() == [1280, 720]
        assert options.display_refresh.currentData() == 60
        assert options.scale.currentData() == 125
        dialog.reject()
        window.player.close()
        pump(app, lambda: window.player is None and not window.busy)
        assert window.client.pc is None
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
