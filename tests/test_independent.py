import asyncio
import json
import time

import aiohttp
import pytest

from elink.core.input import InputSession, RecordingInput
from elink.core.security import Identity
from elink.core.transport import Client, HostServer, request, settings
from elink.models import Endpoint, ValidationError


def test_input_replay_focus_and_watchdog():
    backend = RecordingInput()
    session = InputSession(backend)
    send = lambda e, reliable=True: session.receive(json.dumps(e), reliable)
    key = {"type": "key", "vk": 65, "scan": 30, "extended": False, "down": True, "epoch": 1}
    assert not send(key)
    assert send({"type": "heartbeat", "active": True, "epoch": 1})
    assert not send(dict(key, vk=True))
    assert not send(key, False)
    assert send(key)
    move = {"type": "move", "x": 5, "y": 2, "absolute": False, "seq": 1, "epoch": 1}
    assert send(move, False)
    assert not send(move, False)
    session.heartbeat = time.monotonic() - 3
    session.watchdog()
    assert backend.events[-1]["down"] is False
    assert not send(key)
    assert send({"type": "heartbeat", "active": False, "epoch": 2})
    assert not send(dict(move, seq=3), False)
    assert not session.receive("[]")
    assert not session.receive("x" * 3000)


async def until(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition timed out")
        await asyncio.sleep(0.05)


async def roundtrip(root, synthetic=True, width=640, height=360, fps=30):
    backend = RecordingInput()
    messages = []
    host = HostServer(root / "host", synthetic=synthetic, backend_factory=lambda: backend, notify=messages.append)
    client = Client(root / "client", messages.append, play_audio=False)
    try:
        port = await host.start("127.0.0.1", 0)
        address = f"127.0.0.1:{port}"
        fp = host.identity.fingerprint
        assert (await request(Endpoint.parse(address), fp, "GET", "/v1/apps"))["apps"][0]["id"] == "desktop"
        with pytest.raises(aiohttp.ServerFingerprintMismatch):
            await request(Endpoint.parse(address), "0" * 64, "GET", "/v1/info")
        await client.connect(address, {"width": width, "height": height, "fps": fps, "audio": True})
        await until(lambda: client.mailbox.received >= 10 and client.audio_frames >= 5)
        first = client.mailbox.take()
        await until(lambda: client.mailbox.received >= 20)
        second = client.mailbox.take()
        assert first.shape == (height, width, 3)
        if synthetic:
            assert (first != second).any()
        client.focus(True)
        client.send({"type": "key", "vk": 65, "scan": 30, "extended": False, "down": True})
        await until(lambda: bool(backend.events))
        assert backend.events[-1]["down"] is True
        client.focus(False)
        await until(lambda: backend.events[-1].get("down") is False)
        client.focus(True)
        await until(lambda: host.input.active)
        client.send({"type": "pad", "index": 0, "values": [4096, 0, 0, 100, 0, 0, 0]})
        await until(lambda: any(e["type"] == "pad" for e in backend.events))
        await until(lambda: client.rtt_ms > 0)
        assert client.cursor_supported
        from elink.core.media import DesktopTrack
        video = next(t for t in host.tracks if isinstance(t, DesktopTrack))
        for mode in ('local', 'remote', 'smart'):
            client.set_cursor_mode(mode)
            await until(lambda: client.cursor_applied == mode)
            assert video.cursor_mode == mode
        if synthetic:
            video.cursor_visible = False
            await until(lambda: client.cursor_visible is False)
            video.cursor_visible = True
            await until(lambda: client.cursor_visible is True)
        await until(lambda: client.stats.rx_kbps is not None and client.stats.loss_percent is not None)
        assert client.stats.rx_kbps > 0
        assert client.stats.decode_ms is not None
        from elink.core.codecs import metrics
        report = {"video_frames": client.mailbox.received, "audio_frames": client.audio_frames,
                  "rtt_ms": round(client.rtt_ms, 2), "metrics": dict(metrics), "messages": messages}
        await host.end_session()
        await until(lambda: client.pc is None)
        assert backend.events[-1]["values"] == [0] * 7
        await client.connect(address, {"width": width, "height": height, "fps": fps, "audio": False})
        await until(lambda: client.mailbox.received >= 3)
        await until(lambda: client.cursor_applied == 'smart')
        assert not (root / "host/authorized-devices.json").exists()
        assert not (root / "client/trusted-hosts.json").exists()
        return report
    finally:
        await client.disconnect()
        await host.stop()


def test_automatic_tls_webrtc_input_host_disconnect_and_reconnect(tmp_path):
    print(asyncio.run(roundtrip(tmp_path)))


def test_simultaneous_clients_only_one_enters_negotiation(tmp_path, monkeypatch):
    from elink.core import transport
    async def scenario():
        host = HostServer(tmp_path / 'host', synthetic=True, backend_factory=RecordingInput)
        clients = [Client(tmp_path / str(i), play_audio=False) for i in range(2)]
        original_request = transport.request
        barrier = asyncio.Event()
        infos = []
        async def request_together(endpoint, fp, method, path, **kwargs):
            result = await original_request(endpoint, fp, method, path, **kwargs)
            if path == '/v1/info':
                infos.append(result)
                if len(infos) == 2:
                    barrier.set()
                await barrier.wait()
            return result
        monkeypatch.setattr(transport, 'request', request_together)
        try:
            port = await host.start('127.0.0.1', 0)
            results = await asyncio.wait_for(asyncio.gather(*(
                c.connect(f'127.0.0.1:{port}', dict(width=640, height=360, audio=False)) for c in clients
            ), return_exceptions=True), 20)
            assert sum(r is None for r in results) == 1
            assert sum(isinstance(r, ValidationError) for r in results) == 1
            assert all(info['busy'] is False for info in infos)
            winner = clients[next(i for i, r in enumerate(results) if r is None)]
            await until(lambda: winner.mailbox.received > 5)
            assert host.host_info()['busy']
            await winner.disconnect()
            await until(lambda: not host.host_info()['busy'])
        finally:
            for client in clients:
                await client.disconnect()
            await host.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize("value", [{"fps": True}, {"fps": 121}, {"width": 1919}, {"audio": 1}, {"bitrate": 81}])
def test_invalid_settings(value):
    with pytest.raises(ValidationError):
        settings(value)


def test_receive_queues_drop_stale_frames_and_recover_idr():
    from elink.core.buffers import DecodeQueue, LatestQueue
    from aiortc.jitterbuffer import JitterFrame
    requested = []
    queue = DecodeQueue(True, lambda: requested.append(True))
    for stamp in range(8):
        queue.put((None, JitterFrame(b"\x00\x00\x01\x41\x01", stamp)))
    assert requested == [True] and queue.empty()
    keyframe = (None, JitterFrame(b"\x00\x00\x01\x65\x01", 10))
    queue.put(keyframe)
    assert queue.get() == keyframe
    queue.put(None)
    assert queue.get() is None
    async def decoded():
        values = LatestQueue(maxsize=1)
        for i in range(100):
            await values.put(i)
        assert values.qsize() == 1 and await values.get() == 99
    asyncio.run(decoded())


@pytest.mark.parametrize("address", ["https://pc", "--help", "pc:0", "pc:65536", "[::1", "pc/path"])
def test_address_rejects_non_endpoints(address):
    with pytest.raises(ValidationError):
        Endpoint.parse(address)


def test_https_fragmented_response(tmp_path):
    from aiohttp import web
    from elink.core.security import Identity
    async def fragmented():
        identity = Identity(tmp_path)
        async def stream(request):
            response = web.StreamResponse(headers={"Content-Type": "application/json"})
            await response.prepare(request)
            await response.write(b'{"value":')
            await asyncio.sleep(0.1)
            await response.write(b'123}')
            await response.write_eof()
            return response
        app = web.Application()
        app.router.add_get("/fragmented", stream)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=identity.context)
        await site.start()
        endpoint = Endpoint("127.0.0.1", site._server.sockets[0].getsockname()[1])
        try:
            assert await request(endpoint, identity.fingerprint, "GET", "/fragmented") == {"value": 123}
        finally:
            await runner.cleanup()
    asyncio.run(fragmented())


def test_cancelled_shutdown_caller_does_not_abandon_transport(tmp_path):
    async def exercise():
        server = HostServer(tmp_path, synthetic=True, backend_factory=RecordingInput)
        entered, release = asyncio.Event(), asyncio.Event()
        class Peer:
            closed = False
            async def close(self):
                entered.set()
                await release.wait()
                self.closed = True
        peer = server.pc = Peer()
        caller = asyncio.create_task(server.end_session())
        await entered.wait()
        caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)
        assert server.ending and not peer.closed
        release.set()
        await server.stop()
        assert peer.closed and not server.ending
    asyncio.run(exercise())
