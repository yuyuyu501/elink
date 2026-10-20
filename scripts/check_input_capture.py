"""Check native Windows pointer confinement on synthetic CPU/GPU players.

Run on an unlocked Windows desktop. Moves the local pointer briefly, restores
it afterwards, and never injects remote input. --baseline disables confinement
to demonstrate the former pointer escape with the same Qt/Raw Input path.
"""
import argparse
import ctypes
from ctypes import wintypes
import json
import time
from types import SimpleNamespace

import av
import numpy as np
from PySide6.QtCore import QEvent, QObject, QPoint, Qt, Signal
from PySide6.QtGui import QCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from elink.core.media import FrameMailbox
from elink.ui.player import Player
from elink.ui.theme import apply_theme


class Runtime(QObject):
    feedback = Signal(object)

    def call(self, function, *args):
        function(*args)


def settle(app):
    deadline = time.monotonic() + .12
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.002)


def clip_bounds():
    rect = wintypes.RECT()
    if not ctypes.windll.user32.GetClipCursor(ctypes.byref(rect)):
        raise ctypes.WinError()
    return rect.left, rect.top, rect.right, rect.bottom


def check(app, renderer, baseline):
    events = []
    client = SimpleNamespace(pc=object(), mailbox=FrameMailbox(), rtt_ms=0, last_pong=time.monotonic(),
                             cursor_supported=True, cursor_visible=False, cursor_updated=time.monotonic(),
                             set_cursor_mode=lambda _: None, set_renderer=lambda _: None,
                             focus=lambda value: events.append(('focus', value)), send=lambda e: events.append(('input', e)))
    view = Player(Runtime(), client, controllers=False, preferences={'renderer': renderer, 'mouse_mode': 'local'})
    view.timer.stop()
    view.resize(900, 620)
    view.move(view.screen().availableGeometry().topLeft() + QPoint(30, 30))
    view.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
    view.show()
    view.raise_()
    view.activateWindow()
    settle(app)
    before = clip_bounds()
    try:
        pixels = np.full((360, 640, 3), (40, 160, 80), dtype=np.uint8)
        if renderer == 'gpu':
            assert view.gpu_surface is not None, 'GPU renderer fell back'
            view.gpu_surface.set_frame(av.VideoFrame.from_ndarray(pixels, format='bgr24').reformat(format='yuv420p'))
        else:
            view.set_frame_pixels(pixels)
        view.refresh_video()
        settle(app)
        assert view.isActiveWindow() and not view.rect_image.isEmpty()
        if baseline:
            view.cursor_confinement.confine = lambda *args: None
        QCursor.setPos(view.mapToGlobal(view.rect_image.center()))
        view.capture()
        settle(app)
        assert view.captured and view.game_mouse and view.raw_mouse.active
        QCursor.setPos(view.mapToGlobal(QPoint(view.width() + 80, view.height() // 2)))
        settle(app)
        escaped = not view.rect_image.contains(view.mapFromGlobal(QCursor.pos()))
        assert escaped == baseline, f'Unexpected pointer escape: {escaped}'
        assert view.captured, 'relative input should remain captured at a game boundary'
        if not baseline:
            bounds = clip_bounds()
            view.move(view.pos() + QPoint(25, 15))
            view.update_mouse()
            assert clip_bounds() != bounds, 'constraint must follow a moved window'
            view.move(view.screen().geometry().topLeft() - QPoint(100, 20))
            view.update_mouse()
        view.open_controls()
        settle(app)
        assert not view.captured and not view.raw_mouse.active and clip_bounds() == before
        view.move(view.screen().availableGeometry().topLeft() + QPoint(30, 30))
        view.open_controls()
        settle(app)
        # Return to desktop mapping. Crossing a letterbox border releases input.
        client.cursor_visible = True
        client.cursor_updated = time.monotonic()
        QCursor.setPos(view.mapToGlobal(view.rect_image.center()))
        settle(app)
        view.capture()
        settle(app)
        assert view.captured and view.software_cursor and not view.game_mouse
        QCursor.setPos(view.mapToGlobal(view.rect_image.topLeft() + QPoint(10, 10)))
        settle(app)
        assert view.captured, (renderer, view.rect_image.getRect(), view.mapFromGlobal(QCursor.pos()),
                               view.isActiveWindow(), events[-5:])
        QCursor.setPos(view.mapToGlobal(QPoint(view.width() + 40, view.height() // 2)))
        settle(app)
        assert not view.captured and not view.software_cursor and events[-1] == ('focus', False)
        assert clip_bounds() == before
        # F8 and focus loss must both release the game constraint and raw input.
        client.cursor_visible = False
        client.cursor_updated = time.monotonic()
        view.capture()
        QTest.keyClick(view, Qt.Key.Key_F8)
        settle(app)
        assert not view.captured and clip_bounds() == before
        view.open_controls()
        view.capture()
        assert view.captured
        QApplication.sendEvent(view, QEvent(QEvent.Type.WindowDeactivate))
        assert not view.captured and not view.raw_mouse.active and clip_bounds() == before
        return dict(renderer=renderer, baseline=baseline, pointer_escaped=escaped,
                    desktop_exit=True, f8_release=True, focus_release=True, offscreen_release=not baseline)
    finally:
        view.close()
        settle(app)
        assert clip_bounds() == before


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', action='store_true')
    args = parser.parse_args()
    app = QApplication.instance() or QApplication([])
    if app.platformName() != 'windows':
        raise RuntimeError('Use the native Windows Qt platform on an unlocked desktop')
    apply_theme(app)
    original = QCursor.pos()
    try:
        results = [check(app, renderer, args.baseline) for renderer in ('cpu', 'gpu')]
        print(json.dumps(dict(ok=True, checks=results), indent=2))
    finally:
        QCursor.setPos(original)


if __name__ == '__main__':
    main()
