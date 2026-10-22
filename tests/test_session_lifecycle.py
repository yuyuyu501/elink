import asyncio
from concurrent.futures import Future
from types import SimpleNamespace

from elink.core import transport
from elink.core.input import RecordingInput
from elink.core.media import DesktopTrack
from elink.core.transport import Client, HostServer
from test_independent import until


def test_failed_capture_release_does_not_leave_host_busy(tmp_path):
    async def scenario():
        messages = []
        host = HostServer(tmp_path, synthetic=True, backend_factory=RecordingInput, notify=messages.append)
        host.port = 49200
        closed = []

        async def close():
            closed.append(True)

        host.pc = SimpleNamespace(close=close)
        track = DesktopTrack(640, 360, 30, True)
        track.stop()
        await asyncio.wrap_future(track.release_future)
        track.release_future = Future()
        track.release_future.set_exception(OSError('capture release failed'))
        host.tracks = [track]
        await host.end_session()
        assert closed and not host.host_info()['busy']
        assert messages and 'capture release failed' in ' '.join(messages)
        await host.end_session()  # The host's disconnect button remains usable.

    asyncio.run(scenario())


def test_host_cleanup_timeout_releases_slot_and_input(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setattr(transport, 'SESSION_CLOSE_TIMEOUT', .05, raising=False)
        host = HostServer(tmp_path, synthetic=True, backend_factory=RecordingInput)
        host.port = 49200
        released, stopped = [], []

        async def stuck():
            await asyncio.Event().wait()

        host.pc = SimpleNamespace(close=stuck, getTransceivers=lambda: [], sctp=None)
        host.input = SimpleNamespace(close=lambda: released.append(True))
        host.tracks = [SimpleNamespace(stop=lambda: stopped.append(True))]
        await asyncio.wait_for(host.end_session(), 1)
        assert released and stopped and not host.host_info()['busy']
        await host.end_session()

    asyncio.run(scenario())


def test_client_notifies_host_before_local_transport_finishes(tmp_path, monkeypatch):
    async def scenario():
        client = Client(tmp_path, play_audio=False)
        closing, release, notified = asyncio.Event(), asyncio.Event(), asyncio.Event()
        sent = []

        async def close():
            closing.set()
            await release.wait()

        async def request(*args, **kwargs):
            assert args[2:4] == ('DELETE', '/v1/session/session')
            notified.set()
            return {'ok': True}

        monkeypatch.setattr(transport, 'request', request)
        client.pc = SimpleNamespace(close=close)
        client.session_id, client.endpoint, client.fp, client.token = 'session', object(), 'fp', ''
        client.control = SimpleNamespace(readyState='open', bufferedAmount=0, send=sent.append)
        task = asyncio.create_task(client.disconnect())
        try:
            await asyncio.wait_for(closing.wait(), 1)
            await asyncio.wait_for(notified.wait(), .3)
            assert any('"type": "disconnect"' in raw for raw in sent)
        finally:
            release.set()
            await task
        assert not client.session_id and client.control is None

    asyncio.run(scenario())


def test_lost_controller_and_missing_heartbeat_release_host_for_reconnect(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setattr(transport, 'CONTROL_HEARTBEAT_TIMEOUT', 1.0)
        monkeypatch.setattr(transport, 'CONTROL_RECOVERY_TIMEOUT', 1.5)
        host = HostServer(tmp_path / 'host', synthetic=True, backend_factory=RecordingInput)
        client = Client(tmp_path / 'client', play_audio=False)
        try:
            port = await host.start('127.0.0.1', 0)
            address = f'127.0.0.1:{port}'
            await client.connect(address, dict(width=640, height=360, audio=False))
            await until(lambda: client.mailbox.received >= 3)
            # Simulate losing the controller transport, without its HTTP DELETE.
            await client.pc.close()
            await until(lambda: not host.host_info()['busy'], timeout=3)
            await client.disconnect()
            await client.connect(address, dict(width=640, height=360, audio=False))
            await until(lambda: client.mailbox.received >= 3)
            send = client.send
            # Pings/media can continue, but the host must still expire input liveness.
            client.send = lambda event: send(event) if event['type'] != 'heartbeat' else None
            await until(lambda: not host.host_info()['busy'], timeout=4)
            await client.disconnect()
            client.send = send
            await client.connect(address, dict(width=640, height=360, audio=False))
            await until(lambda: client.mailbox.received >= 3)
        finally:
            await client.disconnect()
            await host.stop()

    asyncio.run(scenario())


def test_cancelled_client_disconnect_still_closes_transport(tmp_path):
    async def scenario():
        client = Client(tmp_path, play_audio=False)
        closing, release = asyncio.Event(), asyncio.Event()
        closed = []

        async def close():
            closing.set()
            await release.wait()
            closed.append(True)

        client.pc = SimpleNamespace(close=close)
        caller = asyncio.create_task(client.disconnect())
        await closing.wait()
        caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)
        assert client.lock.locked(), 'reconnect must wait until old client cleanup finishes'
        release.set()
        await client.disconnect()
        assert closed and not client.lock.locked()

    asyncio.run(scenario())
