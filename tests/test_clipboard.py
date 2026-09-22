import asyncio


def test_client_clipboard_watcher_sends_only_changes(tmp_path, monkeypatch):
    from elink.core import transport
    from elink.core.transport import Client

    async def run():
        client = Client(tmp_path / 'client', play_audio=False)
        peer = object()
        client.pc = peer
        client.clipboard_seen = 'before'
        sent = []
        client.send = sent.append
        monkeypatch.setattr(transport, 'read_text', lambda: 'after')
        task = asyncio.create_task(client.watch_clipboard(peer))
        await asyncio.sleep(0.8)
        client.pc = None
        await asyncio.wait_for(task, 1)
        assert sent == [{'type': 'clipboard', 'text': 'after'}]

    asyncio.run(run())
