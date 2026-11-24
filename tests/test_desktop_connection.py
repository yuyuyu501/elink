"""Real TCP session negotiation with deterministic media lifecycle doubles."""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QObject, QTimer, Signal
from PySide6.QtNetwork import QTcpSocket
from PySide6.QtWidgets import QApplication

from frontend.desktop.connection import DesktopConnection, PROTOCOL, picture_settings
from frontend.desktop.service import HostSettings, SessionService, ViewerSettings
from frontend.desktop.settings import UiSettings, _merge

APP = QApplication.instance() or QApplication([])

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
        self.expected_stop = False

    def expect_stop(self, *, stop_media=True):
        self.expected_stop = True

    def start_host(self, settings):
        self.expected_stop = False
        self.running, self.mode = True, "host"
        self.inject = settings.allow_inject
        self.starts.append(settings)
        if self.fail_next:
            self.fail_next = False
            QTimer.singleShot(0, lambda: self.state_changed.emit("error", "encoder unavailable"))
        else:
            QTimer.singleShot(0, self.host_prepared.emit)

    def start_viewer(self, settings):
        self.expected_stop = False
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
        self.assertEqual(self.host_media.starts[0].transport, "webrtc")
        self.assertEqual(self.viewer_media.starts[0].transport, "webrtc")
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

    def test_host_is_marked_before_viewer_closes_and_restart_waits_for_host_worker(self):
        self.connect()
        original_stop = self.viewer_media.stop
        checked = []
        def stop_viewer():
            checked.append(self.host_media.expected_stop)
            original_stop()
        with patch.object(self.viewer_media, "stop", side_effect=stop_viewer):
            self.viewer.configure({"width": 1280, "height": 720, "fps": 120, "bitrate_kbps": 4000})
            wait_until(lambda: len(self.applied) == 2 and self.host.phase == "active")
        self.assertTrue(checked)
        self.assertTrue(all(checked))
        self.assertFalse(self.errors)

    def test_invalid_picture_does_not_stop_current_session(self):
        self.connect()
        self.viewer.configure({"width": 0})
        self.assertTrue(self.errors)
        self.assertEqual(self.viewer.phase, "active")
        self.assertEqual(len(self.host_media.starts), 1)

    def test_picture_applies_50_mbps_to_both_sides(self):
        self.connect()
        picture = {"width": 1920, "height": 1080, "fps": 90, "bitrate_kbps": 50000}
        self.viewer.configure(picture)
        wait_until(lambda: len(self.applied) == 2)
        self.assertEqual(self.applied[-1], picture)
        self.assertEqual(self.host_media.starts[-1].bitrate_kbps, 50000)
        self.assertEqual(self.viewer_media.starts[-1].stream_bitrate_kbps, 50000)
        self.assertFalse(self.errors)

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

    def test_remote_failure_transmits_original_diagnostic_under_same_fault_id(self):
        from frontend.desktop import error_report
        from frontend.desktop.paths import error_dir
        self.connect()
        try:
            raise OSError(22, "host source failure")
        except OSError as exc:
            exc.process_diagnostics = {"exit_code": 22, "stderr": "host stderr"}
            self.host_media.failure_id = error_report.save_error(exc)
            self.host_media.failure_message = str(exc)
            self.host_media.failure_details = error_report.error_details(exc)
        self.host_media.state_changed.emit("error", self.host_media.failure_message)
        wait_until(lambda: self.viewer.phase == self.host.phase == "idle")
        self.assertEqual(self.viewer.fault_id, self.host_media.failure_id)
        reports = "\n".join(path.read_text(encoding="utf-8") for path in error_dir().glob("*.txt"))
        self.assertIn('"remote"', reports)
        self.assertIn("host source failure", reports)
        self.assertIn("host stderr", reports)

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
                              "target": "host", "control": False, "transport": "webrtc",
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

    def test_srt_only_peer_is_rejected_before_media_starts(self):
        sock = QTcpSocket()
        sock.connectToHost("127.0.0.1", self.peer["session_port"])
        wait_until(lambda: self.host.socket is not None)
        sock.write(DesktopConnection._encode(
            "request", id="legacy", target="host", control=False, transport="srt",
            picture={"width": 1280, "height": 720, "fps": 30, "bitrate_kbps": 4000}))
        wait_until(lambda: self.host.phase == "idle" and sock.canReadLine())
        replies = [json.loads(line) for line in bytes(sock.readAll()).splitlines()]
        reply = next(item for item in replies if item["kind"] == "error")
        self.assertIn("WebRTC", reply["message"])
        self.assertFalse(self.host_media.starts)
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
    def test_saved_disabled_input_preferences_and_decoder_are_migrated(self):
        from frontend.desktop.settings import load
        with patch("frontend.desktop.settings._read", return_value={
            "host": {"clipboard_enabled": False, "gamepad_enabled": False},
            "viewer": {"clipboard_enabled": False, "gamepad_enabled": False,
                       "decoder": "h264_pyav", "codec_preferences": {
                           "peer": {"codec": "h264", "decoder": "h264_pyav"}},
                       "protocol_preferences": {"peer": "srt", "invalid": "other"}}
        }):
            host, viewer = load()
        self.assertTrue(host.clipboard_enabled and host.gamepad_enabled)
        self.assertTrue(viewer.clipboard_enabled and viewer.gamepad_enabled)
        self.assertEqual(viewer.decoder, "h264")
        self.assertEqual(viewer.codec_preferences["peer"], {"codec": "h264"})
        self.assertEqual(viewer.transport, "webrtc")
        self.assertFalse(hasattr(viewer, "protocol_preferences"))

    def test_legacy_upscale_fields_are_removed_when_settings_are_saved(self):
        from frontend.desktop import settings
        picture = {"width": 2560, "height": 1440, "fps": 120, "bitrate_kbps": 30000}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "desktop.json"
            path.write_text(json.dumps({"viewer": {"sr_enabled": True, "sr_percent": 50,
                "picture_preferences": {"peer": {**picture, "sr_mode": "rtx", "sr_percent": 50}},
                "codec_preferences": {"peer": {"codec": "hevc"}}}}), encoding="utf-8")
            with patch.object(settings, "settings_file", return_value=path):
                host, viewer = settings.load()
                device = settings.for_device(viewer, "peer")
                self.assertEqual((device.stream_width, device.stream_height, device.stream_fps,
                                  device.stream_bitrate_kbps), (2560, 1440, 120, 30000))
                settings.save(host, viewer)
            saved = json.loads(path.read_text(encoding="utf-8"))["viewer"]
            self.assertNotIn("sr_enabled", saved)
            self.assertEqual(saved["picture_preferences"]["peer"], picture)
            self.assertEqual(saved["codec_preferences"]["peer"], {"codec": "hevc"})

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

    def test_picture_bitrate_range_preserves_legacy_minimum(self):
        picture = {"width": 1920, "height": 1080, "fps": 30, "bitrate_kbps": 50000}
        for value in (300, 1000, 50000):
            self.assertEqual(picture_settings({**picture, "bitrate_kbps": value})["bitrate_kbps"],
                             value)
        for value in (299, 50001, float("inf")):
            with self.assertRaises(ValueError):
                picture_settings({**picture, "bitrate_kbps": value})

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
        self.assertEqual(self.viewer._viewer.codec_preferences["host"], {"codec": "hevc"})

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
        self.assertEqual(self.viewer._viewer.codec_preferences["host"], {"codec": "h264"})
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
        self.viewer._message({"kind": "rtc", "session": self.viewer._session_id,
                              "revision": self.viewer._revision - 1})
        self.assertEqual(self.viewer.phase, "active")
        self.viewer_media.delay_validation = True
        self.viewer.configure({**self.applied[-1], "fps": 90})
        self.assertEqual(self.viewer.phase, "validating")
        revision = self.viewer._revision
        self.viewer.disconnect()
        wait_until(lambda: self.viewer.phase == self.host.phase == "idle")
        self.viewer_media.codec_validated.emit(revision, "")
        self.assertEqual(self.viewer.phase, "idle")
        self.assertEqual(len(self.host_media.starts), 1)

    def test_peer_notifications_during_encoder_failure_keep_recovery_pending(self):
        self.connect()
        self.host.phase = "preparing"
        self.host._failed_media("encoder failed")
        pending = self.host._pending_action
        self.assertEqual(self.host.phase, "negotiating")
        self.assertIsNotNone(pending)
        for kind in ("media_failed", "active"):
            self.host._message({"kind": kind, "session": self.host._session_id,
                                "revision": self.host._revision, "message": "receiver closed"})
            self.assertEqual(self.host.phase, "negotiating")
            self.assertIs(self.host._pending_action, pending)

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


class LiveMedia(CapableMedia):
    live_updated = Signal(int, str)

    def __init__(self):
        super().__init__()
        self.updates = []
        self.fail_update = False

    def update_live(self, revision, picture, selection, viewer=None):
        self.updates.append((dict(picture), dict(selection), viewer))
        reason = "live update rejected" if self.fail_update else ""
        self.fail_update = False
        QTimer.singleShot(0, lambda: self.live_updated.emit(revision, reason))


class LiveConnectionTests(unittest.TestCase):
    connect = ConnectionTests.connect
    tearDown = ConnectionTests.tearDown

    def setUp(self):
        with patch(f"{__name__}.Media", LiveMedia):
            ConnectionTests.setUp(self)

    def deliver_live_frame(self, width, height):
        from types import SimpleNamespace

        wait_until(lambda: self.viewer.phase == "receiving", detail=lambda: self.errors)
        self.viewer_media.frame_ready.emit(SimpleNamespace(width=width, height=height))
        self.assertEqual(self.viewer.phase, "active")

    def test_unchanged_picture_does_nothing_and_live_update_keeps_media(self):
        self.connect()
        self.viewer.configure(self.applied[-1])
        self.assertFalse(self.viewer_media.updates)
        picture = dict(self.applied[-1], width=640, height=360, fps=120)
        self.viewer.configure(picture)
        self.deliver_live_frame(640, 360)
        self.assertEqual(len(self.applied), 2)
        self.assertEqual(self.applied[-1], picture)
        self.assertEqual(len(self.host_media.starts), 1)
        self.assertEqual(len(self.viewer_media.starts), 1)
        self.assertEqual(len(self.host_media.updates), 1)
        self.assertFalse(self.errors)

    def test_host_failure_restores_viewer_and_keeps_connection(self):
        self.connect()
        previous = dict(self.applied[-1])
        self.host_media.fail_update = True
        self.viewer.configure(dict(previous, fps=120))
        wait_until(lambda: bool(self.errors) and self.viewer.phase == "active")
        self.assertEqual(self.viewer._picture, previous)
        self.assertEqual(len(self.viewer_media.updates), 2)
        self.assertTrue(self.viewer_media.running and self.host_media.running)
        self.assertEqual(len(self.host_media.starts), 1)

    def test_local_decoder_switch_does_not_restart_media(self):
        self.connect()
        picture = self.applied[-1]
        self.viewer.configure(dict(picture, codec="h264", encoder="h264_nvenc", decoder="h264"))
        self.deliver_live_frame(picture["width"], picture["height"])
        self.assertEqual(len(self.applied), 2)
        self.assertEqual(self.viewer._selection["decoder"], "h264")
        self.assertEqual(len(self.host_media.starts), 1)
        self.assertEqual(len(self.viewer_media.starts), 1)
        self.assertFalse(self.errors)


class MediaIntegrationTests(unittest.TestCase):
    def test_webrtc_reconfigures_real_media_and_recovers_failed_encoder(self):
        from scripts.benchmark_remote import run
        from backend.media.video.codec.inprocess import InprocessEncoder
        original = InprocessEncoder.encode
        def encode(adapter, frame):
            if adapter.stream.width == 352 and threading.current_thread().name.startswith("asyncio"):
                raise RuntimeError("injected encoder failure")
            return original(adapter, frame)
        with patch.object(InprocessEncoder, "encode", encode):
            result = run(seconds=.2, width=320, height=240, fps=60, synthetic=True,
                         warmup=0, display="cpu",
                         updates=[(640, 360, 120), (352, 240, 90), (320, 240, 30)])
        self.assertEqual(len(result["updates"]), 3, result)
        for change, size in zip(result["updates"], ([640, 360], [640, 360], [320, 240])):
            self.assertEqual(change["phases"], ["active", "active"], change)
            self.assertEqual(change["last_frame_size"], size)
            self.assertGreaterEqual(change["frames"], 10)
            self.assertTrue(change["transport_preserved"])
            self.assertTrue(change["control_preserved"])
            self.assertTrue(change["audio_preserved"])
            self.assertTrue(change["pts_monotonic"])
        self.assertTrue(result["closed_cleanly"])
        self.assertTrue(result["workers_stopped"])
        self.assertTrue(any("injected encoder failure" in error for error in result["errors"]),
                        result["errors"])

    def test_webrtc_uses_real_media_and_stops_workers(self):
        from scripts.benchmark_remote import run
        result = run(seconds=.5, width=320, height=240, fps=60, synthetic=True,
                     warmup=0, display="cpu", decoder="h264")
        self.assertFalse(result["errors"])
        self.assertGreater(result["frames"], 10)
        self.assertTrue(result["closed_cleanly"])
        self.assertTrue(result["workers_stopped"])
        self.assertEqual(result["host"]["settings"]["transport"], "webrtc")
        self.assertEqual(result["viewer"]["settings"]["decoder"], "h264")
        measurement = result["measurement"]
        self.assertEqual(measurement["display_mode"], "cpu")
        self.assertGreater(measurement["drawn_fps"], 0)
        self.assertLessEqual(measurement["drawn_fps"], measurement["delivered_fps"] + 2)

if __name__ == "__main__":
    unittest.main()
