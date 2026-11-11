"""Network discovery tests without requiring remote computers."""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication
from PySide6.QtNetwork import QHostAddress, QNetworkInterface

from frontend.desktop.discovery import DiscoveryService, PEER_TTL_S, PROTOCOL
from frontend.desktop.network_candidates import candidate_addresses, network_candidates
from frontend.desktop.network_interfaces import network_interfaces

APP = QCoreApplication.instance() or QCoreApplication([])
PEER = {
    "proto": PROTOCOL, "kind": "announce", "id": "a" * 16, "name": "Remote",
    "platform": "Windows", "version": "0.6.0", "hosting": True,
    "srt_port": 9000, "control_port": 7000,
}


def interface(ip="192.168.1.2", prefix=24, broadcast="192.168.1.255", **extra):
    return {
        "index": 2, "name": "Ethernet", "up": True, "loopback": False,
        "can_broadcast": True,
        "addresses": [{"ip": ip, "prefix": prefix, "broadcast": broadcast}],
        **extra,
    }


class InterfaceTests(unittest.TestCase):
    def test_inventory_keeps_physical_virtual_and_inactive_interfaces(self):
        flags = QNetworkInterface.InterfaceFlag
        entries = []
        for index, (name, state) in enumerate((
            ("Ethernet", flags.IsUp | flags.IsRunning | flags.CanBroadcast),
            ("Virtual adapter", flags.IsUp | flags.IsRunning),
            ("Disconnected", flags.CanBroadcast),
        )):
            adapter = MagicMock()
            adapter.index.return_value = index
            adapter.humanReadableName.return_value = name
            adapter.flags.return_value = state
            entry = MagicMock()
            entry.ip.return_value.toString.return_value = "192.168.1.2"
            entry.prefixLength.return_value = 24
            entry.broadcast.return_value.toString.return_value = "192.168.1.255"
            adapter.addressEntries.return_value = [entry]
            entries.append(adapter)
        with patch.object(QNetworkInterface, "allInterfaces", return_value=entries):
            inventory = network_interfaces()
        self.assertEqual([item["name"] for item in inventory], ["Ethernet", "Virtual adapter", "Disconnected"])
        self.assertFalse(inventory[-1]["up"])
        self.assertEqual(inventory[0]["addresses"][0]["prefix"], 24)


class NetworkCandidateTests(unittest.TestCase):
    def test_neighbors_and_host_routes_filter_and_deduplicate_by_interface(self):
        interfaces = [
            interface(),
            interface(ip="100.98.115.92", prefix=32, broadcast="100.98.115.92", index=26, name="VPN"),
            interface(ip="127.0.0.1", loopback=True, index=1, name="Loopback"),
        ]
        neighbors = [
            {"ip": "192.168.1.10", "interface_index": 2, "state": 5},
            {"ip": "192.168.1.10", "interface_index": 2, "state": 4},
            {"ip": "100.122.191.87", "interface_index": 26, "state": 5},
            {"ip": "100.122.191.88", "interface_index": 26, "state": 0},
            {"ip": "10.0.0.2", "interface_index": 9, "state": 5},
        ]
        routes = [
            {"ip": "100.122.191.87", "interface_index": 26, "prefix_length": 32},
            {"ip": "100.122.191.89", "interface_index": 26, "prefix_length": 32},
            {"ip": "192.168.2.0", "interface_index": 2, "prefix_length": 24},
            {"ip": "192.168.1.1", "interface_index": 2, "prefix_length": 32},
            {"ip": "192.168.1.10", "interface_index": 2, "prefix_length": 32},
        ]
        self.assertEqual(candidate_addresses(interfaces, neighbors, routes), [
            {"ip": "192.168.1.1", "interface_index": 2},
            {"ip": "192.168.1.10", "interface_index": 2},
            {"ip": "100.122.191.87", "interface_index": 26},
            {"ip": "100.122.191.89", "interface_index": 26},
        ])

    def test_non_windows_keeps_broadcast_discovery_available(self):
        with patch("frontend.desktop.network_candidates.os.name", "posix"):
            self.assertEqual(network_candidates([interface()]), [])


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.service = DiscoveryService(port=0, instance_id="b" * 16)

    def tearDown(self):
        self.service.stop()
        self.service.deleteLater()
        APP.processEvents()

    def test_interface_changes_replace_bound_sockets(self):
        interfaces = [
            interface(ip="127.0.0.1", loopback=True),
            interface(ip="169.254.1.2", up=False),
            interface(ip="127.0.0.2", name="Test adapter"),
        ]
        with patch("frontend.desktop.discovery.network_interfaces", return_value=interfaces):
            self.service._update_interfaces()
        self.assertEqual(set(self.service._bindings), {"127.0.0.2"})
        old_socket = self.service._bindings["127.0.0.2"][0]
        with patch("frontend.desktop.discovery.network_interfaces", return_value=[
            interface(ip="127.0.0.3", name="Changed adapter"),
        ]):
            self.service._update_interfaces()
        self.assertEqual(set(self.service._bindings), {"127.0.0.3"})
        self.assertFalse(old_socket.isOpen())
        self.service._running = True
        self.service.stop()
        self.assertFalse(self.service._bindings)

    def test_broadcast_uses_each_eligible_subnet_but_not_host_tunnels(self):
        ethernet, wifi, tunnel = MagicMock(), MagicMock(), MagicMock()
        self.service._bindings = {
            "192.168.1.2": (ethernet, {**interface(), **interface()["addresses"][0]}),
            "10.0.0.2": (wifi, {"can_broadcast": True, "prefix": 24, "broadcast": "10.0.0.255"}),
            "100.64.0.2": (tunnel, {"can_broadcast": True, "prefix": 32, "broadcast": "100.64.0.2"}),
        }
        self.service._broadcast(PEER)
        self.assertEqual(ethernet.writeDatagram.call_args.args[1].toString(), "192.168.1.255")
        self.assertEqual(wifi.writeDatagram.call_args.args[1].toString(), "10.0.0.255")
        tunnel.writeDatagram.assert_not_called()
        self.service._bindings.clear()

    def test_query_broadcast_does_not_list_peers_without_elink_messages(self):
        with patch.object(self.service, "_broadcast") as send:
            self.service._send_query()
        message = send.call_args.args[0]
        self.assertEqual(message["kind"], "query")
        self.assertEqual(message["proto"], PROTOCOL)
        self.assertEqual(self.service.peers(), [])

    def test_network_paths_merge_and_expire_independently(self):
        with patch("frontend.desktop.discovery.time.monotonic", return_value=100):
            self.service._remember(PEER, "192.168.1.10", "Ethernet")
        with patch("frontend.desktop.discovery.time.monotonic", return_value=110):
            self.service._remember(PEER, "192.168.2.10", "Virtual adapter")
            peers = self.service.peers()
        self.assertEqual(len(peers), 1)
        self.assertEqual(peers[0]["ip"], "192.168.1.10")
        self.assertEqual(len(peers[0]["addresses"]), 2)
        with patch("frontend.desktop.discovery.time.monotonic", return_value=100 + PEER_TTL_S + 1):
            self.service._prune(100 + PEER_TTL_S + 1)
            self.assertEqual(self.service.peers()[0]["ip"], "192.168.2.10")
        self.service._prune(110 + PEER_TTL_S + 1)
        self.assertEqual(self.service.peers(), [])

    def test_query_is_answered_but_only_announce_registers_peer(self):
        receiver = MagicMock()
        receiver.hasPendingDatagrams.side_effect = [True, True, False]
        receiver.pendingDatagramSize.return_value = 200
        receiver.readDatagram.side_effect = [
            (b'{"proto":"other"}', QHostAddress("192.168.1.10"), 49320),
            (json.dumps({**PEER, "kind": "query"}).encode(), QHostAddress("192.168.1.10"), 49320),
        ]
        receiver.localAddress.return_value = QHostAddress("192.168.1.2")
        self.service._bindings = {"192.168.1.2": (receiver, {"name": "Ethernet"})}
        self.service._read_datagrams(receiver)
        self.assertEqual(self.service.peers(), [])
        reply = json.loads(bytes(receiver.writeDatagram.call_args.args[0]))
        self.assertEqual(reply["kind"], "announce")

        receiver.hasPendingDatagrams.side_effect = [True, False]
        receiver.readDatagram.side_effect = [
            (json.dumps(PEER).encode(), QHostAddress("192.168.1.10"), 49320),
        ]
        self.service._read_datagrams(receiver)
        self.assertEqual(len(self.service.peers()), 1)
        self.assertEqual(self.service.peers()[0]["interface"], "Ethernet")
        self.service._bindings.clear()

    def test_candidates_are_queried_once_per_interval_through_matching_interface(self):
        first, second = MagicMock(), MagicMock()
        first_interface = {**interface(), **interface()["addresses"][0]}
        second_data = interface(ip="100.98.115.92", prefix=32, index=26, name="VPN")
        second_interface = {**second_data, **second_data["addresses"][0]}
        self.service._bindings = {
            "192.168.1.2": (first, first_interface),
            "100.98.115.92": (second, second_interface),
        }
        self.service._candidates = [
            {"ip": "100.122.191.87", "interface_index": 26},
            {"ip": "192.168.1.10", "interface_index": 2},
        ]
        with patch("frontend.desktop.discovery.time.monotonic", return_value=100):
            self.service._send_query()
        self.assertEqual(
            [call.args[1].toString() for call in first.writeDatagram.call_args_list].count("192.168.1.10"),
            1,
        )
        self.assertEqual(
            [call.args[1].toString() for call in second.writeDatagram.call_args_list],
            ["100.122.191.87"],
        )
        with patch("frontend.desktop.discovery.time.monotonic", return_value=102):
            self.service._send_query()
        self.assertEqual(
            [call.args[1].toString() for call in first.writeDatagram.call_args_list].count("192.168.1.10"),
            1,
        )
        self.assertEqual(
            [call.args[1].toString() for call in second.writeDatagram.call_args_list].count("100.122.191.87"),
            1,
        )
        self.service._bindings.clear()


if __name__ == "__main__":
    unittest.main()
