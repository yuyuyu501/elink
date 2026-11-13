"""Device navigation and connection modes without opening network sessions."""

from __future__ import annotations

import os
import sys
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QApplication, QPushButton

from frontend.desktop import icons
from frontend.desktop.discovery import DiscoveryService
from frontend.desktop.connection import DesktopConnection
from frontend.desktop.service import HostSettings, SessionService, ViewerSettings
from frontend.desktop.settings import UiSettings
from frontend.desktop.theme import apply_theme
from frontend.desktop.ui.devices_page import ComputerName, DevicesPage
from frontend.desktop.ui.window import MainWindow, PAGE_INDEX
from frontend.desktop.ui.stream_window import PictureDialog

APP = QApplication.instance() or QApplication([])
apply_theme(APP)

PEER = {
    "id": "a" * 16,
    "name": "Study computer",
    "ip": "192.168.1.108",
    "platform": "Windows 11",
    "version": "0.6.0",
    "hosting": True,
    "srt_port": 9000,
    "control_port": 7000,
    "session_port": 49321,
    "busy": False,
}


class DevicePageTests(unittest.TestCase):
    def setUp(self):
        self.page = DevicesPage()
        self.page.set_peers([PEER])
        self.requests = []
        self.page.connect_peer.connect(lambda peer, control: self.requests.append((peer, control)))

    def tearDown(self):
        self.page.close()
        self.page.deleteLater()
        APP.processEvents()

    def test_row_opens_detail_without_connecting_and_back_returns_to_list(self):
        self.page.rows[1].open_button.click()
        self.assertEqual(self.page.stack.currentIndex(), 1)
        self.assertFalse(self.requests)
        self.page.back_button.click()
        self.assertEqual(self.page.stack.currentIndex(), 0)

    def test_control_and_watch_are_distinct_connection_requests(self):
        self.page.open_peer(PEER["id"])
        self.page.enter_button.click()
        self.page.watch_button.click()
        self.assertEqual([control for _, control in self.requests], [True, False])

    def test_detail_toolbar_keeps_only_file_and_watch_actions(self):
        buttons = self.page.findChildren(QPushButton, "deviceTool")
        self.assertEqual([button.text().strip() for button in buttons], ["文件传输", "观看模式"])

    def test_keyboard_focus_highlights_entire_row(self):
        self.page.show()
        row = self.page.rows[1]
        for button in (row.open_button, row.info_button, row.arrow_button):
            button.setFocus()
            APP.processEvents()
            self.assertTrue(row.property("focused"))
            self.assertEqual(button.palette().button().color().alpha(), 0)
        self.page.search.setFocus()
        APP.processEvents()
        self.assertFalse(row.property("focused"))

    def test_offline_device_cannot_connect_and_reappearance_recovers(self):
        self.page.open_peer(PEER["id"])
        self.page.set_peers([])
        self.assertFalse(self.page.enter_button.isEnabled())
        self.page._request_connection(True)
        self.assertFalse(self.requests)
        self.assertEqual(self.page.stack.currentIndex(), 1)
        self.page.set_peers([PEER])
        self.assertTrue(self.page.enter_button.isEnabled())

    def test_idle_peer_connects_without_preexisting_stream(self):
        self.page.set_peers([{**PEER, "hosting": False, "srt_port": 0}])
        self.page.rows[1].arrow_button.click()
        self.assertEqual(self.page.stack.currentIndex(), 1)
        self.assertTrue(self.page.enter_button.isEnabled())
        self.assertTrue(self.page.watch_button.isEnabled())

    def test_busy_or_unready_peer_has_no_connection_action(self):
        self.page.open_peer(PEER["id"])
        for update in ({"busy": True}, {"session_port": 0}):
            self.page.set_peers([{**PEER, **update}])
            self.assertFalse(self.page.enter_button.isEnabled())

    def test_connection_uses_latest_discovered_address_and_port(self):
        self.page.open_peer(PEER["id"])
        updated = {**PEER, "ip": "192.168.1.110", "srt_port": 9100}
        self.page.set_peers([updated])
        self.page.enter_button.click()
        self.assertEqual(self.requests[0][0], updated)

    def test_disabled_discovery_prevents_connections(self):
        self.page.open_peer(PEER["id"])
        self.page.set_discovery_state(False)
        self.assertFalse(self.page.enter_button.isEnabled())
        self.page._request_connection(True)
        self.assertFalse(self.requests)

    def test_search_matches_secondary_address_and_info_shows_network(self):
        self.page.set_peers([{**PEER, "addresses": [
            {"ip": PEER["ip"], "interface": "Ethernet"},
            {"ip": "100.64.0.3", "interface": "Virtual adapter"},
        ]}])
        self.page.search.setText("100.64.0.3")
        self.assertEqual(len(self.page.rows), 1)
        self.page.open_peer(PEER["id"])
        with patch("frontend.desktop.ui.devices_page.QMessageBox.exec", return_value=0), patch(
            "frontend.desktop.ui.devices_page.QMessageBox.setInformativeText"
        ) as informative_text:
            self.page.show_selected_info()
        self.assertIn("Virtual adapter：100.64.0.3", informative_text.call_args.args[0])

    def test_search_and_group_collapsing_do_not_start_sessions(self):
        self.page.search.setText("Study")
        self.assertEqual(len(self.page.rows), 1)
        self.page.search.setText("192.168.1.108")
        self.assertEqual(len(self.page.rows), 1)
        self.page.search.setText("no such computer")
        self.assertEqual(len(self.page.rows), 0)
        self.page.search.clear()
        self.assertEqual(len(self.page.rows), 2)
        self.page.group_button.click()
        self.assertTrue(self.page.scroll.isHidden())
        self.assertFalse(self.requests)

    def test_detail_layout_and_resources_at_supported_window_sizes(self):
        self.page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.page.open_peer(PEER["id"])
        self.page.show()
        for width in (810, 860, 1260):
            self.page.resize(width, 610)
            APP.processEvents()
            back = self.page.back_button.mapTo(self.page, self.page.back_button.rect().center())
            name = self.page.detail_name.mapTo(self.page, self.page.detail_name.rect().center())
            menu = self.page.menu_button.mapTo(self.page, self.page.menu_button.rect().center())
            self.assertLess(back.x(), name.x())
            self.assertLess(name.x(), menu.x())
            self.assertLessEqual(menu.x(), width)
            self.assertGreater(self.page.enter_button.width(), 700)
            self.assertEqual(self.page.detail_name.text(), PEER["name"])
        self.assertFalse(self.page.enter_button._wallpaper.isNull())
        for name in ("log-out", "ellipsis-vertical", "info", "search", "arrow-right", "layout-grid"):
            self.assertFalse(icons.icon(name).isNull(), name)

    def test_local_computer_name_has_visible_space(self):
        self.page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.page.resize(810, 610)
        self.page.show()
        APP.processEvents()
        name = self.page.rows[0].findChild(ComputerName)
        self.assertGreater(name.width(), 50)
        self.assertEqual(name.text(), name._full_text)

    def test_long_name_does_not_overlap_header_actions(self):
        self.page.set_peers([{**PEER, "name": "Long computer name " * 4}])
        self.page.open_peer(PEER["id"])
        self.page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.page.resize(810, 610)
        self.page.show()
        APP.processEvents()
        header = self.page.detail_name.parentWidget()
        name_right = self.page.detail_name.mapTo(header, self.page.detail_name.rect().topRight())
        menu_left = self.page.menu_button.mapTo(header, self.page.menu_button.rect().topLeft())
        self.assertLess(name_right.x(), menu_left.x())

    def test_offline_device_information_does_not_claim_connection_is_open(self):
        self.page.open_peer(PEER["id"])
        self.page.set_peers([])
        with patch("frontend.desktop.ui.devices_page.QMessageBox.exec", return_value=0), patch(
            "frontend.desktop.ui.devices_page.QMessageBox.setInformativeText"
        ) as informative_text:
            self.page.show_selected_info()
        self.assertIn("设备状态：离线", informative_text.call_args.args[0])
        self.assertIn("桌面连接：设备离线", informative_text.call_args.args[0])


class WindowConnectionTests(unittest.TestCase):
    def setUp(self):
        self.mocks = ExitStack()
        self.addCleanup(self.mocks.close)
        self.mocks.enter_context(patch("frontend.desktop.ui.window.user_settings.load",
                                      return_value=(HostSettings(), ViewerSettings())))
        self.mocks.enter_context(patch("frontend.desktop.ui.window.user_settings.load_ui",
                                      return_value=UiSettings()))
        self.mocks.enter_context(patch("frontend.desktop.ui.window.user_settings.save"))
        self.mocks.enter_context(patch.object(MainWindow, "_apply_ui_settings"))
        self.mocks.enter_context(patch.object(SessionService, "displays", return_value=[]))
        self.peers = self.mocks.enter_context(patch.object(DiscoveryService, "peers", return_value=[PEER]))
        self.start_viewer = self.mocks.enter_context(patch.object(SessionService, "start_viewer"))
        self.request = self.mocks.enter_context(patch.object(DesktopConnection, "connect_peer"))
        self.window = MainWindow(auto_refresh=False)
        self.window.devices_page.set_peers([PEER])
        self.window.devices_page.open_peer(PEER["id"])

    def tearDown(self):
        self.window._exit_requested = True
        self.window.close()
        self.window.deleteLater()
        APP.processEvents()

    def test_device_is_home_and_both_connection_modes_reach_service(self):
        self.assertEqual(self.window.stack.currentIndex(), PAGE_INDEX["devices"])
        self.assertEqual(set(self.window._nav_buttons), {"devices", "settings"})
        self.window.devices_page.enter_button.click()
        self.assertTrue(self.request.call_args.args[2])
        self.start_viewer.assert_not_called()
        self.window.devices_page.watch_button.click()
        self.assertFalse(self.request.call_args.args[2])
        self.window._session_connected(ViewerSettings(control_enabled=False))
        self.assertEqual(self.window.stack.currentIndex(), PAGE_INDEX["devices"])
        self.assertFalse(hasattr(self.window._stream_window, "watch_button"))
        self.assertFalse(self.window._stream_window._control_enabled)
        self.assertTrue(self.window._stream_window.isWindow())
        self.assertFalse(self.window._stream_window.isFullScreen())
        self.assertFalse(hasattr(self.window, "viewer_page"))
        self.window._show_device_list()
        self.assertEqual(self.window.devices_page.stack.currentIndex(), 0)

    def test_control_connection_preserves_disabled_optional_input_preferences(self):
        self.window._viewer_settings.clipboard_enabled = False
        self.window._viewer_settings.gamepad_enabled = False
        self.window.devices_page.watch_button.click()
        self.window.devices_page.enter_button.click()
        settings = self.request.call_args.args[1]
        self.assertFalse(settings.clipboard_enabled)
        self.assertFalse(settings.gamepad_enabled)

    def test_connect_opens_one_window_before_media_is_ready_and_close_disconnects(self):
        def connect(*_):
            self.window.connection.role = "viewer"
            self.window.connection.phase = "connecting"
        self.request.side_effect = connect
        self.window.devices_page.enter_button.click()
        stream = self.window._stream_window
        self.assertIsNotNone(stream)
        self.assertTrue(stream.isVisible())
        self.assertEqual(self.window.stack.currentIndex(), PAGE_INDEX["devices"])
        self.window._open_stream_window()
        self.assertIs(stream, self.window._stream_window)
        with patch.object(self.window.connection, "disconnect") as disconnect:
            stream.close()
            disconnect.assert_called_once()
        self.assertIsNone(self.window._stream_window)

    def test_picture_restart_preserves_window_and_clears_old_frame(self):
        from frontend.desktop.diagnostics import _synthetic_frame
        self.window._session_connected(ViewerSettings(control_enabled=True))
        stream = self.window._stream_window
        stream.set_frame(_synthetic_frame(0))
        self.window.connection.phase = "updating"
        self.window._connection_changed()
        self.assertFalse(stream.preview.has_frame)
        self.window._on_state("idle", "")
        self.assertIs(stream, self.window._stream_window)
        self.window._session_connected(ViewerSettings(control_enabled=True))
        self.assertIs(stream, self.window._stream_window)
        self.window.connection.phase = "active"
        self.window._connection_changed()
        self.assertTrue(stream.picture_button.isEnabled())

    def test_watch_ack_and_preferences_update_window_service_and_saved_settings(self):
        self.window._session_connected(ViewerSettings(control_enabled=True))
        stream = self.window._stream_window
        with patch.object(self.window.service, "set_control_enabled") as control, patch.object(
            self.window.service, "set_clipboard_enabled"
        ), patch.object(self.window.service, "set_gamepad_enabled"):
            self.window._mode_applied(False)
            control.assert_called_once()
        self.assertFalse(hasattr(stream, "watch_toggled"))
        self.assertFalse(stream._control_enabled)
        self.assertFalse(stream.preview.control_enabled)
        stream.mouse_mode.setCurrentIndex(1)
        self.assertEqual(self.window._viewer_settings.mouse_mode, "remote")
        self.assertEqual(self.window.connection._viewer.mouse_mode, "remote")
        self.assertEqual(stream.preview.mouse_mode, "remote")

    def test_busy_session_is_not_replaced(self):
        with patch.object(SessionService, "running", new_callable=lambda: property(lambda _: True)):
            self.window.devices_page.enter_button.click()
        self.start_viewer.assert_not_called()
        self.request.assert_not_called()

    def test_connection_rereads_current_address(self):
        self.peers.return_value = [{**PEER, "ip": "100.100.100.100", "srt_port": 9100}]
        self.window.devices_page.enter_button.click()
        peer = self.request.call_args.args[0]
        self.assertEqual(peer["ip"], "100.100.100.100")

    def test_expired_peer_is_not_connected(self):
        self.peers.return_value = []
        self.window.devices_page.enter_button.click()
        self.start_viewer.assert_not_called()
        self.request.assert_not_called()

    def test_session_state_preserves_offline_and_busy_device_buttons(self):
        self.peers.return_value = []
        self.window._connection_changed()
        self.assertFalse(self.window.devices_page.enter_button.isEnabled())
        self.assertFalse(self.window.devices_page.watch_button.isEnabled())
        self.window.connection.phase = "connecting"
        self.window._on_peers([PEER])
        self.assertFalse(self.window.devices_page.enter_button.isEnabled())
        self.window.connection.phase = "idle"
        self.peers.return_value = [PEER]
        self.window._connection_changed()
        self.assertTrue(self.window.devices_page.enter_button.isEnabled())

    def test_picture_is_saved_after_applied_signal(self):
        self.window._picture_applied({
            "width": 1280, "height": 720, "fps": 60, "bitrate_kbps": 6500,
        })
        from frontend.desktop import settings
        saved = settings.save.call_args.args[1]
        self.assertEqual(saved.stream_width, 1280)
        self.assertEqual(saved.stream_bitrate_kbps, 6500)

    def test_picture_controls_stage_values_until_apply(self):
        dialog = PictureDialog({"width": 1920, "height": 1080, "fps": 30, "bitrate_kbps": 4000})
        self.assertEqual([dialog.fps.itemData(i) for i in range(dialog.fps.count())],
                         [30, 60, 90, 120])
        self.assertEqual(dialog.fps.currentData(), 30)
        self.assertEqual((dialog.bitrate.minimum(), dialog.bitrate.maximum(),
                          dialog.bitrate.singleStep()), (1, 50, 1))
        dialog.fps.setCurrentIndex(dialog.fps.findData(120))
        dialog.bitrate.setValue(50)
        self.assertEqual(dialog.picture()["fps"], 120)
        self.assertEqual(dialog.picture()["bitrate_kbps"], 50000)
        self.assertEqual(dialog.bitrate_value.text(), "50")
        self.assertEqual(self.window._viewer_settings.stream_fps, 30)
        self.assertEqual(self.window._viewer_settings.stream_bitrate_kbps, 4000)
        self.assertEqual(dialog.result(), 0)
        dialog.apply_button.click()
        self.assertEqual(dialog.result(), dialog.DialogCode.Accepted)
        dialog.close()

    def test_picture_dialog_is_async_unique_and_closes_before_remote_window(self):
        self.window._session_connected(ViewerSettings())
        self.window.connection.role, self.window.connection.phase = "viewer", "active"
        with patch.object(PictureDialog, "exec", side_effect=AssertionError("nested event loop")):
            self.window._edit_picture()
            dialog = self.window._picture_dialog
            self.assertTrue(dialog.isVisible())
            self.window._edit_picture()
            self.assertIs(self.window._picture_dialog, dialog)
            with patch.object(self.window.connection, "disconnect") as disconnect:
                self.window._stream_window.close()
                disconnect.assert_called_once()
        self.assertIsNone(self.window._picture_dialog)
        APP.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_picture_accept_clears_owner_before_reentrant_disconnect(self):
        self.window._session_connected(ViewerSettings())
        self.window.connection.role, self.window.connection.phase = "viewer", "active"
        self.window._edit_picture()
        dialog = self.window._picture_dialog
        with patch.object(self.window.connection, "configure",
                          side_effect=lambda _: self.window._close_stream_window()) as configure:
            dialog.accept()
            configure.assert_called_once()
        self.assertIsNone(self.window._picture_dialog)
        self.assertIsNone(self.window._stream_window)
        APP.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_connection_loss_with_picture_dialog_never_applies_settings(self):
        self.window._session_connected(ViewerSettings())
        self.window.connection.role, self.window.connection.phase = "viewer", "active"
        self.window._edit_picture()
        with patch.object(self.window.connection, "configure") as configure:
            self.window.connection.phase = "stopping"
            self.window._connection_changed()
            self.assertIsNone(self.window._picture_dialog)
            self.assertIsNone(self.window._stream_window)
            configure.assert_not_called()
        APP.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_picture_controls_map_legacy_values(self):
        for fps, bitrate, expected_fps, expected_bitrate in (
            (5, 300, 30, 1000), (45, 6500, 30, 6000),
            (80, 20000, 90, 20000), (120, 51000, 120, 50000),
        ):
            with self.subTest(fps=fps, bitrate=bitrate):
                dialog = PictureDialog({"width": 1920, "height": 1080,
                                        "fps": fps, "bitrate_kbps": bitrate})
                self.assertEqual(dialog.picture()["fps"], expected_fps)
                self.assertEqual(dialog.picture()["bitrate_kbps"], expected_bitrate)
                dialog.close()

    def test_codec_choices_follow_format_and_apply_state(self):
        connection = self.window.connection
        connection._codec_enabled = True
        connection.phase = "active"
        connection._peer_caps = {"encoders": {
            "h264_nvenc": {"codec": "h264", "state": "available"},
            "libx265": {"codec": "hevc", "state": "available"},
        }, "decoders": {}}
        self.window.service.codec_capabilities = {"encoders": {}, "decoders": {
            "h264": {"codec": "h264", "state": "available"},
            "hevc": {"codec": "hevc", "state": "available"},
        }}
        dialog = PictureDialog({"width": 1920, "height": 1080, "fps": 30, "bitrate_kbps": 4000},
                               connection=connection)
        dialog.codec.setCurrentIndex(dialog.codec.findData("hevc"))
        self.assertEqual([dialog.encoder.itemData(i) for i in range(dialog.encoder.count())], ["auto", "libx265"])
        self.assertEqual([dialog.decoder.itemData(i) for i in range(dialog.decoder.count())], ["auto", "hevc"])
        dialog.encoder.setCurrentIndex(1)
        dialog.decoder.setCurrentIndex(1)
        self.assertEqual(dialog.picture()["codec"], "hevc")
        self.assertEqual(dialog.picture()["encoder"], "libx265")
        connection.phase = "validating"
        connection.changed.emit()
        self.assertFalse(dialog.apply_button.isEnabled())
        dialog.close()
        dialog.deleteLater()

    def test_upscale_controls_only_commit_slider_on_release_and_follow_enable(self):
        self.window._session_connected(ViewerSettings())
        stream = self.window._stream_window
        stream.set_session_active(True)
        events = []
        stream.preference_changed.connect(lambda *v: events.append(v))
        stream.apply_settings(ViewerSettings(sr_enabled=False))
        self.assertFalse(stream.sr_percent.isEnabled())
        self.assertFalse(stream.sr_sharpen.isEnabled())
        stream.apply_settings(ViewerSettings(sr_enabled=True, sr_percent=50, sr_sharpen=True))
        self.assertEqual((stream.sr_percent.minimum(), stream.sr_percent.maximum()), (33, 100))
        self.assertEqual(stream.sr_percent.value(), 50)
        self.assertTrue(stream.sr_sharpness.isEnabled())
        self.assertFalse(events)
        stream.preference_changed.disconnect(self.window._viewer_preference)
        stream.sr_percent.setSliderDown(True)
        stream.sr_percent.setValue(40)
        self.assertFalse(events)
        stream.sr_percent.setSliderDown(False)
        self.assertEqual(events, [("sr_percent", 40)])
        stream.set_upscale_capabilities({"fsr": {"state": "available", "message": "ok"},
                                        "rtx": {"state": "unobserved", "message": "not observed"}})
        self.assertTrue(stream.sr_mode.model().item(1).isEnabled())
        self.assertIn("not observed", stream.sr_status.text())

    def test_overlay_uses_measured_units_expiry_and_shared_persisted_toggle(self):
        from frontend.desktop.service import SessionStats
        self.window._session_connected(ViewerSettings(control_enabled=True))
        stream = self.window._stream_window
        stream.stats_button.setChecked(True)
        self.assertTrue(stream.preview.stats_visible)
        self.assertTrue(self.window._viewer_settings.stats_visible)
        stats = SessionStats(fps=119.5, bitrate_kbps=8000, extra={
            "sampled_at": time.monotonic(), "rtt_sampled_at": time.monotonic(),
            "host_sampled_at": time.monotonic(), "rtt_ms": 12, "decode_ms": 3,
            "encoder": "libx264", "decoder": "h264",
        })
        text = "\n".join(self.window._stats_lines(stats))
        for value in ("119.5", "8.00 Mbps", "12.0 ms", "3.0 ms", "libx264", "丢包 --"):
            self.assertIn(value, text)
        stats.extra["decode_label"] = "输出等待"
        self.assertIn("输出等待 3.0 ms", "\n".join(self.window._stats_lines(stats)))
        stats.extra = {}
        self.assertNotIn("119.5", "\n".join(self.window._stats_lines(stats)))
        self.window._toggle_stats_overlay(False)
        self.assertFalse(stream.stats_button.isChecked())
        self.assertFalse(stream.preview.stats_visible)

    def test_disconnect_replaces_connected_notice(self):
        self.window.notice_bar.show_message("桌面已连接", "info")
        self.window.connection.phase = "stopping"
        self.window._connection_changed()
        self.assertEqual(self.window.notice_bar._message, "正在断开连接…")
        self.window.connection.phase = "idle"
        self.window._connection_changed()
        self.assertEqual(self.window.notice_bar._message, "桌面已断开")

    def test_removed_switches_and_manual_host_entry_are_absent(self):
        self.assertFalse(hasattr(self.window.settings_page, "discovery_row"))
        self.assertFalse(hasattr(self.window.settings_page, "auto_host_row"))
        self.assertNotIn("allow_discovery", UiSettings.__dataclass_fields__)
        self.assertNotIn("auto_host", UiSettings.__dataclass_fields__)


class DiscoveryUpdateTests(unittest.TestCase):
    def test_metadata_and_port_changes_are_published_without_manual_refresh(self):
        service = DiscoveryService(instance_id="b" * 16)
        events = []
        service.peers_changed.connect(events.append)
        service._remember(PEER, PEER["ip"])
        service._publish()
        service._publish()
        self.assertEqual(len(events), 1)
        service._remember({**PEER, "srt_port": 9100, "name": "Renamed computer"}, PEER["ip"])
        service._publish()
        self.assertEqual(len(events), 2)
        self.assertEqual(events[-1][0]["srt_port"], 9100)
        self.assertEqual(events[-1][0]["name"], "Renamed computer")
        service.deleteLater()


if __name__ == "__main__":
    unittest.main()
