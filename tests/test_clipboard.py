import asyncio

import ctypes
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest


@pytest.mark.skipif(os.name != 'nt', reason='Win32 clipboard transactions')
def test_clipboard_serializes_native_transactions(monkeypatch):
    from elink.core import clipboard
    active = 0
    peak = 0
    guard = threading.Lock()

    def transaction(*args):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(active, peak)
        time.sleep(.01)
        with guard:
            active -= 1
        return True if args else 'text'

    monkeypatch.setattr(clipboard, '_read_text_locked', transaction)
    monkeypatch.setattr(clipboard, '_write_text_locked', transaction)
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = [pool.submit(clipboard.write_text, 'value') if i % 2 else pool.submit(clipboard.read_text)
                for i in range(16)]
        assert [job.result() for job in jobs] == ['text', True] * 8
    assert peak == 1


@pytest.mark.skipif(os.name != 'nt', reason='Win32 global clipboard memory')
@pytest.mark.parametrize('payload,expected', [
    ('正常\x00尾部'.encode('utf-16-le'), '正常'),
    ('无终止符'.encode('utf-16-le'), '无终止符'),
    (b'\x00', None),
    ('🙂'.encode('utf-16-le'), '🙂'),
])
def test_clipboard_reads_only_allocated_unicode_memory(monkeypatch, payload, expected):
    from elink.core import clipboard
    buffer = ctypes.create_string_buffer(payload)
    calls = []
    monkeypatch.setattr(clipboard, '_open_clipboard', lambda: True)
    monkeypatch.setattr(clipboard, '_user32', SimpleNamespace(
        GetClipboardData=lambda _: 123, CloseClipboard=lambda: calls.append('close')))
    monkeypatch.setattr(clipboard, '_kernel32', SimpleNamespace(
        GlobalSize=lambda _: len(payload), GlobalLock=lambda _: ctypes.addressof(buffer),
        GlobalUnlock=lambda _: calls.append('unlock')))
    assert clipboard.read_text() == expected
    assert calls == ['unlock', 'close']


def test_client_clipboard_watcher_sends_only_changes(tmp_path, monkeypatch):
    from elink.core import transport
    from elink.core.transport import Client

    async def run():
        client = Client(tmp_path / 'client', play_audio=False)
        peer = object()
        client.pc = peer
        client.clipboard_seen = 'before'
        sent = []
        client.send = sent.append
        monkeypatch.setattr(transport, 'read_text', lambda: 'after')
        task = asyncio.create_task(client.watch_clipboard(peer))
        await asyncio.sleep(0.8)
        client.pc = None
        await asyncio.wait_for(task, 1)
        assert sent == [{'type': 'clipboard', 'text': 'after'}]

    asyncio.run(run())
