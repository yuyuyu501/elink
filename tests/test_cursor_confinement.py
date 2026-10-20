import ctypes
from ctypes import wintypes

from elink.core.input import CursorConfinement


def test_constraint_uses_native_client_pixels_at_fractional_dpi_and_negative_origin():
    clips = []

    class Native:
        origin = (-2560, 200)
        clip_to_desktop = False

        def GetClientRect(self, hwnd, rect):
            rect = ctypes.cast(rect, ctypes.POINTER(wintypes.RECT)).contents
            rect.right, rect.bottom = 1920, 1200
            return True

        def ClientToScreen(self, hwnd, point):
            point = ctypes.cast(point, ctypes.POINTER(wintypes.POINT)).contents
            point.x, point.y = self.origin
            return True

        def ClipCursor(self, rect):
            if rect is None:
                clips.append(None)
            else:
                rect = ctypes.cast(rect, ctypes.POINTER(wintypes.RECT)).contents
                clips.append((max(0, rect.left) if self.clip_to_desktop else rect.left,
                              max(0, rect.top) if self.clip_to_desktop else rect.top, rect.right, rect.bottom))
            return True

        def GetClipCursor(self, rect):
            rect = ctypes.cast(rect, ctypes.POINTER(wintypes.RECT)).contents
            rect.left, rect.top, rect.right, rect.bottom = clips[-1]
            return True

    constraint = CursorConfinement()
    constraint._user32 = native = Native()
    constraint.confine(1, (0, 40, 1280, 720), (1280, 800))
    assert clips == [(-2560, 260, -640, 1340)]
    constraint.confine(1, (0, 40, 1280, 720), (1280, 800))
    assert len(clips) == 1
    native.origin = (100, 0)
    constraint.confine(1, (0, 40, 1280, 720), (1280, 800))
    assert clips[-1] == (100, 60, 2020, 1140)
    constraint.release()
    assert clips[-1] is None
    constraint.release()
    assert clips.count(None) == 1

    constraint.confine(1, (1, 41, 1278, 718), (1280, 800))
    assert clips[-1] == (101, 61, 2019, 1139)
    # Another app has taken ownership: leave its pointer restriction alone.
    clips.append((10, 10, 20, 20))
    constraint.release()
    assert clips[-1] == (10, 10, 20, 20)

    native.origin, native.clip_to_desktop = (-100, -50), True
    constraint.confine(1, (0, 40, 1280, 720), (1280, 800))
    assert clips[-1] == (0, 10, 1820, 1090)
    constraint.release()
    assert clips[-1] is None, 'a clipped offscreen rectangle must still be released'
