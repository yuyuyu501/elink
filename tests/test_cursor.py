import ctypes
from ctypes import wintypes
from types import SimpleNamespace

import numpy as np

from elink.core.cursor import WindowsCursor, cursor_rect
from elink.core.media import DesktopTrack


def test_cursor_hotspot_output_origin_and_clipping():
    assert cursor_rect((100, 200, 3), (-1919, 1), (5, 5), (32, 32), (-1920, 0)) == (0, 0, 28, 28, 4, 4)
    assert cursor_rect((100, 200, 3), (199, 99), (0, 0), (32, 32)) == (199, 99, 200, 100, 0, 0)
    assert cursor_rect((100, 200, 3), (201, 99), (0, 0), (32, 32)) is None


def test_windows_cursor_draw_does_not_change_cached_frame():
    cursor = WindowsCursor()
    cursor.user.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
    cursor.user.LoadCursorW.restype = wintypes.HANDLE
    handle = cursor.user.LoadCursorW(None, 32512)  # Shared system arrow, no ownership.
    assert handle
    background = np.full((100, 200, 3), 80, dtype=np.uint8)
    first = cursor.draw(background, handle, (10, 10))
    second = cursor.draw(background, handle, (80, 10))
    assert np.any(first != background)
    assert np.any(second != background)
    assert np.all(background == 80)
    assert np.all(second[:, :60] == 80)  # No trail left from the previous position.
    clipped = cursor.draw(background, handle, (-5, -5))
    assert clipped.shape == background.shape


def test_hidden_cursor_preserves_game_frame():
    cursor = WindowsCursor()
    def hidden(pointer):
        pointer._obj.flags = 0
        return True
    cursor.user = SimpleNamespace(GetCursorInfo=hidden)
    background = np.zeros((20, 20, 3), dtype=np.uint8)
    assert cursor.composite(background) is background


def test_desktop_redraws_cursor_without_a_new_desktop_frame():
    pixels = np.zeros((16, 16, 3), dtype=np.uint8)
    frames = iter([pixels, None])
    calls = []
    track = DesktopTrack(16, 16, 60)
    track.camera = SimpleNamespace(grab=lambda: next(frames), release=lambda: None,
                                  _output=SimpleNamespace(desc=SimpleNamespace(
                                      DesktopCoordinates=SimpleNamespace(left=-1920, top=0))))
    def composite(frame, origin):
        calls.append(origin)
        result = frame.copy()
        result[:, len(calls) * 4:len(calls) * 4 + 4] = 255
        return result
    track.cursor = SimpleNamespace(composite=composite)
    try:
        first, second = track.capture(), track.capture()
        assert len(calls) == 2 and calls[0] == (-1920, 0)
        assert not np.array_equal(first.to_ndarray(), second.to_ndarray())
        assert np.all(track.last == 0)
    finally:
        track.stop()
        track.release_future.result(timeout=5)
