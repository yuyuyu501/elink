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


def test_window_automatic_host_start_stop_and_shutdown(tmp_path):
    app = QApplication.instance() or QApplication([])
    apply_theme(app)
    window = MainWindow(tmp_path, auto_refresh=False)
    window.show()
    app.processEvents()
    assert window.server is None
    window.pad_backend.setCurrentIndex(window.pad_backend.findData("elinkpad"))
    window.toggle_host()
    pump(app, lambda: window.host_toggle.isEnabled())
    assert window.server.runner is not None
    assert window.server.pad_backend == "elinkpad"
    assert not window.pad_backend.isEnabled()
    assert not hasattr(window, "invitation")
    assert not hasattr(window, "devices")
    assert not hasattr(window, "pair_button")
    window.toggle_host()
    pump(app, lambda: window.host_toggle.isEnabled())
    assert window.server.runner is None
    assert window.pad_backend.isEnabled()
    window.pad_backend.setCurrentIndex(window.pad_backend.findData("disabled"))
    window.toggle_host()
    pump(app, lambda: window.host_toggle.isEnabled())
    assert window.server.pad_backend == "disabled"
    window.toggle_host()
    pump(app, lambda: window.host_toggle.isEnabled())
    window.close()
    pump(app, lambda: window._closed)
    assert not window.runtime.thread.is_alive()
    restored = MainWindow(tmp_path, auto_refresh=False)
    assert restored.pad_backend.currentData() == "disabled"
    restored.close()
    pump(app, lambda: restored._closed)
