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
    from .logging_setup import configure_logging
    configure_logging(root)
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
        import logging
        notify = logging.getLogger(__name__).info
        server = HostServer(root / "host", synthetic=not desktop, backend_factory=RecordingInput, notify=notify)
        client = Client(root / "client", play_audio=not desktop, notify=notify)
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
        center = player.control_menu
        center.mouse_button.click()
        center.sensitivity.setValue(-12)
        assert player.mouse_sensitivity.value == -1.2 and center.isVisible() and center.mouse_options.isVisible()
        assert center.mouse_options.geometry().right() < center.geometry().left()
        center.statistics.click()
        assert not player.show_statistics and center.isVisible()
        player.open_controls()
        assert not center.isVisible() and not center.mouse_options.isVisible()
        player.open_controls()
        assert center.isVisible() and center.sensitivity.value() == -12
        result['session_controls'] = dict(persistent=True, sensitivity=True, toggle=True, side_panel=True)
        player.grab().save(str(root / "control-menu.png"))
        player.control_menu.close()
        player.open_quality()
        from .ui.controls import StreamOptions
        from PySide6.QtWidgets import QPushButton
        pump(lambda: any(b.text() == '应用' and b.isEnabled()
                         for b in player.settings_dialog.findChildren(QPushButton)))
        result['stream_preferences'] = player.settings_dialog.findChild(StreamOptions).values()
        app.processEvents()
        player.settings_dialog.grab().save(str(root / "stream-settings.png"))
        player.settings_dialog.reject()
        for index, name in enumerate(("devices", "settings", "network")):
            window.tabs.setCurrentIndex(index)
            app.processEvents()
            window.grab().save(str(root / f"{name}.png"))
        if window.tray:
            window.close_to_tray.setChecked(True)
            window.close()
            app.processEvents()
            assert not window.isVisible() and window.runtime.thread.is_alive() and not window._closing
            window.restore_window()
            assert window.isVisible()
            result['tray_controls'] = dict(hide=True, restore=True)
    except Exception:
        import logging
        logging.getLogger(__name__).exception('Desktop self-test failed')
        result.update(ok=False, error=traceback.format_exc(), metrics=dict(metrics),
                      video_frames=client.mailbox.received if client else 0,
                      audio_frames=client.audio_frames if client else 0,
                      stream_statistics=asdict(client.stats) if client else {},
                      has_image=bool(player and player.image is not None))
        if server:
            from .core.media import LoopbackTrack
            result['audio_capture'] = [dict(samples=t.samples, captured_blocks=t.captured_blocks,
                                            silence_blocks=t.silence_blocks, error=t.error,
                                            enabled=t.enabled.is_set(), alive=t.thread.is_alive())
                                       for t in server.tracks if isinstance(t, LoopbackTrack)]
    finally:
        if player:
            player.close()
        future = runtime.submit(stop())
        try:
            pump(future.done)
            future.result()
        except Exception:
            result.update(ok=False, shutdown_error=traceback.format_exc())
        window.request_exit()
        pump(lambda: window._closed)
        result["runtime_stopped"] = not runtime.thread.is_alive()
        atomic_json(root / "result.json", result)
    return 0 if result.get("ok") else 1
