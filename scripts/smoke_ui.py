"""Render isolated UI previews, with no hosting, discovery or remote input."""
import argparse
import os
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import numpy as np
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QApplication
from elink.core.media import FrameMailbox
from elink.ui.player import Player
from elink.ui.theme import apply_theme
from elink.ui.window import MainWindow
from elink.discovery import DiscoveredHost


def preview_player(app, runtime, root):
    client = SimpleNamespace(pc=object(), mailbox=FrameMailbox(), rtt_ms=0, last_pong=0,
                             cursor_supported=True, focus=lambda _: None, send=lambda _: None)
    view = Player(runtime, client, controllers=False, preferences={'renderer': 'cpu'})
    view.timer.stop()
    view.set_frame_pixels(np.full((720, 1280, 3), (48, 44, 36), dtype=np.uint8))
    view.show()
    view.raise_()
    view.activateWindow()
    try:
        for width, height in ((1120, 760), (640, 400)):
            view.resize(width, height)
            app.processEvents()
            view.open_controls()
            center = view.control_menu
            center.mouse_button.setChecked(True)
            center.toggle_mouse()
            app.processEvents()
            assert view.rect().contains(center.geometry())
            assert view.rect().contains(center.mouse_options.geometry())
            assert center.mouse_options.geometry().right() < center.geometry().left()
            view.grab().save(str(root / f"controls-{width}.png"))
            center.scroll.verticalScrollBar().setValue(center.scroll.verticalScrollBar().maximum())
            app.processEvents()
            view.grab().save(str(root / f"controls-bottom-{width}.png"))
            view.open_controls()
        view.open_quality()
        dialog = view.settings_dialog
        dialog.options.renderer.setCurrentIndex(1)
        dialog.options.render_scale.setCurrentIndex(2)
        app.processEvents()
        if app.platformName() == 'windows':
            from PySide6.QtTest import QTest
            dialog.raise_()
            dialog.activateWindow()
            QTest.qWait(100)
        dialog.grab().save(str(root / 'quality.png'))
        for control in (dialog.options.upscaler, dialog.options.renderer):
            assert control.width() >= control.minimumSizeHint().width()
        dialog.options.decoder.showPopup()
        app.processEvents()
        popup = dialog.options.decoder.view().window()
        popup.grab().save(str(root / 'decoder-options.png'))
        if app.platformName() == 'windows':
            QTest.qWait(100)
            assert popup.isVisible()
            bottom = dialog.options.decoder.mapToGlobal(dialog.options.decoder.rect().bottomLeft()).y()
            assert popup.geometry().top() >= bottom
        # Compose only owned widgets; desktop capture can include an unrelated occluder.
        preview = dialog.grab()
        painter = QPainter(preview)
        painter.drawPixmap(dialog.mapFromGlobal(popup.pos()), popup.grab())
        painter.end()
        preview.save(str(root / 'quality-dropdown.png'))
        dialog.options.decoder.hidePopup()
        dialog.reject()
    finally:
        view.close()


def preview_window(app, root, config):
    with patch('elink.ui.window.socket.gethostname', return_value='ELINK-DESKTOP'):
        window = MainWindow(config, auto_refresh=False, auto_host=False)
    window.timer.stop()
    window.display_hosts([DiscoveredHost("a" * 64, "游戏主机", {"192.0.2.20": "局域网", "100.80.1.20": "Tailscale"}),
                          DiscoveredHost("b" * 64, "客厅电脑", {"192.0.2.21": "局域网"})])
    window.host_status.setText('可连接 · TCP 49200')
    window.session_status.setText('当前无串流连接')
    window.show()
    try:
        for width, height in ((1120, 800), (900, 680)):
            window.resize(width, height)
            for index, name in enumerate(("devices", "settings", "about")):
                window.tabs.setCurrentIndex(index)
                app.processEvents()
                window.grab().save(str(root / f"{name}-{width}.png"))
        window.tabs.setCurrentIndex(1)
        window.advanced.show()
        app.processEvents()
        window.grab().save(str(root / "settings-advanced.png"))
        window.tabs.setCurrentIndex(2)
        window.about.status.setText('正在下载：24.0 / 120.0 MiB · 6.2 MiB/s')
        window.about.bar.setValue(20)
        window.about.bar.show()
        window.about.cancel_button.show()
        app.processEvents()
        window.grab().save(str(root / 'about-download.png'))
        window.tabs.setCurrentIndex(0)
        window.connect_failed("连接失败：对端未响应，请检查设备是否在线。")
        app.processEvents()
        window.grab().save(str(root / "connection-error.png"))
        window.hide()
        preview_player(app, window.runtime, root)
    finally:
        window.request_exit()
        deadline = time.monotonic() + 10
        while not window._closed and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert window._closed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('.artifacts/independent-ui'))
    root = parser.parse_args().output.resolve()
    root.mkdir(parents=True, exist_ok=True)
    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    apply_theme(app)
    with tempfile.TemporaryDirectory(prefix='elink-ui-') as config:
        preview_window(app, root, Path(config))
    print(root)


if __name__ == "__main__":
    main()
