import asyncio
import json
import threading

from elink.core import transport
from elink.core.input_session import RecordingInput
from elink.core.transport import Client, HostServer
from elink.logging_setup import configure_logging
from test_independent import until


def test_input_exception_is_persisted_on_both_peers(tmp_path):
    class Denied(RecordingInput):
        def emit(self, event):
            raise RuntimeError('injection denied by test target')

    async def scenario():
        configure_logging(tmp_path)
        host = HostServer(tmp_path / 'host', synthetic=True, backend_factory=Denied)
        client = Client(tmp_path / 'controller', play_audio=False)
        try:
            port = await host.start('127.0.0.1', 0)
            await client.connect(f'127.0.0.1:{port}', dict(width=640, height=360, audio=False))
            await until(lambda: client.mailbox.received >= 3)
            client.focus(True)
            await asyncio.sleep(.1)
            client.send(dict(type='key', vk=65, scan=30, extended=False, down=True))
            await until(lambda: client.diagnostics.path.exists())
            h = json.loads(host.diagnostics.path.read_text(encoding='utf-8'))
            c = json.loads(client.diagnostics.path.read_text(encoding='utf-8'))
            assert h['session_tag'] == c['session_tag']
            failure = h['profile']['failures'][-1]
            assert failure['stage'] == 'input_receive_failed'
            assert 'test_session_diagnostics.py' in failure['traceback']
            assert 'injection denied' in c['profile']['failures'][-1]['message']
            assert host.session_id not in host.diagnostics.path.read_text(encoding='utf-8')
            assert 'injection denied' in (tmp_path / 'logs' / 'elink.log').read_text(encoding='utf-8')
            assert (await client.performance_report())['host']['failures']
        finally:
            await client.disconnect()
            await host.stop()

    asyncio.run(scenario())


def test_short_heartbeat_loss_recovers_but_held_input_is_released(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setattr(transport, 'CONTROL_HEARTBEAT_TIMEOUT', .4)
        monkeypatch.setattr(transport, 'CONTROL_RECOVERY_TIMEOUT', 4)
        sink = RecordingInput()
        host = HostServer(tmp_path / 'host', synthetic=True, backend_factory=lambda: sink)
        client = Client(tmp_path / 'controller', play_audio=False)
        try:
            port = await host.start('127.0.0.1', 0)
            await client.connect(f'127.0.0.1:{port}', dict(width=640, height=360, audio=False))
            await until(lambda: client.mailbox.received >= 3)
            client.focus(True)
            await asyncio.sleep(.1)
            client.send(dict(type='key', vk=65, scan=30, extended=False, down=True))
            await until(lambda: bool(sink.events))
            send = client.send
            client.send = lambda event: send(event) if event['type'] != 'heartbeat' else None
            await until(lambda: not host.input.active, timeout=3)
            assert host.pc is not None and not sink.events[-1]['down']
            client.send = send
            await until(lambda: host.input.active)
            assert any(e['event'] == 'control_heartbeat_recovered' for e in host.profile.snapshot()['events'])
            assert client.pc and host.pc
        finally:
            await client.disconnect()
            await host.stop()

    asyncio.run(scenario())


def test_clipboard_native_write_cannot_block_media_or_heartbeats_on_either_peer(tmp_path, monkeypatch):
    async def scenario():
        entered, release = threading.Event(), threading.Event()

        def slow_write(text):
            entered.set()
            release.wait(5)
            return True

        monkeypatch.setattr(transport, 'write_text', slow_write)
        monkeypatch.setattr(transport, 'read_text', lambda: None)
        host = HostServer(tmp_path / 'host', synthetic=True, backend_factory=RecordingInput)
        client = Client(tmp_path / 'controller', play_audio=False)
        try:
            port = await host.start('127.0.0.1', 0)
            await client.connect(f'127.0.0.1:{port}', dict(width=640, height=360, audio=False))
            await until(lambda: client.mailbox.received >= 3)
            client.send(dict(type='clipboard', text='test clipboard'))
            host.control_channel.send(json.dumps(dict(type='clipboard', text='test clipboard')))
            await until(entered.is_set)
            previous, pong = client.mailbox.received, client.last_pong
            await until(lambda: client.last_pong > pong and client.mailbox.received > previous + 10)
            assert host.pc and client.pc and not release.is_set()
        finally:
            release.set()
            await client.disconnect()
            await host.stop()

    asyncio.run(scenario())


def test_independent_https_probe_works_during_control_response_loss(tmp_path, monkeypatch):
    async def scenario():
        host = HostServer(tmp_path / 'host', synthetic=True, backend_factory=RecordingInput)
        client = Client(tmp_path / 'controller', play_audio=False)
        try:
            port = await host.start('127.0.0.1', 0)
            await client.connect(f'127.0.0.1:{port}', dict(width=640, height=360, audio=False))
            await until(lambda: client.mailbox.received >= 3)
            before = host.last_probe
            await client.probe_health(client.pc, client.session_id)
            assert host.last_probe > before
            events = client.profile.snapshot()['events']
            assert any(e['event'] == 'https_session_probe' and e['ok'] for e in events)
            # A different endpoint cannot probe another session.
            with_id = client.session_id
            await client.probe_health(client.pc, 'incorrect-session')
            report = json.loads(client.diagnostics.path.read_text(encoding='utf-8'))
            assert report['reason'] == 'control_probe_failed'
            assert with_id not in client.diagnostics.path.read_text(encoding='utf-8')
        finally:
            await client.disconnect()
            await host.stop()

    asyncio.run(scenario())


def test_six_second_pong_pause_recovers_without_reconnecting(tmp_path, monkeypatch):
    from aiortc import RTCDataChannel

    async def scenario():
        pause = [False]
        original = RTCDataChannel.send

        def send(channel, raw):
            if pause[0] and json.loads(raw).get('type') == 'pong':
                return
            return original(channel, raw)

        monkeypatch.setattr(RTCDataChannel, 'send', send)
        host = HostServer(tmp_path / 'host', synthetic=True, backend_factory=RecordingInput)
        client = Client(tmp_path / 'controller', play_audio=False)
        try:
            port = await host.start('127.0.0.1', 0)
            await client.connect(f'127.0.0.1:{port}', dict(width=640, height=360, audio=False))
            await until(lambda: client.rtt_ms > 0)
            pc = client.pc
            pause[0] = True
            await asyncio.sleep(6.2)
            assert client.pc is pc and client.recovering
            assert host.last_probe > 0
            pause[0] = False
            await until(lambda: not client.recovering)
            assert client.pc is pc and host.pc
            assert any(e['event'] == 'control_pong_recovered' for e in client.profile.snapshot()['events'])
        finally:
            await client.disconnect()
            await host.stop()

    asyncio.run(scenario())
