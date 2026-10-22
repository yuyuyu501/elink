"""A small elevated input broker used for high-integrity desktop windows.

The broker is deliberately a separate process.  A medium-integrity Elink
process cannot inject into a high-integrity target because of UIPI, while an
interactive elevated process can.  The local socket is authenticated with a
random one-use token and carries only the already validated input event.
"""
from __future__ import annotations

import json
import logging
import os
from collections import deque
import secrets
import socket
import struct
import threading
import time
import ctypes as C
from ctypes import wintypes as W


log = logging.getLogger(__name__)
MAX_EVENT_BYTES = 2048
MAX_QUEUED_EVENTS = 8192


def connection_process_id(connection):
    """Identify the process owning the TCP client endpoint on Windows."""
    if os.name != 'nt':
        raise OSError('Windows TCP process lookup is unavailable')

    class TcpRowOwnerPid(C.Structure):
        _fields_ = [('state', W.DWORD), ('local_address', W.DWORD), ('local_port', W.DWORD),
                    ('remote_address', W.DWORD), ('remote_port', W.DWORD), ('pid', W.DWORD)]

    iphlp = C.WinDLL('iphlpapi', use_last_error=True)
    get_table = iphlp.GetExtendedTcpTable
    get_table.argtypes = [C.c_void_p, C.POINTER(W.DWORD), W.BOOL, W.ULONG, C.c_int, W.ULONG]
    get_table.restype = W.DWORD
    size = W.DWORD()
    status = get_table(None, C.byref(size), False, socket.AF_INET, 4, 0)
    if status != 122 or not size.value:
        raise OSError(status, 'Cannot query Windows TCP ownership table')
    buffer = (C.c_ubyte * size.value)()
    status = get_table(buffer, C.byref(size), False, socket.AF_INET, 4, 0)
    if status:
        raise OSError(status, 'Cannot read Windows TCP ownership table')
    count = C.cast(buffer, C.POINTER(W.DWORD)).contents.value
    rows = C.cast(C.byref(buffer, C.sizeof(W.DWORD)), C.POINTER(TcpRowOwnerPid))
    local, peer = connection.getsockname(), connection.getpeername()

    def address_candidates(value):
        raw = socket.inet_aton(value)
        little = int.from_bytes(raw, 'little')
        network = int.from_bytes(raw, 'big')
        return {little, network}

    def port_candidates(value):
        raw = struct.pack('!H', value)
        little = int.from_bytes(raw, 'little')
        network = int.from_bytes(raw, 'big')
        return {little, network, value}

    for index in range(count):
        row = rows[index]
        # The peer's client-side TCP row has the accepted socket's endpoints reversed.
        if (row.local_address in address_candidates(peer[0]) and row.local_port in port_candidates(peer[1])
                and row.remote_address in address_candidates(local[0]) and row.remote_port in port_candidates(local[1])):
            return row.pid
    raise OSError('Accepted TCP connection has no owning process row')


def validate_event(event):
    """Validate the narrow event set accepted by the elevated process."""
    if not isinstance(event, dict) or event.get('type') not in ('key', 'move', 'button', 'wheel'):
        raise ValueError('unsupported elevated input event')
    kind = event['type']
    common = {'type', 'epoch', 'seq'}
    fields = {
        'key': {'vk', 'scan', 'extended', 'down'},
        'move': {'absolute', 'x', 'y'},
        'button': {'button', 'down'},
        'wheel': {'delta'},
    }[kind]
    if event.keys() - (common | fields) or not fields.issubset(event):
        raise ValueError('invalid elevated input fields')
    for name in ('epoch', 'seq'):
        if name in event and (type(event[name]) is not int or not 0 <= event[name] <= 2**53):
            raise ValueError('invalid elevated input sequence')
    integer = lambda name, low, high: type(event.get(name)) is int and low <= event[name] <= high
    if kind == 'key':
        valid = (integer('vk', 1, 255) and integer('scan', 0, 255)
                 and type(event['extended']) is bool and type(event['down']) is bool)
    elif kind == 'move':
        absolute = event['absolute']
        low, high = (0, 65535) if absolute is True else (-32767, 32767)
        valid = (type(absolute) is bool and integer('x', low, high) and integer('y', low, high))
    elif kind == 'button':
        valid = event['button'] in ('left', 'right', 'middle') and type(event['down']) is bool
    else:
        valid = integer('delta', -12000, 12000)
    if not valid:
        raise ValueError('invalid elevated input value')


class ElevatedInputBroker:
    """Lazy client for an elevated broker process."""

    def __init__(self, notify=lambda message: None):
        self.notify = notify
        self._listener = None
        self._connection = None
        self._reader = None
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._failure = None
        self._events = deque()
        self._worker = None
        self._closed = threading.Event()

    @property
    def active(self):
        return self._connection is not None

    def _start(self):
        from .privileges import start_input_broker

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(35.0)
        token = secrets.token_urlsafe(32)
        self._listener = listener
        log.info('Requesting elevated input broker on loopback port %s', listener.getsockname()[1])
        self.notify("正在请求管理员权限以控制高权限窗口，请确认 Windows 提示。")
        try:
            expected_pid = start_input_broker(listener.getsockname()[1], token)
            log.info('Elevated input broker process started: pid=%s', expected_pid)
            deadline = time.monotonic() + 35.0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('管理员输入辅助连接超时。')
                listener.settimeout(remaining)
                connection, _ = listener.accept()
                try:
                    if connection_process_id(connection) != expected_pid:
                        log.warning('Rejected unexpected input broker TCP peer')
                        connection.close()
                        continue
                    connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    connection.settimeout(5.0)
                    reader = connection.makefile("rb")
                    hello = reader.readline(256).decode("ascii", "replace").strip()
                    if hello != token:
                        log.error('Elevated input broker authentication failed')
                        reader.close()
                        connection.close()
                        continue
                    self._connection, self._reader = connection, reader
                    break
                except Exception:
                    connection.close()
                    raise
            log.info("Elevated input broker connected")
            self.notify("管理员输入辅助已连接。")
        except (OSError, TimeoutError) as exc:
            log.exception('Elevated input broker connection failed: %s', type(exc).__name__)
            raise RuntimeError("管理员输入辅助未连接，可能是 UAC 未批准或启动失败。") from exc
        finally:
            listener.close()
            self._listener = None

    def _send(self, event):
        payload = (json.dumps(event, separators=(",", ":"), ensure_ascii=True) + "\n").encode("utf-8")
        self._connection.sendall(payload)
        line = self._reader.readline(MAX_EVENT_BYTES + 1)
        if not line:
            raise ConnectionError("管理员输入辅助已断开。")
        result = json.loads(line.decode("utf-8"))
        if result.get("ok") is not True:
            raise RuntimeError(result.get("error") or "管理员输入辅助拒绝了事件。")

    def _run(self):
        pressed_keys, pressed_buttons = {}, set()
        try:
            self._start()
            while True:
                with self._condition:
                    while not self._events and not self._closed.is_set() and not self._failure:
                        self._condition.wait(timeout=0.25)
                    if self._failure or (self._closed.is_set() and not self._events):
                        break
                    event = self._events.popleft()
                self._send(event)
                if event["type"] == "key":
                    key = (event["vk"], event["scan"], event["extended"])
                    if event["down"]:
                        pressed_keys[key] = dict(event, down=False)
                    else:
                        pressed_keys.pop(key, None)
                elif event["type"] == "button":
                    if event["down"]:
                        pressed_buttons.add(event["button"])
                    else:
                        pressed_buttons.discard(event["button"])
        except Exception as exc:
            with self._lock:
                self._failure = f"管理员输入辅助失败：{exc}"
            if not self._closed.is_set():
                log.exception("Elevated input broker worker failed")
                try:
                    self.notify(self._failure)
                except Exception:
                    log.exception("Elevated input broker failure notification failed")
        finally:
            if self._connection is not None and (pressed_keys or pressed_buttons):
                for event in list(pressed_keys.values()) + [
                        {"type": "button", "button": button, "down": False}
                        for button in pressed_buttons]:
                    try:
                        self._send(event)
                    except Exception:
                        log.exception("Could not release elevated input after broker failure")
            self._close_sockets()

    def emit(self, event):
        """Queue validated input without blocking the RTC callback on UAC or IPC."""
        with self._condition:
            if self._failure or self._closed.is_set():
                return
            try:
                validate_event(event)
                event = dict(event)
                if event['type'] == 'move' and self._events and self._events[-1]['type'] == 'move':
                    self._events[-1] = event
                else:
                    if len(self._events) >= MAX_QUEUED_EVENTS:
                        if event['type'] == 'move':
                            stale_move = next((index for index, queued in enumerate(self._events)
                                               if queued['type'] == 'move'), None)
                            if stale_move is None:
                                log.debug('Dropping elevated pointer update while input queue is full')
                                return
                            del self._events[stale_move]
                        else:
                            raise OverflowError('管理员输入辅助队列已满')
                    self._events.append(event)
                self._condition.notify()
            except (ValueError, OverflowError) as exc:
                self._failure = f"管理员输入辅助队列拒绝事件：{exc}"
                log.error("Elevated input broker queue rejected an event: %s", exc)
                self._condition.notify_all()
                try:
                    self.notify(self._failure)
                except Exception:
                    log.exception("Elevated input broker failure notification failed")
                return
            if self._worker is None:
                self._worker = threading.Thread(target=self._run, name="ElinkInputBroker", daemon=True)
                self._worker.start()

    def _close_sockets(self):
        listener, reader, connection = self._listener, self._reader, self._connection
        self._listener = self._reader = self._connection = None
        for item in (reader, connection, listener):
            if item:
                try:
                    item.close()
                except OSError:
                    pass

    def close(self):
        with self._condition:
            self._closed.set()
            pending_start = self._connection is None
            pending_count = len(self._events)
            if pending_start:
                self._events.clear()
            self._condition.notify_all()
            listener = self._listener if pending_start else None
        if pending_count:
            log.info('Closing elevated input broker with %s queued events', pending_count)
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=3.0)
            if worker.is_alive():
                log.warning('Elevated input broker did not stop gracefully; closing its socket')
                self._close_sockets()
                worker.join(timeout=1.0)


def run_input_broker(port: int, token: str) -> int:
    """Entry point for the elevated child process."""
    if os.name != 'nt':
        return 2
    from ...logging_setup import configure_logging
    from ...paths import data_dir, ensure_dirs
    from .input import WindowsInput

    if not token or not (1 <= int(port) <= 65535):
        return 2
    root = data_dir()
    ensure_dirs(root)
    configure_logging(root, f'input-broker-{os.getpid()}.log')
    log.info('Elevated input broker starting')
    connection = reader = sink = None
    try:
        connection = socket.create_connection(("127.0.0.1", int(port)), timeout=15.0)
        connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        connection.settimeout(None)
        connection.sendall((token + "\n").encode("ascii"))
        reader = connection.makefile("rb")
        sink = WindowsInput(lambda _event: None, "disabled", allow_broker=False)
        while True:
            line = reader.readline(MAX_EVENT_BYTES + 1)
            if not line:
                break
            try:
                event = json.loads(line.decode("utf-8"))
                if len(line) > MAX_EVENT_BYTES:
                    raise ValueError('elevated input message too large')
                validate_event(event)
                sink.emit(event)
                result = {"ok": True}
            except Exception as exc:
                log.exception("Elevated input event failed")
                result = {"ok": False, "error": str(exc)[:512]}
            connection.sendall((json.dumps(result, ensure_ascii=False) + "\n").encode("utf-8"))
        log.info('Elevated input broker disconnected')
        return 0
    except Exception:
        log.exception("Elevated input broker failed to start")
        return 1
    finally:
        if sink is not None:
            sink.close()
        if reader is not None:
            reader.close()
        if connection is not None:
            connection.close()
