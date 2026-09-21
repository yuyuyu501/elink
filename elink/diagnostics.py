"""Explicit local self-test for the source and frozen app. No system input injection."""
import asyncio
import json
import sys
import time
import traceback
from dataclasses import asdict
from pathlib import Path

from PySide6.QtWidgets import QApplication

from .core.codecs import metrics
from .core.input import RecordingInput
from .core.transport import Client, HostServer
from .storage import atomic_json
from .ui.player import Player
from .ui.theme import apply_theme
from .ui.window import MainWindow


def run(root: Path, desktop=False):
    root.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    apply_theme(app)
    window = MainWindow(root / "ui", auto_refresh=False, auto_host=False)
    window.show()
    result = {}
    server = None
    client = None
    player = None
    runtime = window.runtime

    def pump(predicate, timeout=25):
        deadline = time.monotonic() + timeout
        while not predicate():
            app.processEvents()
            if time.monotonic() > deadline:
                raise TimeoutError("self-test timed out")
            time.sleep(0.005)

    async def connect():
        nonlocal server, client
        server = HostServer(root / "host", synthetic=not desktop, backend_factory=RecordingInput)
        client = Client(root / "client", play_audio=not desktop)
        await server.start("127.0.0.1", 0)
        address = f"127.0.0.1:{server.port}"
        await client.connect(address, dict(width=1280, height=720, fps=60, audio=True))

    async def stop():
        if server and server.device_id:
            await server.end_session()
        if client:
            await client.disconnect()
        if server:
            await server.stop()

    try:
        future = runtime.submit(connect())
        pump(future.done)
        future.result()
        player = Player(runtime, client, controllers=False)
        player.show()
        pump(lambda: client.mailbox.received > 90 and client.audio_frames > 10 and player.image is not None
             and client.stats.rx_kbps is not None and client.stats.loss_percent is not None)
        if not desktop:
            assert client.output is not None and client.output.stream.active
        result.update(ok=True, desktop=desktop, video_frames=client.mailbox.received,
                      audio_frames=client.audio_frames, metrics=dict(metrics), rtt_ms=client.rtt_ms,
                      image_width=player.image.width(), image_height=player.image.height(),
                      audio_output_active=bool(client.output and client.output.stream.active),
                      stream_statistics=asdict(client.stats))
        player.grab().save(str(root / "player.png"))
        from .ui.chrome import TITLE_HEIGHT
        assert player.video_viewport().top() == TITLE_HEIGHT
        if app.platformName() == 'windows':
            assert player.frameGeometry() == player.geometry()
        result['single_titlebar'] = True
        player.open_controls()
        app.processEvents()
        player.control_menu.grab().save(str(root / "control-menu.png"))
        player.control_menu.close()
        player.open_quality()
        from .ui.controls import StreamOptions
        from PySide6.QtWidgets import QPushButton
        pump(lambda: any(b.text() == '应用并重新连接' and b.isEnabled()
                         for b in player.settings_dialog.findChildren(QPushButton)))
        display = player.settings_dialog.findChild(StreamOptions).display_snapshot
        if desktop:
            if not display:
                raise RuntimeError('Real desktop display capabilities were not returned.')
            result['display_capabilities'] = display
        app.processEvents()
        player.settings_dialog.grab().save(str(root / "stream-settings.png"))
        player.settings_dialog.reject()
        for index, name in enumerate(("devices", "settings", "network")):
            window.tabs.setCurrentIndex(index)
            app.processEvents()
            window.grab().save(str(root / f"{name}.png"))
    except Exception:
        result.update(ok=False, error=traceback.format_exc())
    finally:
        if player:
            player.close()
        future = runtime.submit(stop())
        try:
            pump(future.done)
            future.result()
        except Exception:
            result.update(ok=False, shutdown_error=traceback.format_exc())
        window.close()
        pump(lambda: window._closed)
        result["runtime_stopped"] = not runtime.thread.is_alive()
        atomic_json(root / "result.json", result)
    return 0 if result.get("ok") else 1
