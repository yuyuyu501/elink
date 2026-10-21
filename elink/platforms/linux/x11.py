"""X11 backends. All X connections are owned and closed by their caller."""
import numpy as np

from .keyboard import keysym_name


class X11Capture:
    name = 'x11-mss'

    def __init__(self):
        import mss
        self.screen = mss.mss(with_cursor=True)
        self.monitor = self.screen.monitors[1]
        self.cursor_visible = None

    def read(self, draw_mouse=True):
        self.screen.with_cursor = draw_mouse
        return np.asarray(self.screen.grab(self.monitor))

    def close(self):
        self.screen.close()


class X11Input:
    def __init__(self, pad_backend='uinput', notify=lambda message: None):
        from Xlib import display
        self.display = display.Display()
        if not self.display.has_extension('XTEST'):
            self.display.close()
            raise RuntimeError('X11 XTEST extension is unavailable')
        self.root = self.display.screen().root
        import mss
        with mss.mss() as screen:
            self.monitor = dict(screen.monitors[1])
        self.pad_backend, self.notify = pad_backend, notify
        self.pads = None
        self.wheel_remainder = 0

    def emit(self, event):
        from Xlib import X, XK
        from Xlib.ext import xtest
        kind = event['type']
        if kind == 'pad':
            if self.pad_backend == 'disabled':
                raise RuntimeError('Virtual gamepads are disabled')
            if self.pads is None:
                from .gamepad import UInputPads
                self.pads = UInputPads()
            self.pads.update(event['index'], event['values'])
            return
        if kind == 'key':
            name = keysym_name(event)
            code = self.display.keysym_to_keycode(XK.string_to_keysym(name)) if name else 0
            if not code:
                raise RuntimeError(f"No X11 mapping for VK {event['vk']}")
            xtest.fake_input(self.display, X.KeyPress if event['down'] else X.KeyRelease, code)
        elif kind == 'move':
            if event['absolute']:
                x = self.monitor['left'] + round(event['x'] * (self.monitor['width'] - 1) / 65535)
                y = self.monitor['top'] + round(event['y'] * (self.monitor['height'] - 1) / 65535)
                xtest.fake_input(self.display, X.MotionNotify, x=x, y=y)
            else:
                xtest.fake_input(self.display, X.MotionNotify, detail=True, x=event['x'], y=event['y'])
        elif kind == 'button':
            xtest.fake_input(self.display, X.ButtonPress if event['down'] else X.ButtonRelease,
                             {'left': 1, 'middle': 2, 'right': 3}[event['button']])
        elif kind == 'wheel':
            self.wheel_remainder += event['delta']
            while abs(self.wheel_remainder) >= 120:
                button = 4 if self.wheel_remainder > 0 else 5
                xtest.fake_input(self.display, X.ButtonPress, button)
                xtest.fake_input(self.display, X.ButtonRelease, button)
                self.wheel_remainder += -120 if self.wheel_remainder > 0 else 120
        self.display.sync()

    def close(self):
        if self.pads:
            self.pads.close()
        self.display.close()
