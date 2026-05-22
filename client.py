import socket
import threading
import queue
import time
import json
import pyglet
import pyglet.image
import pyglet.sprite
from pyglet.window import key, mouse
from protocol import send_cmd, recv_cmd, recv_msg, MSG_VID, MSG_PNG
from codec import create_decoder

KEY_MAP = {
    key.LSHIFT: 'shift', key.RSHIFT: 'shift',
    key.LCTRL: 'ctrl', key.RCTRL: 'ctrl',
    key.LALT: 'alt', key.RALT: 'alt',
    key.LMETA: 'alt', key.RMETA: 'alt',
    key.LWINDOWS: 'win', key.RWINDOWS: 'win',
    key.UP: 'up', key.DOWN: 'down', key.LEFT: 'left', key.RIGHT: 'right',
    key.HOME: 'home', key.END: 'end', key.PAGEUP: 'pageup', key.PAGEDOWN: 'pagedown',
    key.RETURN: 'enter', key.TAB: 'tab',
    key.BACKSPACE: 'backspace', key.DELETE: 'delete', key.INSERT: 'insert',
    key.ESCAPE: 'escape', key.PAUSE: 'pause',
    key.PRINT: 'printscreen',
    key.MENU: 'menu',
    key.CAPSLOCK: 'capslock', key.NUMLOCK: 'numlock', key.SCROLLLOCK: 'scrolllock',
    key.SPACE: 'space',
    key.F1: 'f1', key.F2: 'f2', key.F3: 'f3', key.F4: 'f4',
    key.F5: 'f5', key.F6: 'f6', key.F7: 'f7', key.F8: 'f8',
    key.F9: 'f9', key.F10: 'f10', key.F11: 'f11', key.F12: 'f12',
    key._0: '0', key._1: '1', key._2: '2', key._3: '3', key._4: '4',
    key._5: '5', key._6: '6', key._7: '7', key._8: '8', key._9: '9',
    key.MINUS: '-', key.EQUAL: '=', key.BRACKETLEFT: '[', key.BRACKETRIGHT: ']',
    key.SEMICOLON: ';', key.APOSTROPHE: "'", key.COMMA: ',', key.PERIOD: '.', key.SLASH: '/',
    key.BACKSLASH: '\\', key.GRAVE: '`',
}

CTRL_KEYS = {'shift', 'ctrl', 'alt', 'win'}
MOUSE_BTN_MAP = {mouse.LEFT: 1, mouse.MIDDLE: 2, mouse.RIGHT: 3}


class ClientWindow(pyglet.window.Window):
    def __init__(self, host, port, on_disconnect=None):
        self.host = host
        self.port = port
        self.on_disconnect = on_disconnect

        self.sock = None
        self.running = False
        self.closing = False

        self.decoder = create_decoder()
        self.frame_queue = queue.Queue(maxsize=2)

        self.remote_w = 1920
        self.remote_h = 1080

        self.fps = 0
        self.frame_count = 0
        self.fps_timer = time.monotonic()
        self.latency = 0.0
        self.bandwidth = 0.0
        self.modifiers_pressed = set()

        super().__init__(1024, 700, f'Elink - {host}:{port}', resizable=True)

        self.texture = None
        self.sprite = None

        t = threading.Thread(target=self._connect, daemon=True)
        t.start()

        pyglet.clock.schedule_interval(self._update, 1 / 60)
        pyglet.clock.schedule_interval(self._send_ping, 2.0)

    # ---------- network ----------

    def _connect(self):
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(5.0)
            self.sock.connect((self.host, self.port))
            self.sock.settimeout(None)
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

            info = recv_cmd(self.sock)
            if info.get('type') != 'screen_info':
                raise ValueError('Missing screen_info')
            self.remote_w = info['width']
            self.remote_h = info['height']
            self.set_caption(f'Elink - {self.host}:{self.port} ({self.remote_w}x{self.remote_h})')

            self.running = True
            t = threading.Thread(target=self._receiver, daemon=True)
            t.start()
        except Exception as e:
            print(f'Connection failed: {e}')
            time.sleep(2)
            self._disconnect()

    def _receiver(self):
        while self.running:
            try:
                msg_type, data = recv_msg(self.sock)
                if msg_type == MSG_PNG:
                    resp = json.loads(data.decode('utf-8'))
                    self.latency = (time.monotonic() - resp['ts']) * 1000
                    if 'bw' in resp:
                        self.bandwidth = resp['bw'] / 1_000_000
                elif msg_type == MSG_VID:
                    for packet in self.decoder.parse(data):
                        for f in self.decoder.decode(packet):
                            if self.frame_queue.full():
                                try:
                                    self.frame_queue.get_nowait()
                                except queue.Empty:
                                    pass
                            self.frame_queue.put(f)
            except (ConnectionError, EOFError, OSError):
                self.running = False
                break
            except Exception:
                self.running = False
                break

    # ---------- display ----------

    def _update(self, dt):
        if not self.running or self.closing:
            return

        self.frame_count += 1
        if time.monotonic() - self.fps_timer >= 1.0:
            self.fps = self.frame_count
            self.frame_count = 0
            self.fps_timer = time.monotonic()

        try:
            f = self.frame_queue.get_nowait()
            img = f.to_ndarray(format='bgr24')
            h, w = img.shape[:2]
            if w == 0 or h == 0:
                return

            self.remote_w = w
            self.remote_h = h

            data = img[:, :, ::-1].tobytes()

            if self.texture is None:
                image_data = pyglet.image.ImageData(w, h, 'RGB', data)
                self.texture = image_data.get_texture()
                self.sprite = pyglet.sprite.Sprite(self.texture)
            elif w != self.texture.width or h != self.texture.height:
                image_data = pyglet.image.ImageData(w, h, 'RGB', data)
                self.texture = image_data.get_texture()
                self.sprite = pyglet.sprite.Sprite(self.texture)
            else:
                image_data = pyglet.image.ImageData(w, h, 'RGB', data)
                self.texture.blit_into(image_data, 0, 0, 0)
        except queue.Empty:
            pass

    def on_draw(self):
        self.clear()

        if self.sprite:
            cw, ch = self.width, self.height
            sx = cw / self.remote_w
            sy = ch / self.remote_h
            s = min(sx, sy)
            dw = int(self.remote_w * s)
            dh = int(self.remote_h * s)
            ox = (cw - dw) // 2
            oy = (ch - dh) // 2

            self.sprite.update(x=ox, y=oy, scale_x=dw / self.remote_w, scale_y=dh / self.remote_h)
            self.sprite.draw()

        pyglet.text.Label(
            f'FPS: {self.fps}  延迟: {self.latency:.0f}ms  带宽: {self.bandwidth:.1f}Mbps',
            font_size=13, x=10, y=self.height - 18,
            anchor_y='center', color=(0, 255, 0, 255),
        ).draw()

        if not self.running and not self.closing:
            pyglet.text.Label(
                '连接断开', font_size=24,
                x=self.width // 2, y=self.height // 2,
                anchor_x='center', anchor_y='center',
                color=(255, 0, 0, 255),
            ).draw()

    def on_resize(self, width, height):
        self.viewport = (0, 0, width, height)
        from pyglet.math import Mat4
        self.projection = Mat4.orthogonal_projection(0, width, 0, height, -1, 1)

    # ---------- input ----------

    def _img_rect(self):
        cw, ch = self.width, self.height
        sx = cw / self.remote_w
        sy = ch / self.remote_h
        s = min(sx, sy)
        dw = int(self.remote_w * s)
        dh = int(self.remote_h * s)
        ox = (cw - dw) // 2
        oy = (ch - dh) // 2
        return ox, oy, dw, dh

    def _to_remote(self, px, py):
        ox, oy, dw, dh = self._img_rect()
        if not (ox <= px <= ox + dw and oy <= py <= oy + dh):
            return None, None
        rel_x = (px - ox) / dw
        rel_y = (py - oy) / dh
        rx = int(rel_x * self.remote_w)
        ry = int((1 - rel_y) * self.remote_h)
        rx = max(0, min(rx, self.remote_w - 1))
        ry = max(0, min(ry, self.remote_h - 1))
        return rx, ry

    def on_mouse_motion(self, x, y, dx, dy):
        if not self.sock or not self.running:
            return
        rx, ry = self._to_remote(x, y)
        if rx is not None:
            try:
                send_cmd(self.sock, {'type': 'mouse_move', 'x': rx, 'y': ry})
            except OSError:
                pass

    def on_mouse_press(self, x, y, button, modifiers):
        if not self.sock or not self.running:
            return
        btn = MOUSE_BTN_MAP.get(button, 1)
        rx, ry = self._to_remote(x, y)
        if rx is not None:
            try:
                send_cmd(self.sock,
                         {'type': 'mouse_down', 'x': rx, 'y': ry, 'button': btn})
            except OSError:
                pass

    def on_mouse_release(self, x, y, button, modifiers):
        if not self.sock or not self.running:
            return
        btn = MOUSE_BTN_MAP.get(button, 1)
        rx, ry = self._to_remote(x, y)
        if rx is not None:
            try:
                send_cmd(self.sock,
                         {'type': 'mouse_up', 'x': rx, 'y': ry, 'button': btn})
            except OSError:
                pass

    def on_mouse_drag(self, x, y, dx, dy, buttons, modifiers):
        if not self.sock or not self.running:
            return
        rx, ry = self._to_remote(x, y)
        if rx is not None:
            try:
                send_cmd(self.sock, {'type': 'mouse_move', 'x': rx, 'y': ry})
            except OSError:
                pass

    def on_mouse_scroll(self, x, y, scroll_x, scroll_y):
        if not self.sock or not self.running:
            return
        if scroll_y:
            try:
                send_cmd(self.sock, {'type': 'scroll', 'dy': int(scroll_y)})
            except OSError:
                pass

    def _map_key(self, symbol):
        mapped = KEY_MAP.get(symbol)
        if mapped is not None:
            return mapped
        if 32 <= symbol <= 126:
            return chr(symbol)
        return None

    def on_key_press(self, symbol, modifiers):
        if not self.sock or not self.running:
            return
        mapped = self._map_key(symbol)
        if mapped is None:
            return

        if mapped in CTRL_KEYS:
            if mapped not in self.modifiers_pressed:
                self.modifiers_pressed.add(mapped)
                try:
                    send_cmd(self.sock, {'type': 'key', 'action': 'down', 'key': mapped})
                except OSError:
                    pass
        else:
            try:
                send_cmd(self.sock, {'type': 'key', 'action': 'press', 'key': mapped})
            except OSError:
                pass

    def on_key_release(self, symbol, modifiers):
        if not self.sock or not self.running:
            return
        mapped = self._map_key(symbol)
        if mapped is None:
            return

        if mapped in CTRL_KEYS:
            if mapped in self.modifiers_pressed:
                self.modifiers_pressed.discard(mapped)
                try:
                    send_cmd(self.sock, {'type': 'key', 'action': 'up', 'key': mapped})
                except OSError:
                    pass

    # ---------- lifecycle ----------

    def _send_ping(self, dt=None):
        if not self.running or not self.sock or self.closing:
            return
        try:
            send_cmd(self.sock, {'type': 'ping', 'ts': time.monotonic()})
        except OSError:
            pass

    def on_close(self):
        self.closing = True
        self.running = False
        self._disconnect()
        super().on_close()

    def _disconnect(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        if self.on_disconnect:
            self.on_disconnect()
