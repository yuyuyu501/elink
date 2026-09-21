import asyncio
import json

from elink.peer_diagnostics import host, client, until


def test_two_peer_runner_automatic_media_recording_input_and_host_stop(tmp_path):
    async def scenario():
        host_root, client_root = tmp_path / 'host', tmp_path / 'client'
        host_root.mkdir()
        client_root.mkdir()
        task = asyncio.create_task(host({'bind': '127.0.0.1', 'port': 0, 'peer': '127.0.0.1',
                                         'name': 'peer-test', 'timeout': 30}, host_root, lambda _: None))
        receiver = None
        try:
            await until(lambda: (host_root / 'ready.json').exists(), 5)
            ready = json.loads((host_root / 'ready.json').read_text())
            receiver = asyncio.create_task(client({'address': f"127.0.0.1:{ready['port']}",
                                                   'name': 'peer-test',
                                                   'width': 640, 'height': 360, 'fps': 30, 'seconds': 2,
                                                   'wait_stop': True}, client_root, lambda _: None))
            await until(lambda: (client_root / 'awaiting-stop.json').exists(), 20)
            (host_root / 'stop').touch()
            await asyncio.wait_for(asyncio.gather(task, receiver), 10)
            host_report = json.loads((host_root / 'host-result.json').read_text())
            client_report = json.loads((client_root / 'client-result.json').read_text())
            assert host_report['ok'] and host_report['cleanup'] and host_report['host_disconnected']
            assert client_report['ok'] and client_report['cleanup'] and client_report['host_disconnected']
            assert client_report['paths'] and client_report['stats']
        finally:
            for pending in (task, receiver):
                if pending and not pending.done():
                    pending.cancel()
            await asyncio.gather(*(pending for pending in (task, receiver) if pending), return_exceptions=True)
    asyncio.run(scenario())
