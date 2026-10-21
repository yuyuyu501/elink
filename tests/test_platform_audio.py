import asyncio
import locale
import sys
from types import SimpleNamespace

import numpy as np

from elink.core import codecs
from elink.core.media import LoopbackTrack


def test_native_error_locale_is_fixed_without_changing_ui_or_numeric_locale(monkeypatch):
    calls = []
    monkeypatch.setattr(locale, 'LC_MESSAGES', 5, raising=False)
    monkeypatch.setattr(locale, 'setlocale', lambda *args: calls.append(args))
    monkeypatch.setattr(codecs, 'sys', SimpleNamespace(platform='linux'))
    codecs.install_factories()
    assert calls == [(locale.LC_MESSAGES, 'C')]


def test_idle_loopback_keeps_opus_clock_and_resumes_real_samples(monkeypatch):
    monkeypatch.setattr(LoopbackTrack, '_capture', lambda self: self.done.wait(2))
    track = LoopbackTrack(synthetic=True)
    async def run():
        first = await track.recv()
        assert not first.to_ndarray().any()
        track._put(np.full((960, 2), .25, np.float32))
        second = await track.recv()
        assert second.to_ndarray().mean() > 8000
        third = await track.recv()
        assert [f.pts for f in (first, second, third)] == [0, 960, 1920]
        assert track.silence_blocks == 2 and track.captured_blocks == 1
    try:
        asyncio.run(run())
    finally:
        track.stop()
        track.thread.join(timeout=2)
