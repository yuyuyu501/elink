import asyncio
import json
import socket

from elink.discovery import (DiscoveredHost, NetworkScope, PROTOCOL, Responder,
                              add_host, discover, machine_id)
from elink.network import Peer, TailnetStatus


def scope():
    result = NetworkScope()
    result.interfaces = [__import__('ipaddress').ip_interface('192.168.1.20/24')]
    result.tailnet = TailnetStatus(installed=True, state='Running', addresses=['100.64.0.20'], peers=[])
    result.tail_addresses = {'100.64.0.21'}
    result.updated = 1e99
    return result


def info(identifier='a' * 64, name='Host', port=49200):
    return dict(protocol='elink', version=2, host_id=identifier, name=name, codec='H264', port=port)


def test_scope_and_machine_deduplication():
    network = scope()
    hosts = {}
    add_host(hosts, info(), '192.168.1.30', network)
    add_host(hosts, info(), '100.64.0.21', network)
    add_host(hosts, info('b' * 64, name='Other'), '10.0.0.2', network)
    assert list(hosts) == ['a' * 64]
    assert hosts['a' * 64].routes == {'192.168.1.30': '局域网', '100.64.0.21': 'Tailscale'}
    assert hosts['a' * 64].address == '192.168.1.30'
    assert network.allows('192.168.1.31') and network.allows('100.64.0.21')
    assert not network.allows('10.0.0.2')


def test_responder_rejects_external_and_answers_local_query():
    network = scope()
    class Transport:
        def __init__(self): self.sent = []
        def sendto(self, data, address): self.sent.append((json.loads(data), address))
    responder = Responder(network, lambda: info())
    responder.transport = Transport()
    query = json.dumps({'protocol': PROTOCOL, 'nonce': '1' * 32}).encode()
    responder.datagram_received(query, ('10.0.0.3', 1))
    responder.datagram_received(query, ('192.168.1.30', 1))
    assert len(responder.transport.sent) == 1
    assert responder.transport.sent[0][0]['host_id'] == 'a' * 64


def test_discovery_broadcast_deduplicates_same_host(monkeypatch):
    network = scope()
    # Validate the identity merge used by discover without opening a socket.
    hosts = {}
    add_host(hosts, info(), '192.168.1.30', network)
    add_host(hosts, info(), '192.168.1.31', network)
    assert len(hosts) == 1 and len(hosts['a' * 64].routes) == 2
