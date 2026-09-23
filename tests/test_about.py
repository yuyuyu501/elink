import asyncio
import os
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt, Signal, QObject
from PySide6.QtWidgets import QApplication, QMessageBox

from elink import updates
from elink.ui.player import Player
from elink.ui.window import MainWindow
from test_window import pump


def release():
    return updates.Release("0.5.0", "v0.5.0", "Elink-0.5.0-windows-x64.zip", "", 1024, "", True)


def close(window, app):
    window.close()
    pump(app, lambda: window._closed)
    assert not window.runtime.thread.is_alive()


def test_about_check_channel_error_and_no_network_at_start(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    calls = []
    async def check(preview):
        calls.append(preview)
        return None
    monkeypatch.setattr(updates, "check_release", check)
    window = MainWindow(tmp_path, auto_refresh=False, auto_host=False)
    try:
        assert window.tabs.tabText(3) == "关于"
        app.processEvents()
        assert not calls
        window.about.previews.setChecked(False)
        window.about.check()
        pump(app, lambda: window.about.check_button.isEnabled())
        assert calls == [False]
        assert "最新" in window.about.status.text()
        async def failed(*_):
            raise RuntimeError("HTTP 403")
        monkeypatch.setattr(updates, "check_release", failed)
        window.about.check()
        pump(app, lambda: window.about.check_button.isEnabled())
        assert "HTTP 403" in window.about.status.text()
    finally:
        close(window, app)


def test_declining_update_never_downloads(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(tmp_path, auto_refresh=False, auto_host=False)
    monkeypatch.setattr(updates, "installation_dir", lambda _: tmp_path / "Elink")
    monkeypatch.setattr(QMessageBox, "question", lambda *_: QMessageBox.StandardButton.No)
    def unexpected(*_):
        raise AssertionError("download without consent")
    monkeypatch.setattr(updates, "prepare_update", unexpected)
    try:
        window.about.checked(release())
        assert "暂缓" in window.about.status.text()
        assert window.about.check_button.isEnabled()
    finally:
        close(window, app)


def test_confirm_update_prepares_before_helper_and_graceful_close(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    workspace = tmp_path / "stage"
    workspace.mkdir()
    calls = []
    monkeypatch.setattr(updates, "installation_dir", lambda _: tmp_path / "Elink")
    monkeypatch.setattr(QMessageBox, "question", lambda *_: QMessageBox.StandardButton.Yes)
    async def prepare(new, target, cancelled, progress):
        calls.append("download")
        progress(1, 1)
        return workspace
    def helper(*_):
        assert not (workspace / "armed").exists()
        calls.append("helper")
    monkeypatch.setattr(updates, "prepare_update", prepare)
    monkeypatch.setattr(updates, "launch_helper", helper)
    window = MainWindow(tmp_path / "data", auto_refresh=False, auto_host=False)
    try:
        window.about.checked(release())
        pump(app, lambda: window._closed)
        assert calls == ["download", "helper"]
        assert (workspace / "armed").exists()
        assert (tmp_path / "data/desktop-v3.json").exists()
        assert not window.runtime.thread.is_alive()
    finally:
        if not window._closed:
            close(window, app)


def test_download_can_be_cancelled_without_exiting(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(updates, "installation_dir", lambda _: tmp_path / "Elink")
    monkeypatch.setattr(QMessageBox, "question", lambda *_: QMessageBox.StandardButton.Yes)
    started = []
    async def prepare(*_):
        started.append(True)
        await asyncio.sleep(30)
        raise AssertionError("cancel was not delivered")
    monkeypatch.setattr(updates, "prepare_update", prepare)
    window = MainWindow(tmp_path, auto_refresh=False, auto_host=False)
    try:
        window.about.checked(release())
        pump(app, lambda: bool(started))
        window.about.cancel()
        assert window.about.check_button.isEnabled()
        assert "已取消" in window.about.status.text()
        assert not window._closing
    finally:
        close(window, app)


def test_download_progress_displays_speed(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(tmp_path, auto_refresh=False, auto_host=False)
    try:
        window.about._download_started = time.monotonic() - 2
        window.about.show_progress(window.about.generation, 8 * 1048576, 16 * 1048576)
        assert "MiB/s" in window.about.status.text()
    finally:
        close(window, app)


def test_helper_failure_keeps_app_open_and_never_arms(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    workspace = tmp_path / "stage"
    workspace.mkdir()
    def fail(*_):
        raise RuntimeError("helper failed")
    monkeypatch.setattr(updates, "launch_helper", fail)
    window = MainWindow(tmp_path, auto_refresh=False, auto_host=False)
    try:
        window.about.prepared(workspace, window.about.generation)
        pump(app, lambda: not window.about.installing)
        assert "helper failed" in window.about.status.text()
        assert not (workspace / "armed").exists()
        assert not window._closing
    finally:
        close(window, app)


def test_cancelled_completion_cannot_install_after_a_new_check(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(tmp_path, auto_refresh=False, auto_host=False)
    workspace = tmp_path / "stage"
    workspace.mkdir()
    try:
        old_job = window.about.generation
        window.about.cancel()
        window.about.prepared(workspace, old_job)
        assert not window.about.installing
        assert not workspace.exists()
    finally:
        close(window, app)


def test_captured_desktop_mouse_uses_host_cursor_and_release_restores_local():
    app = QApplication.instance() or QApplication([])
    class Runtime(QObject):
        feedback = Signal(object)
        def call(self, fn, *args):
            fn(*args)
    player = Player(Runtime(), SimpleNamespace(focus=lambda _: None), game_mouse=False, controllers=False)
    try:
        player.capture()
        assert player.cursor().shape() == Qt.CursorShape.BlankCursor
        player.release()
        assert player.cursor().shape() != Qt.CursorShape.BlankCursor
    finally:
        player.close()
