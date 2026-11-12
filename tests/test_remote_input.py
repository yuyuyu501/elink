"""Window input tests use recording sinks; no desktop input is injected."""

from __future__ import annotations

import ctypes
import os
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from backend.control_channel.base import ChannelState
from backend.control_channel.protocol import ControlCommand, EventType, InputEvent
from backend.input_capture.windows.raw_mouse import RawMouseInput, _RawInput
from backend.input_injection.session import InputSession
from backend.input_capture.mouse import MouseSensitivity, mouse_mode, relative_mouse, sensitivity_value
from backend.media.video.cursor.capture import CursorState
from backend.media.video.cursor.overlay import CursorOverlay
from backend.media.frame import Frame
from frontend.desktop.diagnostics import _synthetic_frame
from frontend.desktop.service import HostSettings, SessionService, ViewerSettings
from frontend.desktop.theme import apply_theme
from frontend.desktop.ui.stream_window import PictureDialog, StreamWindow, TITLE_HEIGHT

APP = QApplication.instance() or QApplication([])
apply_theme(APP)


def focus(epoch=1, active=True):
    return ControlCommand("input_focus", {"epoch": epoch, "active": active})


def key(epoch=1, pressed=True):
    return InputEvent(EventType.KEY, key_code=65, scan_code=30, pressed=pressed,
                      extra={"epoch": epoch})


class InputSessionTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.session = InputSession(lambda event: self.events.append(event) or True)

    def test_capture_required_and_old_epochs_cannot_reactivate(self):
        self.assertFalse(self.session.receive(key()))
        self.assertTrue(self.session.receive(focus()))
        self.assertTrue(self.session.receive(key()))
        self.session.receive(focus(2, False))
        self.assertFalse(self.session.receive(key()))
        self.assertFalse(self.session.receive(focus(1)))
        self.assertFalse(self.session.receive(focus(2)))
        self.assertTrue(self.session.receive(focus(3)))
        self.assertTrue(self.session.receive(key(3)))

    def test_release_synthesizes_all_key_and_mouse_ups_once(self):
        self.session.receive(focus())
        self.session.receive(key())
        for button in (1, 2, 3):
            self.session.receive(InputEvent(EventType.MOUSE_BUTTON, button=button, pressed=True,
                                           extra={"epoch": 1, "positioned": True}))
        self.session.release()
        ups = self.events[4:]
        self.assertEqual(len(ups), 4)
        self.assertTrue(all(not event.pressed for event in ups))
        self.session.release()
        self.assertEqual(len(self.events), 8)

    def test_heartbeat_timeout_releases_and_requires_new_epoch(self):
        self.session.receive(focus())
        self.session.receive(key())
        self.session.heartbeat = time.monotonic() - 3
        self.session.watchdog()
        self.assertFalse(self.session.active)
        self.assertFalse(self.events[-1].pressed)
        self.assertFalse(self.session.receive(focus()))
        self.assertTrue(self.session.receive(focus(2)))

    def test_invalid_events_are_rejected_before_injection(self):
        self.session.receive(focus())
        for event in (
            InputEvent(EventType.MOUSE_MOVE, x=65536, extra={"epoch": 1, "coordinates": "normalized"}),
            InputEvent(EventType.MOUSE_MOVE, x=True, extra={"epoch": 1}),
            InputEvent(EventType.MOUSE_BUTTON, button=4, pressed=True, extra={"epoch": 1}),
            InputEvent(EventType.MOUSE_WHEEL, delta=12001, extra={"epoch": 1}),
            InputEvent(EventType.KEY, key_code=256, extra={"epoch": 1}),
        ):
            self.assertFalse(self.session.receive(event))
        self.assertFalse(self.events)

    def test_failed_release_is_retried_without_losing_other_held_keys(self):
        self.session.receive(focus())
        self.session.receive(key())
        self.session.receive(InputEvent(EventType.MOUSE_BUTTON, button=1, pressed=True, extra={"epoch": 1}))
        original = self.session._inject
        def inject(event):
            if event.event_type == EventType.KEY:
                raise OSError("denied")
            return original(event)
        self.session._inject = inject
        with self.assertRaises(OSError):
            self.session.release()
        self.assertFalse(self.events[-1].pressed)
        self.assertEqual(len(self.session._held), 1)
        self.session._inject = original
        self.session.release()
        self.assertFalse(self.session._held)


class WindowInputTests(unittest.TestCase):
    def setUp(self):
        self.window = StreamWindow()
        self.window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.preview = self.window.preview
        self.events, self.focuses = [], []
        self.preview.bind_input(lambda event: self.events.append(event) or True,
                                lambda active: self.focuses.append(active) or True)
        self.window.apply_settings(ViewerSettings(control_enabled=True))
        self.window.set_session_active(True)
        self.window.set_input_available(True)
        self.window.resize(1000, 700)
        self.window.show()
        self.window.activateWindow()
        self.preview.set_frame(_synthetic_frame(0))
        APP.processEvents()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        APP.processEvents()

    def test_mapping_uses_actual_letterboxed_picture_at_multiple_sizes(self):
        for size in ((640, 400), (1280, 760), (1600, 900)):
            self.window.resize(*size)
            APP.processEvents()
            rect = self.preview._target_rect()
            self.assertEqual(self.preview.normalized_position(QPointF(rect.topLeft())), (0, 0))
            self.assertEqual(self.preview.normalized_position(QPointF(rect.bottomRight())), (65535, 65535))
            center = self.preview.normalized_position(QPointF(rect.x() + (rect.width() - 1) / 2,
                                                             rect.y() + (rect.height() - 1) / 2))
            self.assertEqual(center, (32768, 32768))
            self.assertEqual(self.preview.y(), TITLE_HEIGHT)
            self.assertFalse(self.preview.video_contains(QPointF(-1, -1)))
            self.assertFalse(self.preview.video_contains(QPointF(rect.x() + rect.width(), rect.y())))

    def test_first_click_moves_before_down_and_up_and_keyboard_is_single(self):
        point = self.preview._target_rect().center()
        QTest.mouseClick(self.preview, Qt.MouseButton.LeftButton, pos=point)
        self.assertEqual([event.event_type for event in self.events], [
            EventType.MOUSE_MOVE, EventType.MOUSE_BUTTON, EventType.MOUSE_MOVE, EventType.MOUSE_BUTTON])
        self.assertEqual([event.pressed for event in self.events if event.event_type == EventType.MOUSE_BUTTON],
                         [True, False])
        self.assertTrue(self.events[1].extra["positioned"])
        self.events.clear()
        QTest.keyClick(self.preview, Qt.Key.Key_A)
        self.assertEqual([(event.key_code, event.pressed) for event in self.events], [(65, True), (65, False)])
        self.events.clear()
        QTest.keyClick(self.preview, Qt.Key.Key_Tab)
        self.assertEqual([(event.key_code, event.pressed) for event in self.events], [(9, True), (9, False)])

    def test_watch_and_empty_picture_do_not_capture(self):
        self.window.apply_settings(ViewerSettings(control_enabled=False))
        QTest.mouseClick(self.preview, Qt.MouseButton.LeftButton, pos=self.preview._target_rect().center())
        self.assertFalse(self.preview.captured)
        self.assertFalse(self.events)
        self.window.apply_settings(ViewerSettings(control_enabled=True))
        self.preview.clear()
        self.assertFalse(self.preview.capture_input())

    def test_drag_sends_final_position_and_releases_outside_video(self):
        rect = self.preview._target_rect()
        QTest.mousePress(self.preview, Qt.MouseButton.LeftButton, pos=rect.center())
        point = QPointF(rect.x() - 2, rect.center().y())
        move = QMouseEvent(QEvent.Type.MouseMove, point, point, Qt.MouseButton.NoButton,
                           Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        APP.sendEvent(self.preview, move)
        self.assertFalse(self.preview.captured)
        self.assertEqual(self.events[-1].event_type, EventType.MOUSE_MOVE)
        self.assertEqual(self.events[-1].x, 0)
        self.assertFalse(self.focuses[-1])

    def test_black_bar_and_toolbar_clicks_stay_local(self):
        QTest.mouseClick(self.preview, Qt.MouseButton.LeftButton, pos=QPoint(1, 1))
        QTest.mouseClick(self.window.toolbar, Qt.MouseButton.LeftButton, pos=QPoint(150, 20))
        self.assertFalse(self.events)
        self.assertFalse(self.focuses)

    def test_focus_loss_panel_minimize_and_watch_each_release_capture(self):
        actions = [
            lambda: APP.sendEvent(self.preview, QEvent(QEvent.Type.FocusOut)),
            self.window.toggle_controls,
            self.window.minimize_window,
            lambda: self.window.apply_settings(ViewerSettings(control_enabled=False)),
        ]
        for action in actions:
            self.window.panel.hide()
            self.window.showNormal()
            self.window.activateWindow()
            self.window.apply_settings(ViewerSettings(control_enabled=True))
            APP.processEvents()
            self.assertTrue(self.preview.capture_input())
            action()
            self.assertFalse(self.preview.captured)
            self.assertFalse(self.focuses[-1])
        self.assertEqual(self.focuses.count(True), self.focuses.count(False))

    def test_wheel_targets_current_position_and_relative_button_does_not_warp(self):
        self.assertTrue(self.preview.capture_input())
        point = QPointF(self.preview._target_rect().center())
        wheel = QWheelEvent(point, point, QPoint(), QPoint(0, 120), Qt.MouseButton.NoButton,
                            Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
        APP.sendEvent(self.preview, wheel)
        self.assertEqual([event.event_type for event in self.events],
                         [EventType.MOUSE_MOVE, EventType.MOUSE_WHEEL])
        self.preview.set_mouse_mode("relative")
        self.events.clear()
        QTest.mouseClick(self.preview, Qt.MouseButton.RightButton, pos=point.toPoint())
        self.assertEqual([event.event_type for event in self.events],
                         [EventType.MOUSE_BUTTON, EventType.MOUSE_BUTTON])

    def test_fullscreen_reuses_window_and_preserves_picture_rectangle_when_captured(self):
        self.window.toggle_fullscreen()
        APP.processEvents()
        rect = self.preview.geometry()
        self.assertTrue(self.preview.capture_input())
        self.assertFalse(self.window.toolbar.isVisible())
        self.assertEqual(self.preview.geometry(), rect)
        self.window.release_input()
        self.assertTrue(self.window.toolbar.isVisible())
        self.assertEqual(self.preview.geometry(), rect)
        self.window.toggle_fullscreen()
        self.assertFalse(self.window.isFullScreen())

    def test_picture_label_uses_mbps_outside_input(self):
        dialog = PictureDialog({"width": 1920, "height": 1080, "fps": 30, "bitrate_kbps": 4000})
        self.assertEqual(dialog.layout().labelForField(dialog.bitrate).text(), "码率（Mbps）")
        self.assertEqual(dialog.bitrate.suffix(), "")
        self.assertEqual(dialog.picture()["bitrate_kbps"], 4000)
        dialog.close()

    def test_three_modes_and_cursor_updates_keep_last_known_visibility(self):
        self.assertEqual([self.window.mouse_mode.itemData(i) for i in range(3)],
                         ["smart", "remote", "local"])
        self.preview.set_cursor_state({"mode": "smart", "visible": False})
        self.assertTrue(self.preview._relative)
        self.preview.set_cursor_state({"mode": "smart", "visible": None})
        self.assertTrue(self.preview._relative)
        self.preview.set_cursor_state({"mode": "remote", "visible": True})
        self.assertTrue(self.preview._relative)
        self.preview.set_cursor_state({"mode": "smart", "visible": True})
        self.assertFalse(self.preview._relative)
        self.preview.set_mouse_mode("local")
        self.preview.request_mouse_mode()
        self.assertFalse(self.preview.capture_input())
        self.preview.set_cursor_state({"mode": "local", "visible": True})
        self.assertTrue(self.preview.capture_input())
        self.assertFalse(self.preview._relative)

    def test_local_mode_waits_for_cursor_free_video_before_capture(self):
        self.preview.set_mouse_mode("local")
        self.preview.request_mouse_mode()
        self.preview.set_cursor_state({"mode": "local", "visible": True, "pts": 90000})
        self.assertFalse(self.preview.capture_input())
        self.preview.set_frame(_synthetic_frame(0).with_data(_synthetic_frame(0).data, pts=90000))
        self.assertTrue(self.preview.capture_input())

    def test_sensitivity_slider_reset_and_absolute_delta_scaling(self):
        self.window.sensitivity.setValue(50)
        self.assertEqual(self.window.sensitivity_value.text(), "5.0")
        self.preview.set_sensitivity(5)
        self.assertTrue(self.preview.capture_input())
        rect = self.preview._target_rect()
        start = QPointF(rect.center())
        self.preview._absolute_motion(start)
        self.preview._absolute_motion(start + QPointF(10, 0))
        self.assertEqual(self.events[-1].x, self.preview.normalized_position(start + QPointF(20, 0))[0])
        self.preview.release_input()
        self.assertIsNone(self.preview._absolute_last)
        self.assertEqual(self.preview._sensitivity.remainder, [0, 0])


class MouseMathTests(unittest.TestCase):
    def test_migration_bounds_and_fractional_relative_motion(self):
        self.assertEqual(mouse_mode("absolute"), "smart")
        self.assertEqual(mouse_mode("relative"), "remote")
        self.assertEqual(mouse_mode("broken"), "smart")
        self.assertEqual(mouse_mode([]), "smart")
        self.assertEqual(mouse_mode({}), "smart")
        for value, expected in ((None, 0), (float("nan"), 0), (100, 5), (-100, -5)):
            self.assertEqual(sensitivity_value(value), expected)
        sensitivity = MouseSensitivity(-5)
        self.assertEqual(sensitivity.relative(1, -1), (0, 0))
        self.assertEqual(sensitivity.relative(1, -1), (1, -1))
        for mode in ("smart", "remote", "local"):
            self.assertFalse(relative_mouse(mode, True))
            self.assertTrue(relative_mouse(mode, False))

    def test_cursor_negative_origin_hotspot_scaling_and_monochrome_inversion(self):
        frame = Frame(data=bytes([20, 40, 60, 255]) * 16, width=4, height=4)
        state = CursorState(-99, 1, 1, 1, 1, 1, True, bytes([200, 100, 50, 255]))
        result = CursorOverlay(origin=(-100, 0)).composite(frame, state)
        self.assertEqual(result.data[:4], state.image)
        self.assertEqual(frame.data[:4], bytes([20, 40, 60, 255]))
        inversion = CursorState(0, 0, 1, 1, image=bytes([255] * 4), background=bytes(4))
        result = CursorOverlay(scale_x=2, scale_y=2).composite(frame, inversion)
        self.assertEqual(result.data[:4], bytes([235, 215, 195, 255]))
        self.assertEqual(result.data[4:8], result.data[:4])


class ServiceInputTests(unittest.TestCase):
    def setUp(self):
        self.service = SessionService()
        self.service._mode = "viewer"
        self.service._viewer_live = ViewerSettings(control_enabled=True)
        self.service._control = Mock(state=ChannelState.OPEN)

    def tearDown(self):
        self.service._stop_control()

    def test_events_require_capture_and_keep_epoch_order(self):
        self.assertFalse(self.service.send_input(key()))
        self.assertTrue(self.service.set_input_focus(True))
        self.service.send_input(key())
        channel = self.service._control
        sent_epoch = channel.send_event.call_args.args[0].extra["epoch"]
        self.assertEqual(channel.send_command.call_args.args[0].args["epoch"], sent_epoch)
        self.service.set_input_focus(False)
        self.assertGreater(channel.send_command.call_args.args[0].args["epoch"], sent_epoch)
        self.assertFalse(self.service.send_input(key()))

    def test_control_disconnect_releases_remote_buttons(self):
        events = []
        self.service._input_session = InputSession(lambda event: events.append(event) or True)
        self.service._input_session.receive(focus())
        self.service._input_session.receive(key())
        self.service._stop_control()
        self.assertEqual([event.pressed for event in events], [True, False])

    def test_tcp_peer_close_releases_a_held_button_without_session_notification(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        host, viewer = SessionService(), SessionService()
        events = []
        host._mode = "host"
        host._injector = Mock()
        host._injector.inject.side_effect = lambda event: events.append(event) or True
        viewer._mode = "viewer"
        settings = ViewerSettings(control_host="127.0.0.1", control_port=port, control_enabled=True)
        viewer._viewer_live = settings
        def wait(predicate):
            deadline = time.monotonic() + 4
            while not predicate():
                if time.monotonic() > deadline:
                    self.fail("TCP control state did not settle")
                APP.processEvents()
                time.sleep(0.01)
        try:
            host._start_control_server(HostSettings(control_port=port))
            viewer._start_control_client(settings)
            wait(lambda: viewer._control is not None and viewer._control.state == ChannelState.OPEN)
            self.assertTrue(viewer.set_input_focus(True))
            self.assertTrue(viewer.send_input(InputEvent(EventType.MOUSE_BUTTON, button=1,
                                                         pressed=True, extra={"positioned": True})))
            wait(lambda: len(events) == 1)
            # Drop only the input socket; no release command or session "bye".
            viewer._control.close()
            wait(lambda: len(events) == 2)
            self.assertFalse(events[-1].pressed)
        finally:
            for service in (host, viewer):
                service._stop.set()
                service._stop_control()
                if service._control_worker is not None:
                    service._control_worker.join(timeout=3)

    def test_capture_and_injection_failures_use_existing_error_report(self):
        from frontend.desktop.paths import error_dir
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"ELINK_DATA_DIR": root}):
            self.service._mode = "host"
            self.service._input_session = InputSession(self.service._inject_remote_input)
            self.service._injector = Mock()
            self.service._injector.inject.side_effect = OSError("SendInput denied")
            self.service._on_remote_input(focus(), HostSettings())
            self.service._on_remote_input(key(), HostSettings())
            reports = list(error_dir().glob("*.txt"))
            self.assertEqual(len(reports), 1)
            text = reports[0].read_text(encoding="utf-8")
            self.assertIn("SendInput denied", text)
            self.assertIn("traceback", text)
            self.assertFalse(self.service._input_session.active)


@unittest.skipUnless(sys.platform == "win32", "Windows API adapter")
class WindowsAdapterTests(unittest.TestCase):
    def test_selected_monitor_normalized_mapping_and_legacy_pixels(self):
        from backend.input_injection.windows.send_input import SendInputInjector
        injector = SendInputInjector(region=(-1920, -200, 1920, 1080))
        with patch.object(injector, "_absolute_move", return_value=True) as move:
            injector.inject(InputEvent(EventType.MOUSE_MOVE, x=0, y=0,
                                       extra={"coordinates": "normalized"}))
            move.assert_called_with(-1920, -200)
            injector.inject(InputEvent(EventType.MOUSE_MOVE, x=65535, y=65535,
                                       extra={"coordinates": "normalized"}))
            move.assert_called_with(-1, 879)
            injector.inject(InputEvent(EventType.MOUSE_MOVE, x=30, y=40))
            move.assert_called_with(30, 40)

    def test_relative_move_buttons_and_sendinput_failure(self):
        from backend.input_injection.windows import send_input as impl
        injector = impl.SendInputInjector()
        items = []
        with patch.object(impl.user32, "SendInput", side_effect=lambda _, ptr, size:
                          items.append(ctypes.cast(ptr, ctypes.POINTER(impl._INPUT)).contents.union.mi.dwFlags) or 1):
            injector.inject(InputEvent(EventType.MOUSE_MOVE, x=-7, y=9, extra={"coordinates": "relative"}))
            injector.inject(InputEvent(EventType.MOUSE_BUTTON, button=1, pressed=True, extra={"positioned": True}))
        self.assertEqual(items, [impl.MOUSEEVENTF_MOVE, impl.MOUSEEVENTF_LEFTDOWN])
        with patch.object(impl.user32, "SendInput", return_value=0):
            with self.assertRaises(impl.InjectionError) as caught:
                injector.inject(InputEvent(EventType.MOUSE_WHEEL, delta=120))
            self.assertEqual(caught.exception.detail["stage"], "SendInput")
        self.assertEqual(ctypes.sizeof(impl._INPUT), 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)

    def test_virtual_desktop_conversion_uses_negative_origin(self):
        from backend.input_injection.windows import send_input as impl
        injector = impl.SendInputInjector()
        metrics = {76: -1920, 77: -200, 78: 3840, 79: 1280}
        with patch.object(impl.user32, "GetSystemMetrics", side_effect=metrics.__getitem__), patch.object(
            injector, "_send_mouse", return_value=True
        ) as send:
            injector._absolute_move(-1920, -200)
            self.assertEqual((send.call_args.kwargs["dx"], send.call_args.kwargs["dy"]), (0, 0))
            injector._absolute_move(1919, 1079)
            self.assertEqual((send.call_args.kwargs["dx"], send.call_args.kwargs["dy"]), (65535, 65535))
            self.assertTrue(send.call_args.kwargs["flags"] & impl.MOUSEEVENTF_VIRTUALDESK)

    def test_partial_hook_install_failure_is_reported_and_unhooks_successful_handle(self):
        from backend.input_capture.windows import send_input_hook as impl
        with patch.object(impl.user32, "SetWindowsHookExW", side_effect=[123, 0]), patch.object(
            impl.user32, "UnhookWindowsHookEx"
        ) as unhook, patch.object(impl.user32, "PostThreadMessageW"):
            hook = impl.SendInputHookCapture()
            with self.assertRaises(impl.HookError):
                hook.open()
            unhook.assert_called_once_with(123)
            self.assertIsNone(hook._thread)

    def test_keyboard_hook_only_intercepts_shell_keys_while_active(self):
        from backend.input_capture.windows import send_input_hook as impl
        hook = impl.SendInputHookCapture(keyboard_only=True)
        with patch.object(impl.user32, "CallNextHookEx", return_value=0):
            data = impl._KBDLLHOOKSTRUCT(vkCode=65, scanCode=30)
            self.assertEqual(hook._keyboard_callback(0, impl.WM_KEYDOWN, ctypes.addressof(data)), 0)
            self.assertEqual(hook.read_all(), [])
            data.vkCode = 0x5B
            self.assertEqual(hook._keyboard_callback(0, impl.WM_KEYDOWN, ctypes.addressof(data)), 1)
            self.assertEqual(len(hook.read_all()), 1)
            hook.active = lambda: False
            self.assertEqual(hook._keyboard_callback(0, impl.WM_KEYUP, ctypes.addressof(data)), 0)
            self.assertEqual(hook.read_all(), [])

    def test_raw_input_uses_signed_hardware_deltas(self):
        raw = _RawInput()
        raw.header.kind = 0
        raw.mouse.x, raw.mouse.y = -8, 12
        self.assertEqual(RawMouseInput._relative_delta(raw), (-8, 12))
        raw.mouse.flags = 1
        self.assertIsNone(RawMouseInput._relative_delta(raw))

    def test_capture_preserves_negative_monitor_origin(self):
        from backend.config.schema import CaptureConfig
        self.assertEqual(CaptureConfig(region=(-1920, -200, 1919, 1079)).clamped_region(),
                         (-1920, -200, 1918, 1078))

    def test_cursor_confinement_converts_logical_to_native_client_pixels_and_releases(self):
        from ctypes import wintypes as w
        from backend.input_capture.windows.raw_mouse import CursorConfinement
        clip = CursorConfinement()
        api = Mock()
        def client(_, ptr):
            rect = ctypes.cast(ptr, ctypes.POINTER(w.RECT)).contents
            rect.right, rect.bottom = 1600, 900
            return True
        def origin(_, ptr):
            point = ctypes.cast(ptr, ctypes.POINTER(w.POINT)).contents
            point.x, point.y = -1600, 100
            return True
        def actual(ptr):
            rect = ctypes.cast(ptr, ctypes.POINTER(w.RECT)).contents
            rect.left, rect.top, rect.right, rect.bottom = -1400, 200, -200, 800
            return True
        clips = []
        def constrain(ptr):
            if ptr is None:
                clips.append(None)
            else:
                rect = ctypes.cast(ptr, ctypes.POINTER(w.RECT)).contents
                clips.append((rect.left, rect.top, rect.right, rect.bottom))
            return True
        api.GetClientRect.side_effect = client
        api.ClientToScreen.side_effect = origin
        api.GetClipCursor.side_effect = actual
        api.ClipCursor.side_effect = constrain
        clip._user = api
        clip.confine(123, (100, 50, 600, 300), (800, 450))
        clip.release()
        self.assertEqual(clips, [(-1400, 200, -200, 800), None])

    def test_raw_mouse_registers_target_and_unregisters_on_release(self):
        from backend.input_capture.windows import raw_mouse as impl
        api = Mock()
        devices = []
        def register(ptr, count, size):
            item = ctypes.cast(ptr, ctypes.POINTER(impl._RawInputDevice)).contents
            devices.append((item.flags, item.target))
            return True
        api.RegisterRawInputDevices.side_effect = register
        with patch.object(impl.c, "WinDLL", return_value=api):
            raw = impl.RawMouseInput()
            self.assertTrue(raw.start(123))
            raw.stop()
            self.assertFalse(raw.active)
        self.assertEqual(devices, [(0x100, 123), (1, None)])


if __name__ == "__main__":
    unittest.main()
