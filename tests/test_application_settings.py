import json
import os
from pathlib import Path
import sys
import subprocess
import textwrap
import uuid

import pytest
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from elink import startup
from elink.ui.window import MainWindow
from test_window import pump


def test_startup_command_quotes_current_portable_executable(monkeypatch, tmp_path):
    executable = tmp_path / 'portable application' / 'Elink.exe'
    monkeypatch.setattr(sys, 'executable', str(executable))
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    assert startup.startup_command() == f'"{executable.resolve()}" --startup'


def test_source_startup_uses_windowed_python_and_absolute_entrypoint(monkeypatch, tmp_path):
    monkeypatch.delattr(sys, 'frozen', raising=False)
    monkeypatch.setattr(sys, 'executable', str(tmp_path / 'python.exe'))
    (tmp_path / 'pythonw.exe').touch()
    command = startup.startup_command()
    assert str(tmp_path / 'pythonw.exe') in command
    assert str(Path(startup.__file__).resolve().parents[1] / 'main.py') in command
    assert command.endswith(' --startup')


@pytest.mark.skipif(os.name != 'nt', reason='Windows registry')
def test_registry_registration_roundtrip_in_isolated_test_key(monkeypatch):
    import winreg
    key_path = 'Software\\ElinkTest-' + uuid.uuid4().hex
    monkeypatch.setattr(startup, 'RUN_KEY', key_path)
    try:
        startup.set_enabled(True)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            assert winreg.QueryValueEx(key, 'Elink') == (startup.startup_command(), winreg.REG_SZ)
        startup.set_enabled(False)
        startup.set_enabled(False)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            with pytest.raises(FileNotFoundError):
                winreg.QueryValueEx(key, 'Elink')
    finally:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)


def test_general_settings_persist_and_registration_failures_are_visible(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    registrations = []
    monkeypatch.setattr(startup, 'set_enabled', registrations.append)
    window = MainWindow(tmp_path, auto_host=False, auto_refresh=False)
    try:
        assert not window.startup_enabled.isChecked() and not window.close_to_tray.isChecked()
        window.startup_enabled.click()
        window.close_to_tray.click()
        assert registrations == [True]
        config = json.loads(window.config_path.read_text(encoding='utf-8'))
        assert config['startup_enabled'] and config['close_to_tray']
        def denied(enabled):
            raise PermissionError('test registry denied')
        monkeypatch.setattr(startup, 'set_enabled', denied)
        window.startup_enabled.click()
        assert window.startup_enabled.isChecked()
        assert 'test registry denied' in window.notice.text()
    finally:
        window.request_exit()
        pump(app, lambda: window._closed)
    monkeypatch.setattr(startup, 'set_enabled', registrations.append)
    restored = MainWindow(tmp_path, auto_host=False, auto_refresh=False)
    try:
        assert restored.startup_enabled.isChecked() and restored.close_to_tray.isChecked()
        assert registrations == [True, True]  # Portable path refreshed on startup.
        restored.startup_enabled.click()
        assert registrations[-1] is False
    finally:
        restored.request_exit()
        pump(app, lambda: restored._closed)


def test_tray_close_preserves_runtime_and_restore_then_exit_stops_it(tmp_path, monkeypatch):
    from elink.core.input import RecordingInput
    from elink.core.transport import HostServer
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(QSystemTrayIcon, 'isSystemTrayAvailable', lambda: True)
    window = MainWindow(tmp_path, auto_host=False, auto_refresh=False)
    client = window.client
    client.play_audio = False
    window.server = HostServer(tmp_path / 'host', synthetic=True, backend_factory=RecordingInput)
    async def connect():
        await window.server.start('127.0.0.1', 0)
        await client.connect(f'127.0.0.1:{window.server.port}', dict(width=1280, height=720, fps=30, audio=False))
    window.show()
    try:
        future = window.runtime.submit(connect())
        pump(app, future.done)
        future.result()
        host_pc = window.server.pc
        window.close_to_tray.setChecked(True)
        window.close()
        app.processEvents()
        assert not window.isVisible() and not window._closing
        assert window.runtime.thread.is_alive() and window.timer.isActive()
        assert window.server.pc is host_pc and host_pc.connectionState != 'closed'
        assert window.server.runner is not None
        assert window.tray.isVisible()
        window.tray_activated(QSystemTrayIcon.ActivationReason.Trigger)
        assert window.isVisible()
        window.close()
        window.tray.contextMenu().actions()[-1].trigger()
        pump(app, lambda: window._closed)
        assert not window.runtime.thread.is_alive() and not window.tray.isVisible()
    finally:
        if not window._closed:
            future = window.runtime.submit(client.disconnect())
            pump(app, future.done)
            future.result()
        window.request_exit()
        pump(app, lambda: window._closed)
        # The explicit tray exit also closes the remote connection.
        assert window.server.pc is None and window.server.runner is None


def test_startup_hidden_only_when_tray_is_available_and_updates_exit(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(QSystemTrayIcon, 'isSystemTrayAvailable', lambda: True)
    window = MainWindow(tmp_path, auto_host=False, auto_refresh=False)
    try:
        window.show_startup()
        assert not window.isVisible() and window.tray.isVisible()
        window.close_to_tray.setChecked(True)
        window.about.workspace = tmp_path
        window.about.helper_started(None)
        pump(app, lambda: window._closed)
        assert (tmp_path / 'armed').is_file()
    finally:
        window.request_exit()
        pump(app, lambda: window._closed)


def test_startup_without_tray_shows_window_and_close_exits(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(QSystemTrayIcon, 'isSystemTrayAvailable', lambda: False)
    window = MainWindow(tmp_path, auto_host=False, auto_refresh=False)
    try:
        window.show_startup()
        assert window.isVisible() and window.tray is None
        window.close_to_tray.setChecked(True)
        window.close()
        pump(app, lambda: window._closed)
    finally:
        window.request_exit()
        pump(app, lambda: window._closed)


@pytest.mark.skipif(os.name != 'nt', reason='Windows application entrypoint')
def test_hidden_startup_tray_exit_terminates_the_real_application_loop(tmp_path):
    program = textwrap.dedent('''
        import json
        import sys
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication, QSystemTrayIcon
        from elink import app as entry
        original = entry.MainWindow
        windows, expired = [], []
        def create(root):
            window = original(root, auto_host=False, auto_refresh=False)
            window.close_to_tray.setChecked(True)
            windows.append(window)
            QTimer.singleShot(50, window.request_exit)
            def timeout():
                expired.append(True)
                QApplication.instance().quit()
            QTimer.singleShot(3000, timeout)
            return window
        entry.MainWindow = create
        QSystemTrayIcon.isSystemTrayAvailable = lambda: True
        sys.argv = ['Elink', '--startup']
        result = entry.main()
        window = windows[0]
        print(json.dumps(dict(result=result, watchdog=bool(expired),
                              closed=window._closed, alive=window.runtime.thread.is_alive())))
    ''')
    result = subprocess.run([sys.executable, '-c', program],
                            env=dict(os.environ, ELINK_DATA_DIR=str(tmp_path), QT_QPA_PLATFORM='offscreen'),
                            capture_output=True, text=True, timeout=15, check=True)
    assert json.loads(result.stdout) == dict(result=0, watchdog=False, closed=True, alive=False)
