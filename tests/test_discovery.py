import asyncio
import json
import socket

from elink.discovery import (DiscoveredHost, NetworkScope, PROTOCOL, Responder,
                              add_host, discover, machine_id, presence_info, PRESENCE_TTL)
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


def test_controller_only_is_visible_without_a_host_port():
    network = scope()
    phone = dict(info(port=0), capabilities={'platform': 'android', 'roles': ['controller']})
    hosts = {}
    add_host(hosts, phone, '192.168.1.31', network)
    peer = hosts['a' * 64]
    assert peer.platform == 'android' and not peer.connectable
    assert peer.address == '192.168.1.31'
    for invalid in (dict(phone, port=49200), dict(info(), capabilities={'roles': []}),
                    dict(info(), capabilities={'roles': 'host'}), dict(info(), port=0)):
        rejected = {}
        add_host(rejected, invalid, '192.168.1.31', network)
        assert rejected == {}


def test_reciprocal_presence_recovers_one_way_broadcast_and_expires(monkeypatch):
    network = scope()
    clock = [100.0]
    monkeypatch.setattr('elink.discovery.time.monotonic', lambda: clock[0])
    class Transport:
        def sendto(self, *_): pass
    responder = Responder(network, lambda: info('b' * 64))
    responder.transport = Transport()
    phone = dict(info(port=0), capabilities={'platform': 'android', 'roles': ['controller']})
    query = json.dumps(dict(protocol=PROTOCOL, nonce='1' * 32, device=phone)).encode()
    responder.datagram_received(query, ('192.168.1.31', 12345))
    assert not network.online()['a' * 64].connectable
    assert network.online('a' * 64) == {}
    # Route-level expiry must not keep an obsolete interface alive indefinitely.
    clock[0] += PRESENCE_TTL - 1
    network.remember(phone, '192.168.1.32')
    clock[0] += 2
    assert network.online()['a' * 64].routes == {'192.168.1.32': '局域网'}
    clock[0] += PRESENCE_TTL
    assert network.online() == {}


def test_presence_payload_stays_small_and_omits_session_details():
    full = dict(info(), fingerprint='private', features=['x'] * 100,
                capabilities={'platform': 'windows', 'roles': ['controller', 'host'], 'extra': 'x' * 8000})
    compact = presence_info(full)
    assert len(json.dumps(compact).encode()) < 512
    assert 'fingerprint' not in compact and 'extra' not in compact['capabilities']


def test_discovery_collects_real_udp_reply_and_controller_announcement(monkeypatch):
    async def run():
        network = scope()
        network.interfaces = []
        loop = asyncio.get_running_loop()
        remote = dict(info(port=0), capabilities={'platform': 'android', 'roles': ['controller']})
        transport, _ = await loop.create_datagram_endpoint(lambda: Responder(network, lambda: remote),
                                                          local_addr=('127.0.0.1', 0))
        try:
            hosts = await discover(network, extra_targets=[transport.get_extra_info('sockname')], duration=.1)
            assert len(hosts) == 1 and not hosts[0].connectable
            assert hosts[0].address == '127.0.0.1'
        finally:
            transport.close()
    asyncio.run(run())


def test_learned_presence_is_probed_by_unicast_without_broadcast(monkeypatch):
    async def run():
        local = scope()
        local.interfaces = []
        local.local_info = lambda: info('b' * 64, 'Desktop')
        remote_scope = scope()
        remote_scope.interfaces = []
        phone = dict(info(port=0), capabilities={'platform': 'android', 'roles': ['controller']})
        local.remember(phone, '127.0.0.1')
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(lambda: Responder(remote_scope, lambda: phone),
                                                          local_addr=('127.0.0.1', 0))
        monkeypatch.setattr('elink.discovery.DISCOVERY_PORT', transport.get_extra_info('sockname')[1])
        try:
            # No explicit target or broadcast: a route learned from an incoming
            # announcement must be sufficient to refresh presence in both directions.
            found = await discover(local, 'b' * 64, duration=.1)
            assert len(found) == 1 and not found[0].connectable
            assert remote_scope.online()['b' * 64].name == 'Desktop'
            assert local.presence[('a' * 64, '127.0.0.1')][0] > 0
        finally:
            transport.close()
    asyncio.run(run())


def test_presence_deduplicates_routes_but_keeps_distinct_same_name_devices():
    network = scope()
    phone = dict(info(port=0, name='Android'), capabilities={'platform': 'android', 'roles': ['controller']})
    network.remember(phone, '192.168.1.31')
    network.remember(phone, '192.168.1.32')
    network.remember(dict(phone, host_id='b' * 64), '192.168.1.33')
    online = network.online()
    assert len(online) == 2
    assert len(online['a' * 64].routes) == 2
    assert not any(peer.connectable for peer in online.values())
