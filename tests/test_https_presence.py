"""Controller-only presence over the same HTTPS route used by Android."""
import asyncio
import base64
import json
import logging

import aiohttp
import pytest

from elink.core.input import RecordingInput
from elink.core.transport import HostServer
from elink.discovery import NetworkScope, PRESENCE_TTL, discover


def phone_header(**overrides):
    phone = dict(protocol='elink', version=2, host_id='c' * 64, name='Android',
                 port=0, capabilities=dict(platform='android', roles=['controller']))
    return base64.b64encode(json.dumps(dict(phone, **overrides)).encode()).decode()


@pytest.mark.parametrize('header', [
    'not base64', 'x' * 2733, base64.b64encode(b' ' * 2049).decode(),
    base64.b64encode(b'\xff').decode(), base64.b64encode(b'[]').decode(),
    base64.b64encode(b'null').decode(), phone_header(port=49200),
    phone_header(capabilities={'roles': ['host']}),
])
def test_invalid_https_presence_does_not_create_a_device(header):
    network = NetworkScope()
    assert network.introduce(header, '127.0.0.1') is False
    assert network.online() == {}


def test_https_presence_requires_local_source_and_renews_route_ttl(monkeypatch, caplog):
    network = NetworkScope()
    network.interfaces = []
    assert not network.introduce(phone_header(), '203.0.113.100')
    clock = [100.0]
    monkeypatch.setattr('elink.discovery.time.monotonic', lambda: clock[0])
    with caplog.at_level(logging.INFO, logger='elink.discovery'):
        assert network.introduce(phone_header(), '127.0.0.1')
    assert 'platform=android' in caplog.text and "('controller',)" in caplog.text
    clock[0] += PRESENCE_TTL - 1
    assert network.introduce(phone_header(), '127.0.0.1')
    clock[0] += 2
    assert len(network.online()) == 1
    clock[0] += PRESENCE_TTL
    assert network.online() == {}


def test_real_https_android_presence_is_returned_by_desktop_discovery(tmp_path, monkeypatch):
    async def run():
        network = NetworkScope()
        network.interfaces = []
        network.updated = 1e99
        host = HostServer(tmp_path, synthetic=True, backend_factory=RecordingInput, scope=network)
        port = await host.start('127.0.0.1', 0)
        try:
            async with aiohttp.ClientSession() as client:
                for _ in range(2):
                    async with client.get(f'https://127.0.0.1:{port}/v1/info', ssl=False,
                                          headers={'X-Elink-Presence': phone_header()}) as response:
                        assert response.status == 200
                        payload = await response.json()
                        assert payload['presence_received'] is True
                        assert 'presence-https-v1' in payload['features']
                # Older clients without a presence header remain compatible.
                async with client.get(f'https://127.0.0.1:{port}/v1/info', ssl=False) as response:
                    assert not (await response.json())['presence_received']
                async with client.get(f'https://127.0.0.1:{port}/v1/info', ssl=False,
                                      headers={'X-Elink-Presence': 'invalid'}) as response:
                    assert response.status == 200
                    assert not (await response.json())['presence_received']
            found = await discover(network, host.host_id, duration=.01)
            assert len(found) == 1
            assert found[0].id == 'c' * 64
            assert found[0].platform == 'android'
            assert found[0].address == '127.0.0.1'
            assert not found[0].connectable
            return found
        finally:
            await host.stop()
    asyncio.run(run())
