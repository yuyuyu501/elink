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

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from frontend.desktop import icons
from frontend.desktop.discovery import DiscoveryService
from frontend.desktop.service import HostSettings, SessionService, ViewerSettings
from frontend.desktop.settings import UiSettings
from frontend.desktop.theme import apply_theme
from frontend.desktop.ui.devices_page import ComputerName, DevicesPage
from frontend.desktop.ui.window import MainWindow, PAGE_INDEX

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

    def test_offline_device_cannot_connect_and_reappearance_recovers(self):
        self.page.open_peer(PEER["id"])
        self.page.set_peers([])
        self.assertFalse(self.page.enter_button.isEnabled())
        self.page._request_connection(True)
        self.assertFalse(self.requests)
        self.assertEqual(self.page.stack.currentIndex(), 1)
        self.page.set_peers([PEER])
        self.assertTrue(self.page.enter_button.isEnabled())

    def test_idle_peer_opens_detail_but_has_no_connection_action(self):
        self.page.set_peers([{**PEER, "hosting": False, "srt_port": 0}])
        self.page.rows[1].arrow_button.click()
        self.assertEqual(self.page.stack.currentIndex(), 1)
        self.assertFalse(self.page.enter_button.isEnabled())
        self.assertFalse(self.page.watch_button.isEnabled())

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
        settings = self.start_viewer.call_args.args[0]
        self.assertTrue(settings.control_enabled)
        self.assertEqual(settings.srt_url, "srt://192.168.1.108:9000")
        self.assertEqual(settings.control_host, PEER["ip"])
        self.assertEqual(self.window.stack.currentIndex(), PAGE_INDEX["viewer"])
        self.window._back_from_viewer()
        self.assertEqual(self.window.devices_page.stack.currentIndex(), 1)
        self.window.devices_page.watch_button.click()
        watch_settings = self.start_viewer.call_args.args[0]
        self.assertFalse(watch_settings.control_enabled)
        self.assertFalse(watch_settings.clipboard_enabled)
        self.assertFalse(watch_settings.gamepad_enabled)
        self.window.devices_page.enter_button.click()
        control_settings = self.start_viewer.call_args.args[0]
        self.assertTrue(control_settings.control_enabled)
        self.assertTrue(control_settings.clipboard_enabled)
        self.assertTrue(control_settings.gamepad_enabled)
        self.window._show_device_list()
        self.assertEqual(self.window.devices_page.stack.currentIndex(), 0)

    def test_control_connection_preserves_disabled_optional_input_preferences(self):
        self.window.viewer_page.clipboard_switch.setChecked(False)
        self.window.viewer_page.gamepad_switch.setChecked(False)
        self.window.devices_page.watch_button.click()
        self.window.devices_page.enter_button.click()
        settings = self.start_viewer.call_args.args[0]
        self.assertTrue(settings.control_enabled)
        self.assertFalse(settings.clipboard_enabled)
        self.assertFalse(settings.gamepad_enabled)

    def test_busy_session_is_not_replaced(self):
        with patch.object(SessionService, "running", new_callable=lambda: property(lambda _: True)):
            self.window.devices_page.enter_button.click()
        self.start_viewer.assert_not_called()

    def test_connection_rereads_current_address(self):
        self.peers.return_value = [{**PEER, "ip": "100.100.100.100", "srt_port": 9100}]
        self.window.devices_page.enter_button.click()
        settings = self.start_viewer.call_args.args[0]
        self.assertEqual(settings.srt_url, "srt://100.100.100.100:9100")

    def test_expired_peer_is_not_connected(self):
        self.peers.return_value = []
        self.window.devices_page.enter_button.click()
        self.start_viewer.assert_not_called()


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
