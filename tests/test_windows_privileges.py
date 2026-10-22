import ctypes
import os
from pathlib import Path
import threading
import sys
from types import SimpleNamespace
import time

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


def test_high_integrity_input_uses_one_elevated_broker(monkeypatch):
    from elink.platforms.windows import input as native, privileges

    def denied(*args):
        ctypes.set_last_error(5)
        return 0

    class Broker:
        instances = []

        def __init__(self, notify):
            self.events = []
            Broker.instances.append(self)

        def emit(self, event):
            self.events.append(dict(event))

        def close(self):
            pass

    monkeypatch.setattr(ctypes, 'WinDLL', lambda *args, **kwargs: SimpleNamespace(SendInput=denied))
    monkeypatch.setattr(privileges, 'is_elevated', lambda: False)
    monkeypatch.setattr('elink.platforms.windows.input_broker.ElevatedInputBroker', Broker)
    sink = native.WindowsInput(lambda event: None)
    monkeypatch.setattr(sink, '_input_context', lambda: {
        'foreground_integrity': 'high', 'thread_desktop': 'Default', 'input_desktop': 'Default'})
    event = dict(type='key', vk=65, scan=0, extended=False, down=True)
    sink.emit(event)
    sink.emit(dict(event, down=False))
    assert len(Broker.instances) == 1
    assert Broker.instances[0].events == [event, dict(event, down=False)]


def test_elevated_input_broker_protocol_rejects_unsupported_events():
    from elink.platforms.windows.input_broker import validate_event

    validate_event(dict(type='move', absolute=False, x=1, y=-2))
    with pytest.raises(ValueError):
        validate_event(dict(type='pad', index=0, values=[0] * 7))
    with pytest.raises(ValueError):
        validate_event(dict(type='move', absolute=True, x=65536, y=0))
    with pytest.raises(ValueError):
        validate_event(dict(type='key', vk=65, scan=0, extended=False, down=True, secret='no'))


def test_start_input_broker_uses_shell_execute_ex_and_returns_child_pid(monkeypatch):
    from elink.platforms.windows import privileges
    calls = {}

    class Function:
        def __init__(self, callback):
            self.callback = callback
        def __call__(self, *args):
            return self.callback(*args)

    def execute(info):
        value = info._obj
        calls['verb'] = value.verb
        calls['file'] = value.file
        calls['parameters'] = value.parameters
        value.process = 77
        return 1

    class Shell:
        ShellExecuteExW = Function(execute)

    class Kernel:
        GetProcessId = Function(lambda handle: 6789)
        CloseHandle = Function(lambda handle: calls.__setitem__('closed', handle))

    def load(name, **kwargs):
        return Shell() if name == 'shell32' else Kernel()

    monkeypatch.setattr(privileges.C, 'WinDLL', load)
    monkeypatch.setattr(sys, 'frozen', False, raising=False)
    assert privileges.start_input_broker(43210, 'test-token') == 6789
    assert calls['verb'] == 'runas'
    assert '--input-broker 43210 test-token' in calls['parameters']
    assert calls['closed'] == 77


def test_elevated_broker_does_not_block_the_input_callback_on_uac():
    from elink.platforms.windows.input_broker import ElevatedInputBroker

    starting, approve = threading.Event(), threading.Event()
    sent = []
    broker = ElevatedInputBroker()
    def start():
        starting.set()
        assert approve.wait(2)
    broker._start = start
    broker._send = sent.append
    first = dict(type='key', vk=65, scan=0, extended=False, down=True)
    second = dict(first, down=False)

    before = time.monotonic()
    broker.emit(first)
    assert time.monotonic() - before < 0.25
    assert starting.wait(1)
    broker.emit(second)
    approve.set()
    deadline = time.monotonic() + 2
    while len(sent) < 2 and time.monotonic() < deadline:
        time.sleep(0.005)
    broker.close()
    assert sent == [first, second]


def test_elevated_broker_coalesces_queued_pointer_motion_without_reordering_keys():
    from elink.platforms.windows.input_broker import ElevatedInputBroker

    starting, approve = threading.Event(), threading.Event()
    sent = []
    broker = ElevatedInputBroker()
    def start():
        starting.set()
        assert approve.wait(2)
    broker._start = start
    broker._send = sent.append
    broker.emit(dict(type='move', absolute=False, x=1, y=1))
    assert starting.wait(1)
    broker.emit(dict(type='move', absolute=False, x=2, y=2))
    down = dict(type='key', vk=65, scan=0, extended=False, down=True)
    broker.emit(down)
    broker.emit(dict(type='move', absolute=False, x=3, y=3))
    broker.emit(dict(type='move', absolute=False, x=4, y=4))
    approve.set()
    deadline = time.monotonic() + 2
    while len(sent) < 3 and time.monotonic() < deadline:
        time.sleep(0.005)
    broker.close()
    assert sent == [dict(type='move', absolute=False, x=2, y=2), down,
                    dict(type='move', absolute=False, x=4, y=4)]


def test_elevated_broker_releases_held_key_on_graceful_close():
    from elink.platforms.windows.input_broker import ElevatedInputBroker

    starting, sent_down = threading.Event(), threading.Event()
    sent = []
    class Connection:
        def close(self):
            pass

    broker = ElevatedInputBroker()
    broker._connection = Connection()
    def start():
        starting.set()
    def send(event):
        sent.append(dict(event))
        if event['type'] == 'key' and event['down']:
            sent_down.set()
    broker._start = start
    broker._send = send
    down = dict(type='key', vk=65, scan=0, extended=False, down=True)
    broker.emit(down)
    assert starting.wait(1) and sent_down.wait(1)
    broker.close()
    assert sent == [down, dict(down, down=False)]


def test_cancelled_elevation_is_reported_once_without_throwing_into_rtc_callback():
    from elink.platforms.windows.input_broker import ElevatedInputBroker

    notified = threading.Event()
    messages = []
    broker = ElevatedInputBroker(lambda message: (messages.append(message), notified.set()))
    def cancelled():
        raise OSError('UAC cancelled')
    broker._start = cancelled
    event = dict(type='move', absolute=True, x=100, y=200)
    broker.emit(event)
    assert notified.wait(1)
    broker.emit(event)
    broker.close()
    assert len(messages) == 1
    assert 'UAC cancelled' in messages[0]


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
