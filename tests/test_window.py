import os
import time

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
    finally:
        window.close()
        pump(app, lambda: window._closed)
    assert not window.runtime.thread.is_alive()
    assert window.server.runner is None
    restored = MainWindow(tmp_path, auto_refresh=False)
    assert restored.pad_backend.currentData() == "disabled"
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
