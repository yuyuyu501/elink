"""Linux X11 rendering and input check in an owned test window."""
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace

import av
import numpy as np
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QLineEdit

from elink.core.media import FrameMailbox
from elink.platforms.linux.x11 import X11Input
from elink.ui.player import Player


class Runtime(QObject):
    feedback = Signal(dict)
    def call(self, function, *args):
        function(*args)


def main():
    app = QApplication([])
    root = Path('.artifacts/linux-desktop-check')
    root.mkdir(parents=True, exist_ok=True)
    results = {}
    for renderer in ('cpu', 'gpu'):
        mailbox = FrameMailbox()
        client = SimpleNamespace(mailbox=mailbox, pc=None, rtt_ms=0, last_pong=0,
                                 focus=lambda active: None, send=lambda *args, **kwargs: None,
                                 set_renderer=lambda mode: None, cursor_modes=['remote'])
        runtime = Runtime()
        player = Player(runtime, client, controllers=False, preferences={'renderer': renderer})
        player.show()
        pixels = np.zeros((360, 640, 3), np.uint8)
        pixels[:, :320] = (40, 180, 230)
        pixels[:, 320:] = (200, 80, 30)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            mailbox.put(av.VideoFrame.from_ndarray(pixels, format='bgr24').reformat(format='yuv420p')
                        if renderer == 'gpu' else pixels)
            app.processEvents()
            time.sleep(.01)
        image = player.gpu_surface.grabFramebuffer() if player.gpu_surface else player.grab().toImage()
        image.save(str(root / f'{renderer}.png'))
        colors = [image.pixelColor(image.width() * x // 4, image.height() // 2) for x in (1, 3)]
        rgb = [(c.red(), c.green(), c.blue()) for c in colors]
        assert sum(abs(a - b) for a, b in zip(rgb[0], rgb[1])) > 150, rgb
        assert renderer != 'gpu' or player.gpu_surface is not None, 'GPU fell back'
        results[renderer] = dict(ok=True, colors=rgb)
        player.close()
        app.processEvents()
    field = QLineEdit()
    field.resize(320, 100)
    field.show()
    app.processEvents()
    from Xlib import X
    backend = X11Input(pad_backend='disabled')
    focus = backend.display.get_input_focus().focus
    position = backend.root.query_pointer()
    try:
        backend.display.create_resource_object('window', int(field.winId())).set_input_focus(X.RevertToParent, X.CurrentTime)
        backend.display.sync()
        for down in (True, False):
            backend.emit(dict(type='key', vk=65, scan=0, extended=False, down=down))
        for _ in range(20):
            app.processEvents()
            time.sleep(.01)
        assert field.text() == 'a', field.text()
        backend.emit(dict(type='move', absolute=True, x=32768, y=32768))
        actual = backend.root.query_pointer()
        expected_x = backend.monitor['left'] + (backend.monitor['width'] - 1) / 2
        assert abs(actual.root_x - expected_x) <= 1
        results['input'] = dict(key=True, captured_monitor_coordinates=True)
    finally:
        if hasattr(focus, 'set_input_focus'):
            focus.set_input_focus(X.RevertToParent, X.CurrentTime)
        backend.root.warp_pointer(position.root_x, position.root_y)
        backend.display.sync()
        backend.close()
        field.close()
    (root / 'result.json').write_text(json.dumps(results, indent=2))
    print(json.dumps(results))


if __name__ == '__main__':
    main()
