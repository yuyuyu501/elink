import ctypes
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

from elink.ui.window import MainWindow
from test_window import pump


pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows UIPI and elevation')


def test_elevation_relaunch_quotes_paths_and_preserves_the_data_directory(tmp_path, monkeypatch):
    from elink.platforms.windows import privileges
    calls = []

    def execute(*args):
        calls.append(args)
        return 42

    monkeypatch.setattr(ctypes, 'WinDLL', lambda *args, **kwargs: SimpleNamespace(ShellExecuteW=execute))
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(tmp_path / 'Program Files' / 'Elink.exe'))
    root = tmp_path / 'user data'
    privileges.restart_elevated(root)
    assert calls[0][1] == 'runas'
    assert calls[0][2] == str(Path(sys.executable).resolve())
    assert '--elevated-restart ' + str(os.getpid()) in calls[0][3]
    assert '"' + str(root.resolve()) + '"' in calls[0][3]


def test_input_denial_logs_safe_foreground_and_desktop_context(monkeypatch, caplog):
    from elink.platforms.windows import input as native, privileges

    def denied(*args):
        ctypes.set_last_error(5)
        return 0

    monkeypatch.setattr(ctypes, 'WinDLL', lambda *args, **kwargs: SimpleNamespace(SendInput=denied))
    monkeypatch.setattr(privileges, 'is_elevated', lambda: False)
    sink = native.WindowsInput(lambda event: None)
    context = {'pid': 123, 'elevated': False, 'foreground_pid': 456,
               'thread_desktop': 'Default', 'input_desktop': 'Default',
               'input_desktop_error': None}
    monkeypatch.setattr(sink, '_input_context', lambda: context)
    with pytest.raises(RuntimeError, match='Win32=5') as failure:
        sink.emit(dict(type='move', absolute=True, x=0, y=0))
    assert 'SendInput' in str(failure.value) and 'foreground_pid' not in str(failure.value)
    assert '被控端' in str(failure.value) and '任务管理器' in str(failure.value)
    assert 'SendInput failed: kind=move win32=5' in caplog.text
    assert 'foreground_pid' in caplog.text and 'Default' in caplog.text


def test_cancelled_admin_restart_does_not_close_the_current_instance(tmp_path, monkeypatch):
    from elink.platforms.windows import privileges
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(privileges, 'is_elevated', lambda: False)
    monkeypatch.setattr(QMessageBox, 'question', lambda *args: QMessageBox.StandardButton.Yes)

    def cancelled(root):
        raise OSError('UAC cancelled in test')

    monkeypatch.setattr(privileges, 'restart_elevated', cancelled)
    window = MainWindow(tmp_path, auto_host=False, auto_refresh=False)
    try:
        assert window.admin_button.isEnabled()
        window.admin_button.click()
        assert 'UAC cancelled' in window.notice.text()
        assert not window._closing and window.runtime.thread.is_alive()
    finally:
        window.request_exit()
        pump(app, lambda: window._closed)
