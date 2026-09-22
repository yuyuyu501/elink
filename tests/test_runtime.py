import asyncio
import concurrent.futures
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtWidgets import QApplication

from elink.core.runtime import Runtime


@pytest.mark.parametrize('failed', [False, True])
def test_already_finished_future_delivers_after_submit_returns(monkeypatch, failed):
    app = QApplication.instance() or QApplication([])
    runtime = Runtime()
    delivered = []
    def immediate(coro, loop):
        coro.close()
        future = concurrent.futures.Future()
        if failed:
            future.set_exception(RuntimeError('instant error'))
        else:
            future.set_result('instant result')
        return future
    monkeypatch.setattr(asyncio, 'run_coroutine_threadsafe', immediate)
    try:
        future = runtime.submit(asyncio.sleep(0), delivered.append, delivered.append)
        assert future.done()
        assert delivered == []  # Callers have a chance to assign their job handle.
        app.processEvents()
        assert delivered == ['instant error' if failed else 'instant result']
    finally:
        runtime.stop()
