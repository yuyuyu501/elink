"""Real TCP session negotiation with deterministic media lifecycle doubles."""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
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

def setUpModule():
    global _test_data, _test_environment
    _test_data = tempfile.TemporaryDirectory()
    _test_environment = patch.dict(os.environ, ELINK_DATA_DIR=_test_data.name)
    _test_environment.start()


def tearDownModule():
    _test_environment.stop()
    _test_data.cleanup()


def wait_until(predicate, timeout=3, detail=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        APP.processEvents()
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError(f"Timed out waiting for session state: {detail() if detail else ''}")


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
        from frontend.desktop.paths import error_dir
        reports = set(error_dir().glob("*.txt"))
        self.connect()
        old_socket = self.viewer.socket
        picture = {"width": 1280, "height": 720, "fps": 60, "bitrate_kbps": 6500}
        self.viewer.configure(picture)
        wait_until(lambda: len(self.applied) == 2)
        self.assertEqual(self.applied[-1], picture)
        self.assertIs(self.viewer.socket, old_socket)
        self.assertEqual(len(self.host_media.starts), 2)
        self.viewer.disconnect()
        wait_until(lambda: self.viewer.phase == self.host.phase == "idle")
        new_reports = set(error_dir().glob("*.txt")) - reports
        self.assertFalse(new_reports, "\n".join(path.read_text(encoding="utf-8")
                                               for path in new_reports))

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

    def test_either_side_and_simultaneous_close_are_normal_and_reconnectable(self):
        from frontend.desktop.paths import error_dir
        reports = set(error_dir().glob("*.txt"))
        host_errors = []
        self.host.error.connect(host_errors.append)
        for initiator in ("host", "viewer", "both"):
            self.connect()
            session_id = self.viewer._session_id
            self.assertTrue(session_id)
            self.assertEqual(session_id, self.host._session_id)
            if initiator in ("viewer", "both"):
                self.viewer.disconnect()
            if initiator in ("host", "both"):
                self.host.disconnect()
            # Late media errors during an acknowledged shutdown are expected.
            for connection in (self.viewer, self.host):
                if connection.phase == "stopping":
                    connection._service_state("error", "closed")
            wait_until(lambda: self.viewer.phase == self.host.phase == "idle")
            self.assertFalse(self.viewer_media.running or self.host_media.running)
        self.assertFalse(self.errors or host_errors)
        self.assertEqual(set(error_dir().glob("*.txt")), reports)

    def test_cancel_while_connecting_or_updating_does_not_restart_media(self):
        from frontend.desktop.paths import error_dir
        reports = set(error_dir().glob("*.txt"))
        self.viewer.connect_peer(self.peer, ViewerSettings(), True)
        self.viewer.disconnect()
        wait_until(lambda: self.viewer.phase == self.host.phase == "idle")
        self.connect()
        self.viewer.configure({"width": 1280, "height": 720, "fps": 120, "bitrate_kbps": 6500})
        self.viewer.disconnect()
        wait_until(lambda: self.viewer.phase == self.host.phase == "idle")
        starts = len(self.host_media.starts)
        for _ in range(5):
            APP.processEvents()
        self.assertEqual(len(self.host_media.starts), starts)
        self.assertEqual(set(error_dir().glob("*.txt")), reports)
        self.assertFalse(self.errors)

    def test_unexpected_socket_loss_is_reported_and_old_session_messages_are_ignored(self):
        self.connect()
        self.viewer._message({"kind": "bye", "session": "old-session"})
        self.assertEqual(self.viewer.phase, "active")
        self.viewer.socket.abort()
        wait_until(lambda: self.host.phase == self.viewer.phase == "idle")
        self.assertTrue(self.viewer.fault_id or self.host.fault_id)

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
        self.assertTrue(self.viewer.fault_id)
        self.assertEqual(self.host.fault_id, self.viewer.fault_id)
        from frontend.desktop.paths import error_dir
        text = "\n".join(path.read_text(encoding="utf-8") for path in error_dir().glob("*.txt"))
        self.assertIn('"source": "remote:viewer"', text)

    def test_recovery_starts_a_new_fault_after_rollback(self):
        from frontend.desktop.paths import error_dir
        self.connect()
        self.host_media.fail_next = True
        picture = {"width": 1280, "height": 720, "fps": 30, "bitrate_kbps": 4000}
        self.viewer.configure(picture)
        wait_until(lambda: len(self.applied) == 2 and self.host.phase == "active")
        previous_reports = set(error_dir().glob("*.txt"))
        self.assertEqual(self.host.fault_id, "")
        self.assertEqual(self.viewer.fault_id, "")
        self.assertEqual(self.host._fault_source, "local")
        self.viewer._last_rx = time.monotonic() - 60
        self.viewer._tick()
        wait_until(lambda: self.viewer.phase == self.host.phase == "idle")
        self.assertEqual(len(set(error_dir().glob("*.txt")) - previous_reports), 1)

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

    def test_picture_accepts_120_fps_and_rejects_out_of_range(self):
        picture = {"width": 1920, "height": 1080, "fps": 120, "bitrate_kbps": 4000}
        self.assertEqual(picture_settings(picture)["fps"], 120)
        for value in (4, 121, float("inf")):
            with self.assertRaises(ValueError):
                picture_settings({**picture, "fps": value})
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

    @unittest.skipUnless(os.name == "nt", "Windows process tree")
    def test_srt_abort_stops_launcher_children(self):
        with patch("backend.transport.srt.subprocess.Popen") as process, patch(
            "backend.transport.srt.subprocess.run"
        ) as terminate:
            transport = SrtTransport("srt://127.0.0.1:9000")
            transport._proc = process.return_value
            transport._proc.poll.side_effect = [None, 0]
            transport.abort()
            self.assertEqual(terminate.call_args.args[0],
                             ["taskkill", "/PID", str(transport._proc.pid), "/T", "/F"])


class CapableMedia(Media):
    capabilities_changed = Signal()
    codec_validated = Signal(int, str)

    def __init__(self):
        super().__init__()
        self.codec_capabilities = {
            "encoders": {name: {"codec": codec, "state": "available"} for name, codec in (
                ("h264_nvenc", "h264"), ("libx264", "h264"), ("libx265", "hevc"), ("av1_nvenc", "av1"))},
            "decoders": {name: {"codec": codec, "state": "available"} for name, codec in (
                ("h264_qsv", "h264"), ("h264", "h264"), ("hevc", "hevc"), ("av1", "av1"))},
        }
        self.known_capabilities = self.codec_capabilities
        self.validation_failure = None
        self.validations = []
        self.delay_validation = False

    def refresh_codecs(self, _path="ffmpeg", *, refresh=False):
        if self.codec_capabilities is None:
            self.codec_capabilities = self.known_capabilities
        QTimer.singleShot(0, self.capabilities_changed.emit)

    def validate_codec(self, revision, kind, selection, picture, path):
        self.validations.append((revision, kind, dict(selection), dict(picture)))
        reason = "hardware unavailable" if self.validation_failure == (kind, selection[kind]) else ""
        if not self.delay_validation:
            QTimer.singleShot(0, lambda: self.codec_validated.emit(revision, reason))

    def cancel_codec_validation(self, **_):
        pass

    def start_viewer(self, settings):
        if self.fail_next:
            self.fail_next = False
            self.running, self.mode = True, "viewer"
            self.starts.append(settings)
            QTimer.singleShot(0, lambda: self.state_changed.emit("error", "decoder failed after preflight"))
        else:
            super().start_viewer(settings)


class CodecConnectionTests(unittest.TestCase):
    connect = ConnectionTests.connect
    tearDown = ConnectionTests.tearDown

    def setUp(self):
        with patch(f"{__name__}.Media", CapableMedia):
            ConnectionTests.setUp(self)

    def test_initial_capability_exchange_prefers_verified_hardware(self):
        self.connect()
        self.assertEqual(self.host_media.starts[0].encoder, "h264_nvenc")
        self.assertEqual(self.viewer_media.starts[0].decoder, "h264_qsv")
        self.assertEqual(self.host._selection, self.viewer._selection)
        self.assertTrue(self.viewer._codec_enabled)

    def test_explicit_format_and_backends_apply_then_save(self):
        self.connect()
        options = {"codec": "hevc", "encoder": "libx265", "decoder": "hevc"}
        picture = {"width": 1280, "height": 720, "fps": 120, "bitrate_kbps": 6000}
        self.viewer.configure({**picture, **options})
        wait_until(lambda: len(self.applied) == 2 and self.host.phase == "active")
        self.assertEqual(self.applied[-1], picture)
        self.assertEqual(self.host_media.starts[-1].codec, "hevc")
        self.assertEqual(self.viewer_media.starts[-1].decoder, "hevc")
        self.assertEqual(self.viewer._viewer.codec_preferences["host"], options)

    def test_auto_preflight_failure_tries_next_encoder(self):
        self.host_media.validation_failure = ("encoder", "h264_nvenc")
        self.connect()
        self.assertEqual(self.host_media.starts[-1].encoder, "libx264")
        self.assertFalse(self.errors)

    def test_auto_preflight_failure_tries_next_decoder(self):
        self.viewer_media.validation_failure = ("decoder", "h264_qsv")
        self.connect()
        self.assertEqual(self.viewer_media.starts[-1].decoder, "h264")
        self.assertFalse(self.errors)

    def test_decode_runtime_failure_tries_another_pair_and_reaches_first_frame(self):
        self.viewer_media.fail_next = True
        self.connect()
        self.assertEqual(len(self.viewer_media.starts), 2)
        self.assertEqual(self.viewer_media.starts[-1].decoder, "h264")
        self.assertFalse(self.errors)

    def test_explicit_failure_restores_previous_picture_and_preferences(self):
        self.connect()
        original = dict(self.applied[-1])
        self.host_media.validation_failure = ("encoder", "libx265")
        self.viewer.configure({**original, "codec": "hevc", "encoder": "libx265", "decoder": "hevc"})
        wait_until(lambda: len(self.applied) == 2 and self.host.phase == "active")
        self.assertEqual(self.applied[-1], original)
        self.assertEqual(self.host_media.starts[-1].encoder, "h264_nvenc")
        self.assertEqual(self.viewer._viewer.codec_preferences["host"]["encoder"], "auto")
        self.assertTrue(self.errors)

    def test_local_preflight_rejection_keeps_existing_host_and_selection(self):
        self.connect()
        original = dict(self.viewer._selection)
        self.viewer_media.validation_failure = ("decoder", "hevc")
        picture = dict(self.applied[-1], width=1280)
        self.viewer.configure({**picture, "codec": "hevc", "encoder": "libx265", "decoder": "hevc"})
        wait_until(lambda: self.viewer.phase == "active")
        self.assertEqual(len(self.host_media.starts), 1)
        self.assertEqual(self.viewer._selection, original)
        self.assertEqual(self.viewer._picture, self.applied[-1])

    def test_new_viewer_connects_to_old_host_without_codec_extension(self):
        self.host.close()
        self.host.deleteLater()
        self.host_media = Media()
        self.host = DesktopConnection(self.host_media, "host", HostSettings())
        self.assertTrue(self.host.start(0))
        self.peer["session_port"] = self.host.server.serverPort()
        self.connect()
        self.assertFalse(self.viewer._codec_enabled)
        self.assertEqual(self.viewer_media.starts[-1].decoder, "h264")

    def test_disconnect_during_host_validation_and_stale_callback(self):
        self.connect()
        self.host_media.delay_validation = True
        self.viewer.configure(dict(self.applied[-1], codec="hevc", encoder="libx265", decoder="hevc"))
        wait_until(lambda: self.host.phase == "validating")
        revision = self.host._revision
        self.viewer.disconnect()
        wait_until(lambda: self.viewer.phase == self.host.phase == "idle")
        self.host_media.codec_validated.emit(revision, "")
        self.assertEqual(len(self.host_media.starts), 1)

    def test_refresh_stops_media_then_exchanges_capabilities_and_restarts(self):
        self.connect()
        self.viewer.refresh_codecs()
        wait_until(lambda: len(self.applied) == 2 and self.host.phase == "active")
        self.assertEqual(len(self.host_media.starts), 2)
        self.assertFalse(self.errors)

    def test_stale_revision_and_late_validation_after_disconnect_are_ignored(self):
        self.connect()
        self.viewer._message({"kind": "ready", "session": self.viewer._session_id,
                              "revision": self.viewer._revision - 1})
        self.assertEqual(self.viewer.phase, "active")
        self.viewer_media.delay_validation = True
        self.viewer.configure(self.applied[-1])
        self.assertEqual(self.viewer.phase, "validating")
        revision = self.viewer._revision
        self.viewer.disconnect()
        wait_until(lambda: self.viewer.phase == self.host.phase == "idle")
        self.viewer_media.codec_validated.emit(revision, "")
        self.assertEqual(self.viewer.phase, "idle")
        self.assertEqual(len(self.host_media.starts), 1)

    def test_unavailable_saved_choice_waits_for_user_without_overwriting_it(self):
        options = {"codec": "hevc", "encoder": "hevc_amf", "decoder": "hevc"}
        settings = ViewerSettings(codec_preferences={"host": options})
        self.viewer.connect_peer(self.peer, settings, True)
        wait_until(lambda: self.viewer.phase == self.host.phase == "selection")
        self.assertEqual(settings.codec_preferences["host"], options)
        self.assertFalse(self.host_media.starts)
        self.viewer.configure({**self.viewer._picture, "codec": "auto", "encoder": "auto", "decoder": "auto"})
        wait_until(lambda: self.viewer.phase == self.host.phase == "active")
        self.assertEqual(self.host_media.starts[-1].encoder, "h264_nvenc")


class MediaIntegrationTests(unittest.TestCase):
    def test_srt_picture_update_and_reconnect_with_real_encoder_decoder(self):
        from types import SimpleNamespace
        from frontend.desktop.diagnostics import _synthetic_frame
        from frontend.desktop.paths import error_dir

        reports = set(error_dir().glob("*.txt"))

        class Capture:
            def __init__(self, config):
                self.index = 0
                self.config = config
                self.cursor_mode = self.cursor_applied_mode = "smart"
                self.cursor_visible = True

            def open(self):
                pass

            def read(self):
                self.cursor_applied_mode = self.cursor_mode
                frame = _synthetic_frame(self.index)
                self.index += 1
                return frame

            def close(self):
                pass

        host_media, viewer_media = SessionService(), SessionService()
        host = DesktopConnection(host_media, "media-host", HostSettings())
        viewer = DesktopConnection(viewer_media, "media-viewer", HostSettings())
        errors, applied = [], []
        cursors, frame_pts = [], []
        viewer_media.cursor_changed.connect(cursors.append)
        viewer_media.frame_ready.connect(lambda frame: frame_pts.append(frame.pts))
        host.error.connect(errors.append)
        viewer.error.connect(errors.append)
        viewer.picture_applied.connect(applied.append)
        # Client ephemeral ports must not occupy the adjacent control port.
        for port in range(23000, 23100, 3):
            try:
                with socket.socket() as session_probe, socket.socket() as control_probe, socket.socket(
                    socket.AF_INET, socket.SOCK_DGRAM
                ) as srt_probe:
                    session_probe.bind(("0.0.0.0", port))
                    control_probe.bind(("0.0.0.0", port + 1))
                    srt_probe.bind(("0.0.0.0", port + 2))
                self.assertTrue(host.start(port))
                break
            except OSError:
                continue
        else:
            host.close()
            viewer.close()
            self.fail("No available session/control/SRT port group")
        peer = {"id": "media-host", "ip": "127.0.0.1",
                "session_port": host.server.serverPort()}
        settings = ViewerSettings(stream_width=320, stream_height=240,
                                  stream_bitrate_kbps=800, clipboard_enabled=False,
                                  gamepad_enabled=False)
        injected = []
        def state():
            return {
                "host": (host.phase, host_media.running),
                "viewer": (viewer.phase, viewer_media.running),
                "stderr": [service._transport.stderr_text() if service._transport else ""
                           for service in (host_media, viewer_media)],
                "errors": errors,
            }
        with patch("backend.bootstrap.build_capture", side_effect=lambda config: Capture(config.capture)), patch(
            "frontend.desktop.service._display_region", return_value=(0, 0, 320, 240)
        ), patch.object(host_media, "_inject_remote_input",
                        side_effect=lambda event: injected.append(event) or True), patch.object(
            host_media, "_start_clipboard"
        ), patch.object(host_media, "_start_gamepad_host"):
            try:
                viewer.connect_peer(peer, settings, False)
                wait_until(lambda: viewer.phase == "active" or bool(errors), timeout=25)
                self.assertFalse(errors)
                self.assertEqual(len(applied), 1)
                viewer_media.set_mouse_mode("local")
                wait_until(lambda: cursors and cursors[-1]["mode"] == "local")
                boundary = cursors[-1]["pts"]
                wait_until(lambda: frame_pts and frame_pts[-1] >= boundary)
                self.assertLess(frame_pts[-1] - boundary, 90000)
                wait_until(lambda: "rtt_ms" in viewer_media.stats.extra)
                self.assertGreaterEqual(viewer_media.stats.extra["rtt_ms"], 0)
                from backend.control_channel.protocol import EventType, InputEvent
                from backend.control_channel.base import ChannelState
                wait_until(lambda: viewer_media._control is not None
                           and viewer_media._control.state == ChannelState.OPEN)
                self.assertFalse(viewer_media.set_input_focus(True))
                viewer.set_control(True)
                wait_until(lambda: host_media._host_live.allow_inject)
                viewer_media.set_control_enabled(True, viewer_media._viewer_live)
                self.assertTrue(viewer_media.set_input_focus(True))
                self.assertTrue(viewer_media.send_input(InputEvent(
                    EventType.MOUSE_MOVE, x=65535, y=65535, extra={"coordinates": "normalized"})))
                self.assertTrue(viewer_media.send_input(InputEvent(
                    EventType.MOUSE_BUTTON, button=1, pressed=True, extra={"positioned": True})))
                wait_until(lambda: len(injected) == 2)
                viewer.set_control(False)
                wait_until(lambda: len(injected) == 3)
                self.assertEqual([event.event_type for event in injected],
                                 [EventType.MOUSE_MOVE, EventType.MOUSE_BUTTON, EventType.MOUSE_BUTTON])
                self.assertFalse(injected[-1].pressed)
                viewer_media.set_control_enabled(False, viewer_media._viewer_live)
                viewer.configure({"width": 320, "height": 240, "fps": 20, "bitrate_kbps": 1000})
                wait_until(lambda: len(applied) == 2 or bool(errors), timeout=25, detail=state)
                self.assertFalse(errors)
                self.assertEqual(applied[-1]["fps"], 20)
                viewer.disconnect()
                wait_until(lambda: host.phase == viewer.phase == "idle", timeout=15)
                self.assertFalse(host_media.running)
                self.assertFalse(viewer_media.running)
                viewer.connect_peer(peer, settings, False)
                wait_until(lambda: len(applied) == 3 or bool(errors), timeout=25)
                self.assertFalse(errors)
                for initiator in ("host", "both"):
                    viewer.configure({"width": 320, "height": 240, "fps": 120, "bitrate_kbps": 1000})
                    count = len(applied)
                    wait_until(lambda: len(applied) > count or bool(errors), timeout=25, detail=state)
                    self.assertFalse(errors)
                    self.assertEqual(host_media._pipeline.capture.config.fps, 120)
                    if initiator == "both":
                        viewer.disconnect()
                    host.disconnect()
                    wait_until(lambda: host.phase == viewer.phase == "idle", timeout=15, detail=state)
                    viewer.connect_peer(peer, settings, False)
                    count = len(applied)
                    wait_until(lambda: len(applied) > count or bool(errors), timeout=25, detail=state)
                    self.assertFalse(errors)
                for codec in ("h264", "hevc", "av1"):
                    encoders = [n for n, v in host_media.codec_capabilities["encoders"].items()
                                if v["codec"] == codec]
                    decoders = [n for n, v in viewer_media.codec_capabilities["decoders"].items()
                                if v["codec"] == codec]
                    if not encoders or not decoders:
                        print(f"No runtime pair for {codec}; transport fixture still covers this format.")
                        continue
                    count = len(applied)
                    viewer.configure({
                        "width": 320, "height": 240, "fps": 120, "bitrate_kbps": 1000,
                        "codec": codec, "encoder": encoders[0], "decoder": decoders[0],
                    })
                    wait_until(lambda: len(applied) > count or bool(errors), timeout=25, detail=state)
                    self.assertFalse(errors)
                    self.assertEqual(host_media._pipeline.stream.codec.value, codec)
                    self.assertEqual(viewer_media._pipeline.stream.codec.value, codec)
                    self.assertEqual(viewer_media._pipeline.stats.extra["decoder"], decoders[0])
                count = len(applied)
                viewer.refresh_codecs()
                wait_until(lambda: len(applied) > count or bool(errors), timeout=25, detail=state)
                self.assertFalse(errors)
            finally:
                viewer.close()
                host.close()
                wait_until(lambda: not host_media.running and not viewer_media.running, timeout=15)
                viewer.deleteLater()
                host.deleteLater()
                APP.processEvents()
        new_reports = set(error_dir().glob("*.txt")) - reports
        self.assertFalse(new_reports, "\n".join(path.read_text(encoding="utf-8")
                                               for path in new_reports))


if __name__ == "__main__":
    unittest.main()
