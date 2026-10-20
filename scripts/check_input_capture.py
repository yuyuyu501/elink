"""Check windowed pointer exit and fullscreen capture on CPU/GPU players.

Run on an unlocked Windows desktop. Moves the local pointer briefly, restores
it afterwards, and never injects remote input. Uses only isolated test players.
"""
import argparse
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import time
from types import SimpleNamespace
from unittest.mock import patch

import av
import numpy as np
from PySide6.QtCore import QEvent, QObject, QPoint, Qt, Signal
from PySide6.QtGui import QCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from elink.core.media import FrameMailbox
from elink.ui.player import Player
from elink.ui.theme import apply_theme


class Runtime(QObject):
    feedback = Signal(object)

    def call(self, function, *args):
        function(*args)


def settle(app, until=None):
    deadline = time.monotonic() + .12
    while time.monotonic() < deadline:
        app.processEvents()
        if until and until():
            return
        time.sleep(.002)


def clip_bounds():
    rect = wintypes.RECT()
    if not ctypes.windll.user32.GetClipCursor(ctypes.byref(rect)):
        raise ctypes.WinError()
    return rect.left, rect.top, rect.right, rect.bottom


def present(view, renderer, source_size):
    width, height = source_size
    pixels = np.full((height, width, 3), (40, 160, 80), dtype=np.uint8)
    if renderer == 'gpu':
        assert view.gpu_surface is not None, 'GPU renderer fell back'
        view.gpu_surface.set_frame(av.VideoFrame.from_ndarray(pixels, format='bgr24').reformat(format='yuv420p'))
    else:
        view.set_frame_pixels(pixels)
    view.refresh_video()


def check_windowed(app, view, client, events, before, mode, visible, fallback=False):
    client.cursor_visible = visible
    view.set_mouse_mode(mode)
    rect = view.rect_image
    points = [(QPoint(rect.left() + 1, rect.center().y()), QPoint(rect.left() - 4, rect.center().y())),
              (QPoint(rect.right() - 1, rect.center().y()), QPoint(rect.right() + 4, rect.center().y())),
              (QPoint(rect.center().x(), rect.top() + 1), QPoint(rect.center().x(), rect.top() - 4)),
              (QPoint(rect.center().x(), rect.bottom() - 1), QPoint(rect.center().x(), rect.bottom() + 4))]
    for inside, outside in points:
        view.release()
        QCursor.setPos(view.mapToGlobal(rect.center()))
        settle(app)
        client.cursor_updated = time.monotonic()
        with patch.object(QCursor, 'setPos', wraps=QCursor.setPos) as warp:
            view.capture()
            settle(app, lambda: view.captured)
            assert view.captured and not warp.called, (
                'windowed capture changed', mode, visible, view.captured, QCursor.pos(),
                view.rect_image.getRect(), view.isActiveWindow(), events[-5:])
        assert clip_bounds() == before, 'windowed sessions must not confine the system pointer'
        if not visible:
            assert view.game_mouse and (fallback or view.raw_mouse.active)
        else:
            assert not view.game_mouse and not view.raw_mouse.active
        QCursor.setPos(view.mapToGlobal(inside))
        def desktop_aligned():
            moves = [event for kind, event in events if kind == 'input' and event.get('type') == 'move']
            if not moves or not moves[-1].get('absolute'):
                return False
            point = view.mapFromGlobal(QCursor.pos())
            move = moves[-1]
            x = rect.x() + move['x'] * (rect.width() - 1) / 65535
            y = rect.y() + move['y'] * (rect.height() - 1) / 65535
            # QCursor exposes integer logical coordinates; native Qt move
            # events retain fractional positions at 125/150% Windows scaling.
            return abs(x - point.x()) <= 1 and abs(y - point.y()) <= 1
        settle(app, desktop_aligned if visible else lambda: view.captured)
        assert view.captured and view.cursor().shape() == Qt.CursorShape.BlankCursor, (
            mode, rect.getRect(), inside, view.mapFromGlobal(QCursor.pos()), events[-5:])
        if visible:
            assert desktop_aligned(), ('desktop pointer does not match controller', mode, events[-5:])
            if mode == 'local':
                delta = view.software_cursor_pos - view.mapFromGlobal(QCursor.pos())
                assert abs(delta.x()) <= 1 and abs(delta.y()) <= 1
        assert all(not handle.isVisible() for handle in view.resize_handles)
        events.clear()
        QCursor.setPos(view.mapToGlobal(outside))
        assert not view.video_contains(view.mapFromGlobal(QCursor.pos())), (
            'pointer did not cross video edge', mode, visible, outside, view.mapFromGlobal(QCursor.pos()),
            view.rect_image.getRect(), view.captured, clip_bounds(), events[-5:])
        settle(app, lambda: not view.captured)
        assert not view.captured and not view.software_cursor and not view.raw_mouse.active, (
            'pointer exit was not released', mode, visible, rect.getRect(), outside,
            view.mapFromGlobal(QCursor.pos()), view.isActiveWindow(), events[-5:])
        assert events[-1] == ('focus', False) and clip_bounds() == before
        if visible:
            final = events[-2][1]
            assert final['absolute']
            assert final['x'] in (0, 65535) or final['y'] in (0, 65535)
        events.clear()
        # Released windows expose local resize handles along their outer edge.
        # Reenter the video past those handles, without clicking them.
        reentry = QPoint(max(rect.left() + 10, min(rect.right() - 10, inside.x())),
                         max(rect.top() + 10, min(rect.bottom() - 10, inside.y())))
        QCursor.setPos(view.mapToGlobal(reentry))
        settle(app, lambda: view.captured)
        assert view.captured and events[0] == ('focus', True), (
            'pointer reentry needed a click', mode, visible, view.isActiveWindow(), view._resume_on_hover,
            QApplication.widgetAt(QCursor.pos()), view.mapFromGlobal(QCursor.pos()), events[-5:])
        assert clip_bounds() == before
    return dict(mode=mode, host_cursor_visible=visible, fallback=fallback, four_edges=True,
                reentry_without_click=True,
                desktop_aligned=True if visible else None)


def check(app, renderer):
    events = []
    client = SimpleNamespace(pc=object(), mailbox=FrameMailbox(), rtt_ms=0, last_pong=time.monotonic(),
                             cursor_supported=True, cursor_visible=False, cursor_updated=time.monotonic(),
                             set_cursor_mode=lambda _: None, set_renderer=lambda _: None,
                             focus=lambda value: events.append(('focus', value)),
                             send=lambda e, reliable=False: events.append(('input', e)))
    view = Player(Runtime(), client, controllers=False, preferences={'renderer': renderer, 'mouse_mode': 'local'})
    view.resize(900, 620)
    view.move(view.screen().availableGeometry().topLeft() + QPoint(30, 30))
    view.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
    view.show()
    view.raise_()
    view.activateWindow()
    settle(app)
    before = clip_bounds()
    try:
        present(view, renderer, (640, 360))
        settle(app)
        assert view.isActiveWindow() and not view.rect_image.isEmpty()
        windowed = []
        for size, source_size in (((640, 400), (640, 360)), ((1000, 650), (480, 360))):
            view.resize(*size)
            present(view, renderer, source_size)
            settle(app)
            for mode, visible in (('smart', True), ('smart', False), ('local', True),
                                  ('local', False), ('remote', True), ('remote', False)):
                result = check_windowed(app, view, client, events, before, mode, visible)
                windowed.append(dict(size=size, source_size=source_size, **result))
        start = view.raw_mouse.start
        view.raw_mouse.start = lambda _: False
        try:
            windowed.append(check_windowed(app, view, client, events, before, 'remote', False, fallback=True))
        finally:
            view.raw_mouse.start = start

        # Returning from an active local window also needs no activation click.
        client.cursor_visible = True
        client.cursor_updated = time.monotonic()
        view.set_mouse_mode('local')
        QCursor.setPos(view.mapToGlobal(view.rect_image.center()))
        view.capture()
        QCursor.setPos(view.mapToGlobal(QPoint(view.width() + 30, view.height() // 2)))
        settle(app)
        assert not view.captured and view._resume_on_hover
        local = QWidget()
        local.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
        local.resize(100, 120)
        local.move(view.mapToGlobal(QPoint(view.width() + 10, 80)))
        try:
            local.show()
            local.activateWindow()
            settle(app)
            assert local.isActiveWindow() and not view.isActiveWindow()
            QCursor.setPos(view.mapToGlobal(view.rect_image.center()))
            settle(app, lambda: view.captured)
            assert view.captured and view.isActiveWindow(), 'return from local app needs a click'
            view.release(resume_on_hover=True)
            local.move(view.mapToGlobal(view.rect_image.center() - QPoint(50, 60)))
            local.activateWindow()
            settle(app)
            assert not view.captured, 'covered video must not resume remote input'
        finally:
            local.close()
            local.deleteLater()
            view.release()
            view.activateWindow()
            settle(app)

        view.open_controls()
        center = view.control_menu
        center.mouse_button.click()
        settle(app)
        QTest.mouseClick(center.modes['local'], Qt.MouseButton.LeftButton)
        assert center.isVisible() and center.mouse_options.isVisible()
        events.clear()
        QTest.mouseClick(view, Qt.MouseButton.LeftButton, pos=view.rect_image.bottomLeft() + QPoint(20, -20))
        settle(app)
        assert not center.isVisible() and not center.mouse_options.isVisible()
        assert not any(kind == 'input' and value.get('type') == 'button' for kind, value in events)
        view.release()

        # Fullscreen games retain continuous relative input and release on F8.
        view.showFullScreen()
        settle(app)
        client.cursor_visible = False
        client.cursor_updated = time.monotonic()
        QCursor.setPos(view.mapToGlobal(view.rect_image.center()))
        view.capture()
        settle(app)
        assert view.captured and view.game_mouse and view.raw_mouse.active
        QCursor.setPos(view.mapToGlobal(QPoint(view.width() + 80, view.height() // 2)))
        settle(app)
        assert view.video_contains(view.mapFromGlobal(QCursor.pos())) and view.captured
        QTest.keyClick(view, Qt.Key.Key_F8)
        settle(app)
        assert not view.captured and not view.raw_mouse.active and clip_bounds() == before
        view.open_controls()
        view.showNormal()
        view.move(view.screen().availableGeometry().topLeft() + QPoint(30, 30))
        settle(app)
        assert not view.captured and clip_bounds() == before
        # Windowed input still releases on F8 and focus loss, including offscreen windows.
        view.move(view.screen().geometry().topLeft() - QPoint(100, 20))
        settle(app)
        QCursor.setPos(view.mapToGlobal(view.rect_image.center()))
        view.capture()
        view.update_mouse()
        assert clip_bounds() == before
        QTest.keyClick(view, Qt.Key.Key_F8)
        settle(app)
        assert not view.captured and clip_bounds() == before
        view.open_controls()
        QCursor.setPos(view.mapToGlobal(view.rect_image.center()))
        view.capture()
        assert view.captured
        QApplication.sendEvent(view, QEvent(QEvent.Type.WindowDeactivate))
        assert not view.captured and not view.raw_mouse.active and clip_bounds() == before
        return dict(renderer=renderer, windowed=windowed, fullscreen_confined=True,
                    f8_release=True, focus_release=True, offscreen_unconfined=True,
                    local_window_return=True, covered_video_ignored=True, outside_click_dismisses=True)
    finally:
        view.close()
        settle(app)
        assert clip_bounds() == before


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    app = QApplication.instance() or QApplication([])
    if app.platformName() != 'windows':
        raise RuntimeError('Use the native Windows Qt platform on an unlocked desktop')
    apply_theme(app)
    original = QCursor.pos()
    try:
        results = [check(app, renderer) for renderer in ('cpu', 'gpu')]
        report = json.dumps(dict(ok=True, scale_factor=app.devicePixelRatio(), checks=results), indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(report, encoding='utf-8')
        print(report)
    finally:
        QCursor.setPos(original)


if __name__ == '__main__':
    main()
