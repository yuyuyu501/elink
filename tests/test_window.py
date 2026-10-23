import os
import sys
import time
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from elink.ui.window import MainWindow
from elink.ui.theme import apply_theme


def pump(app, predicate, timeout=10):
    end = time.monotonic() + timeout
    while not predicate():
        app.processEvents()
        if time.monotonic() > end:
            raise AssertionError("UI timeout")
        time.sleep(0.01)


@pytest.mark.skipif(sys.platform != 'win32', reason="Windows main-window chrome")
def test_main_titlebar_and_sectioned_sidebar_preserve_window_actions(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QLabel, QFrame, QPushButton
    from elink.ui.chrome import MAIN_TITLE_HEIGHT, MAIN_WINDOW_SIZE
    app = QApplication.instance() or QApplication([])
    apply_theme(app)
    window = MainWindow(tmp_path, auto_refresh=False, auto_host=False)
    window.show()
    app.processEvents()
    try:
        assert window.windowFlags() & Qt.WindowType.FramelessWindowHint
        assert window.titlebar.height() == MAIN_TITLE_HEIGHT
        assert window.size().toTuple() == MAIN_WINDOW_SIZE
        assert window.minimumSize().toTuple() == MAIN_WINDOW_SIZE
        assert window.maximumSize().toTuple() == MAIN_WINDOW_SIZE
        assert window.centralWidget().geometry().top() >= MAIN_TITLE_HEIGHT
        assert hasattr(window.titlebar, 'minimize') and hasattr(window.titlebar, 'close_button')
        assert not hasattr(window.titlebar, 'maximize')
        assert len(window.titlebar.findChildren(QPushButton)) == 2
        assert not window.sidebar.findChildren(QLabel)
        assert window.sidebar.width() == 204
        devices, settings, about = window.navigation_buttons
        divider = window.sidebar.findChild(QFrame, 'sidebarSeparator')
        assert devices.geometry().bottom() < about.geometry().top()
        assert about.geometry().bottom() < divider.geometry().top() < settings.geometry().top()
        assert window.sidebar.height() - settings.geometry().bottom() <= 20
        for control, index in ((about, 2), (settings, 1), (devices, 0)):
            control.click()
            assert window.tabs.currentIndex() == index and control.isChecked()
        calls = []
        with monkeypatch.context() as patch:
            patch.setattr(window, 'windowHandle', lambda: SimpleNamespace(startSystemMove=lambda: calls.append('move')))
            QTest.mouseClick(window.titlebar, Qt.MouseButton.LeftButton, pos=QPoint(280, 24))
        assert calls == ['move']
        QTest.mouseDClick(window.titlebar, Qt.MouseButton.LeftButton, pos=QPoint(280, 24))
        app.processEvents()
        assert not window.isMaximized()
        window.titlebar.minimize.click()
        app.processEvents()
        assert window.isMinimized()
        window.showNormal()
        window.titlebar.close_button.click()
        pump(app, lambda: window._closed)
    finally:
        window.request_exit()
        pump(app, lambda: window._closed)


def test_window_automatic_host_start_settings_and_shutdown(tmp_path, monkeypatch):
    from elink.core.transport import HostServer
    original = HostServer.start
    async def local_start(server, bind=None, port=49200):
        return await original(server, bind="127.0.0.1", port=0)
    monkeypatch.setattr(HostServer, "start", local_start)
    app = QApplication.instance() or QApplication([])
    apply_theme(app)
    window = MainWindow(tmp_path, auto_refresh=False)
    window.show()
    try:
        pump(app, lambda: window.server and window.server.runner and not window.host_starting)
        assert not hasattr(window, "host_toggle")
        assert not hasattr(window, "invitation")
        assert "可连接" in window.host_status.text()
        window.pad_backend.setCurrentIndex(window.pad_backend.findData("disabled"))
        window.apply_host_settings()
        pump(app, lambda: not window.host_starting)
        assert window.server.pad_backend == "disabled"
        window.renderer.setCurrentIndex(window.renderer.findData('gpu'))
        window.render_scale.setCurrentIndex(window.render_scale.findData(67))
    finally:
        window.close()
        pump(app, lambda: window._closed)
    assert not window.runtime.thread.is_alive()
    assert window.server.runner is None
    restored = MainWindow(tmp_path, auto_refresh=False)
    assert restored.pad_backend.currentData() == "disabled"
    assert restored.renderer.currentData() == 'gpu'
    assert restored.render_scale.currentData() == 67
    restored.close()
    pump(app, lambda: restored._closed)
    assert restored.server is None  # Closing before the queued start does not start hosting.


def test_port_conflict_reports_and_retries_without_a_toggle(tmp_path, monkeypatch):
    from elink.core.transport import HostServer
    original = HostServer.start
    attempts = []
    async def local_start(server, bind=None, port=49200):
        attempts.append(True)
        if len(attempts) == 1:
            raise OSError("port in use")
        return await original(server, bind="127.0.0.1", port=0)
    monkeypatch.setattr(HostServer, "start", local_start)
    app = QApplication.instance() or QApplication([])
    window = MainWindow(tmp_path, auto_refresh=False)
    try:
        pump(app, lambda: window.host_retry_at > 0)
        assert "暂不可连接" in window.host_status.text()
        window.host_retry_at = 0
        window.refresh_host()
        pump(app, lambda: window.server.runner and not window.host_starting)
        assert "可连接" in window.host_status.text()
    finally:
        window.close()
        pump(app, lambda: window._closed)
