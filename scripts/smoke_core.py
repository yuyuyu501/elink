"""Real desktop loopback; never inject input into the user's desktop."""
import asyncio
import json
import logging
import tempfile
import time
from pathlib import Path

from elink.core.transport import HostServer, Client
from elink.core.input import RecordingInput
from elink.core.codecs import metrics


async def main():
    root = Path(tempfile.mkdtemp(prefix="elink-native-"))
    host = HostServer(root / "host", backend_factory=RecordingInput, notify=print)
    client = Client(root / "client", notify=print, play_audio=False)
    await host.start("127.0.0.1", 0)
    code = host.authority.invite()
    address = f"127.0.0.1:{host.port}"
    pair = asyncio.create_task(client.pair(address, code))
    while not host.authority.pending:
        await asyncio.sleep(0.05)
    host.authority.approve(next(iter(host.authority.pending)))
    await pair
    try:
        await client.connect(address, {"width": 1920, "height": 1080, "fps": 60, "audio": True})
        for i in range(8):
            await asyncio.sleep(1)
            print("frames", client.mailbox.received, "audio", client.audio_frames, "host", host.pc.connectionState if host.pc else None, flush=True)
        assert client.mailbox.received > 30 and client.audio_frames > 10
        report = {"video_frames": client.mailbox.received, "audio_frames": client.audio_frames, "metrics": dict(metrics), "rtt_ms": client.rtt_ms}
        Path(".artifacts/independent-desktop.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("revoke", flush=True)
        await host.revoke(host.device_id)
        print("revoked", flush=True)
        await asyncio.sleep(1)
    finally:
        await client.disconnect()
        print("client closed", flush=True)
        await host.stop()
        print("host closed", flush=True)


async def monitored():
    task = asyncio.create_task(main())
    try:
        await asyncio.wait_for(asyncio.shield(task), 25)
    except TimeoutError:
        for pending in asyncio.all_tasks():
            pending.print_stack()
            coro = pending.get_coro()
            while coro is not None:
                print("awaiting", repr(coro), flush=True)
                coro = getattr(coro, "cr_await", getattr(coro, "gi_yieldfrom", None))
        task.cancel()
        raise


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(monitored())
