"""Real TCP session negotiation with deterministic media lifecycle doubles."""

from __future__ import annotations

import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QObject, QTimer, Signal
from PySide6.QtNetwork import QTcpSocket

from frontend.desktop.connection import DesktopConnection, PROTOCOL, picture_settings
from frontend.desktop.service import HostSettings, SessionService, ViewerSettings
from frontend.desktop.settings import UiSettings, _merge
from backend.transport.srt import SrtTransport

APP = QCoreApplication.instance() or QCoreApplication([])


def wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        APP.processEvents()
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("Timed out waiting for session state")


class Media(QObject):
    host_prepared = Signal()
    frame_ready = Signal(object)
    state_changed = Signal(str, str)

    def __init__(self):
        super().__init__()
        self.running = False
        self.starts = []
        self.mode = ""
        self.inject = False
        self.fail_next = False

    def start_host(self, settings):
        self.running, self.mode = True, "host"
        self.inject = settings.allow_inject
        self.starts.append(settings)
        if self.fail_next:
            self.fail_next = False
            QTimer.singleShot(0, lambda: self.state_changed.emit("error", "encoder unavailable"))
        else:
            QTimer.singleShot(0, self.host_prepared.emit)

    def start_viewer(self, settings):
        self.running, self.mode = True, "viewer"
        self.starts.append(settings)
        QTimer.singleShot(0, lambda: self.frame_ready.emit(object()))

    def stop(self):
        if self.running:
            self.running, self.mode = False, ""
            QTimer.singleShot(0, lambda: self.state_changed.emit("idle", "stopped"))

    def set_host_control(self, enabled):
        self.inject = enabled


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.host_media, self.viewer_media = Media(), Media()
        self.host = DesktopConnection(self.host_media, "host", HostSettings())
        self.viewer = DesktopConnection(self.viewer_media, "viewer", HostSettings())
        self.assertTrue(self.host.start(0))
        self.assertTrue(self.viewer.start(0))
        self.peer = {"id": "host", "ip": "127.0.0.1",
                     "session_port": self.host.server.serverPort()}
        self.applied, self.errors, self.modes = [], [], []
        self.viewer.picture_applied.connect(self.applied.append)
        self.viewer.error.connect(self.errors.append)
        self.viewer.mode_applied.connect(self.modes.append)

    def tearDown(self):
        self.viewer.close()
        self.host.close()
        APP.processEvents()
        self.viewer.deleteLater()
        self.host.deleteLater()
        APP.processEvents()

    def connect(self, control=True):
        self.viewer.connect_peer(self.peer, ViewerSettings(), control)
        wait_until(lambda: self.viewer.phase == self.host.phase == "active")

    def test_idle_listener_does_not_start_media_and_connection_starts_both_sides(self):
        self.assertFalse(self.host_media.starts)
        self.connect()
        self.assertTrue(self.host_media.inject)
        self.assertEqual(self.host_media.starts[0].srt_mode, "listener")
        self.assertEqual(self.viewer_media.starts[0].srt_mode, "caller")
        self.assertEqual(len(self.applied), 1)

    def test_watch_switch_changes_host_permission_without_restarting_media(self):
        self.connect(False)
        self.assertFalse(self.host_media.inject)
        self.viewer.set_control(True)
        wait_until(lambda: self.modes == [True])
        self.assertTrue(self.host_media.inject)
        self.viewer.set_control(False)
        wait_until(lambda: self.modes == [True, False])
        self.assertFalse(self.host_media.inject)
        self.assertEqual(len(self.host_media.starts), 1)

    def test_picture_restart_preserves_session_and_commits_after_frames(self):
        self.connect()
        old_socket = self.viewer.socket
        picture = {"width": 1280, "height": 720, "fps": 60, "bitrate_kbps": 6500}
        self.viewer.configure(picture)
        wait_until(lambda: len(self.applied) == 2)
        self.assertEqual(self.applied[-1], picture)
        self.assertIs(self.viewer.socket, old_socket)
        self.assertEqual(len(self.host_media.starts), 2)

    def test_invalid_picture_does_not_stop_current_session(self):
        self.connect()
        self.viewer.configure({"width": 0})
        self.assertTrue(self.errors)
        self.assertEqual(self.viewer.phase, "active")
        self.assertEqual(len(self.host_media.starts), 1)

    def test_delayed_idle_from_previous_pipeline_does_not_stop_current_media(self):
        self.connect()
        self.viewer._service_state("idle", "previous pipeline stopped")
        self.viewer._tick()
        self.assertEqual(self.viewer.phase, "active")
        self.assertFalse(self.errors)
        self.viewer_media.running = False
        self.viewer._tick()
        self.assertEqual(self.viewer.phase, "stopping")

    def test_failed_update_rolls_back_previous_picture(self):
        self.connect()
        original = self.applied[-1]
        self.host_media.fail_next = True
        self.viewer.configure({"width": 1280, "height": 720, "fps": 60, "bitrate_kbps": 6500})
        wait_until(lambda: len(self.applied) == 2)
        self.assertEqual(self.applied[-1], original)
        self.assertTrue(self.errors)
        self.assertEqual(self.host_media.starts[-1].width, original["width"])

    def test_disconnect_releases_media_and_listener_accepts_next_connection(self):
        self.connect()
        self.viewer.disconnect()
        self.viewer.disconnect()
        wait_until(lambda: self.viewer.phase == self.host.phase == "idle")
        self.assertFalse(self.host_media.running)
        self.assertFalse(self.viewer_media.running)
        self.assertTrue(self.host.server.isListening())
        self.connect(False)

    def test_wrong_target_is_rejected_without_starting_media(self):
        self.viewer.connect_peer({**self.peer, "id": "wrong"}, ViewerSettings(), True)
        wait_until(lambda: bool(self.errors))
        self.assertFalse(self.host_media.starts)

    def test_busy_host_rejects_second_connection(self):
        self.connect()
        other = DesktopConnection(Media(), "other", HostSettings())
        errors = []
        other.error.connect(errors.append)
        try:
            other.connect_peer(self.peer, ViewerSettings(), True)
            wait_until(lambda: bool(errors))
            self.assertEqual(len(self.host_media.starts), 1)
        finally:
            other.close()
            other.deleteLater()

    def test_fragmented_request_and_invalid_message_cleanup(self):
        sock = QTcpSocket()
        sock.connectToHost("127.0.0.1", self.peer["session_port"])
        wait_until(lambda: self.host.socket is not None)
        payload = json.dumps({"proto": PROTOCOL, "kind": "request", "id": "viewer",
                              "target": "host", "control": False,
                              "picture": {"width": 1280, "height": 720, "fps": 30,
                                          "bitrate_kbps": 4000}}).encode() + b"\n"
        sock.write(payload[:18])
        for _ in range(5):
            APP.processEvents()
        self.assertFalse(self.host_media.starts)
        sock.write(payload[18:])
        wait_until(lambda: bool(self.host_media.starts))
        sock.write(b"not json\n")
        wait_until(lambda: self.host.phase == "idle")
        self.assertFalse(self.host_media.running)
        sock.abort()

    def test_timeout_stops_both_sides(self):
        self.connect()
        self.viewer._last_rx = time.monotonic() - 60
        self.viewer._tick()
        wait_until(lambda: self.host.phase == self.viewer.phase == "idle")
        self.assertTrue(any("超时" in error for error in self.errors))

    def test_failed_start_releases_both_sides(self):
        self.host_media.fail_next = True
        self.viewer.connect_peer(self.peer, ViewerSettings(), True)
        wait_until(lambda: self.host.phase == self.viewer.phase == "idle")
        self.assertTrue(self.errors)
        self.assertFalse(self.host_media.running)


class SettingsAndTransportTests(unittest.TestCase):
    def test_legacy_switches_are_ignored(self):
        settings = _merge(UiSettings, {"allow_discovery": False, "auto_host": True,
                                       "close_to_tray": True})
        self.assertTrue(settings.close_to_tray)
        self.assertFalse(hasattr(settings, "allow_discovery"))

    def test_invalid_picture_values_are_rejected(self):
        picture = {"width": 1920, "height": 1080, "fps": 30, "bitrate_kbps": 4000}
        for update in ({"fps": float("nan")}, {"width": 321}, {"height": True},
                       {"bitrate_kbps": "4000"}):
            with self.assertRaises(ValueError):
                picture_settings({**picture, **update})

    def test_srt_direction_is_independent_of_connection_mode(self):
        with patch("backend.transport.srt.subprocess.Popen") as process:
            sender = SrtTransport("srt://0.0.0.0:9000", mode="listener", direction="send")
            sender.open()
            self.assertIn("pipe:0", process.call_args.args[0])
            self.assertIn("mode=listener", sender.url)
            self.assertIs(sender.writer, process.return_value.stdin)
            receiver = SrtTransport("srt://127.0.0.1:9000", mode="caller", direction="receive")
            receiver.open()
            self.assertIn("pipe:1", process.call_args.args[0])
            self.assertIs(receiver.reader, process.return_value.stdout)


class MediaIntegrationTests(unittest.TestCase):
    def test_srt_picture_update_and_reconnect_with_real_encoder_decoder(self):
        from types import SimpleNamespace
        from frontend.desktop.diagnostics import _synthetic_frame

        class Capture:
            config = SimpleNamespace(fps=30)

            def __init__(self):
                self.index = 0

            def open(self):
                pass

            def read(self):
                frame = _synthetic_frame(self.index)
                self.index += 1
                return frame

            def close(self):
                pass

        host_media, viewer_media = SessionService(), SessionService()
        host = DesktopConnection(host_media, "media-host", HostSettings())
        viewer = DesktopConnection(viewer_media, "media-viewer", HostSettings())
        errors, applied = [], []
        host.error.connect(errors.append)
        viewer.error.connect(errors.append)
        viewer.picture_applied.connect(applied.append)
        self.assertTrue(host.start(0))
        peer = {"id": "media-host", "ip": "127.0.0.1",
                "session_port": host.server.serverPort()}
        settings = ViewerSettings(stream_width=320, stream_height=240,
                                  stream_bitrate_kbps=800)
        with patch("backend.bootstrap.build_capture", side_effect=lambda _: Capture()), patch(
            "frontend.desktop.service._display_region", return_value=(0, 0, 320, 240)
        ):
            try:
                viewer.connect_peer(peer, settings, False)
                wait_until(lambda: viewer.phase == "active" or bool(errors), timeout=25)
                self.assertFalse(errors)
                self.assertEqual(len(applied), 1)
                viewer.configure({"width": 320, "height": 240, "fps": 20, "bitrate_kbps": 1000})
                wait_until(lambda: len(applied) == 2 or bool(errors), timeout=25)
                self.assertFalse(errors)
                self.assertEqual(applied[-1]["fps"], 20)
                viewer.disconnect()
                wait_until(lambda: host.phase == viewer.phase == "idle", timeout=15)
                self.assertFalse(host_media.running)
                self.assertFalse(viewer_media.running)
                viewer.connect_peer(peer, settings, False)
                wait_until(lambda: len(applied) == 3 or bool(errors), timeout=25)
                self.assertFalse(errors)
            finally:
                viewer.close()
                host.close()
                wait_until(lambda: not host_media.running and not viewer_media.running, timeout=15)
                viewer.deleteLater()
                host.deleteLater()
                APP.processEvents()


if __name__ == "__main__":
    unittest.main()
