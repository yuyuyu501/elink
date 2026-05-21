import socket
import threading
import queue
import io
import time
import json
import tkinter as tk
from PIL import Image, ImageTk
from protocol import send_cmd, recv_cmd, recv_msg, MSG_SCR, MSG_PNG

TK_KEY_MAP = {
    'Shift_L': 'shift', 'Shift_R': 'shift',
    'Control_L': 'ctrl', 'Control_R': 'ctrl',
    'Alt_L': 'alt', 'Alt_R': 'alt',
    'Meta_L': 'alt', 'Meta_R': 'alt',
    'Win_L': 'win', 'Win_R': 'win',
    'Super_L': 'win', 'Super_R': 'win',
    'Hyper_L': 'win', 'Hyper_R': 'win',
    'Up': 'up', 'Down': 'down', 'Left': 'left', 'Right': 'right',
    'Home': 'home', 'End': 'end', 'Prior': 'pageup', 'Next': 'pagedown',
    'Return': 'enter', 'Tab': 'tab',
    'BackSpace': 'backspace', 'Delete': 'delete', 'Insert': 'insert',
    'Escape': 'escape', 'Pause': 'pause', 'Break': 'pause',
    'Print': 'printscreen', 'Sys_Req': 'printscreen',
    'Menu': 'menu', 'Cancel': 'cancel', 'Clear': 'clear',
    'Caps_Lock': 'capslock', 'Num_Lock': 'numlock', 'Scroll_Lock': 'scrolllock',
    'space': 'space',
    'F1': 'f1', 'F2': 'f2', 'F3': 'f3', 'F4': 'f4',
    'F5': 'f5', 'F6': 'f6', 'F7': 'f7', 'F8': 'f8',
    'F9': 'f9', 'F10': 'f10', 'F11': 'f11', 'F12': 'f12',
}

CTRL_KEYS = {'shift', 'ctrl', 'alt', 'win'}


class ClientController(tk.Frame):
    def __init__(self, master, host, port, on_disconnect=None):
        super().__init__(master)
        self.master = master
        self.host = host
        self.port = port
        self.on_disconnect = on_disconnect

        self.sock = None
        self.remote_w = 1920
        self.remote_h = 1080
        self.scale = 1.0
        self.offset_x = 0
        self.offset_y = 0
        self.display_w = 1
        self.display_h = 1

        self.mouse_held = {}
        self.modifiers_pressed = set()

        self.img_queue = queue.Queue(maxsize=2)
        self.running = False
        self.photo = None

        self.fps = 0
        self.frame_count = 0
        self.fps_timer = time.monotonic()
        self.latency = 0.0
        self.bandwidth = 0.0

        self._build_ui()
        self.after(500, self._connect)

    def _build_ui(self):
        self.pack(fill=tk.BOTH, expand=True)

        top = tk.Frame(self)
        top.pack(fill=tk.X, side=tk.TOP)

        tk.Label(top, text=f'目标: {self.host}:{self.port}').pack(side=tk.LEFT, padx=8)
        self.status_var = tk.StringVar(value='连接中...')
        tk.Label(top, textvariable=self.status_var).pack(side=tk.LEFT, padx=8)
        self.fps_label = tk.Label(top, text='FPS: 0')
        self.fps_label.pack(side=tk.LEFT, padx=8)
        self.latency_label = tk.Label(top, text='延迟: 0ms')
        self.latency_label.pack(side=tk.LEFT, padx=8)
        self.bandwidth_label = tk.Label(top, text='带宽: 0Mbps')
        self.bandwidth_label.pack(side=tk.LEFT, padx=8)
        tk.Button(top, text='断开', command=self._disconnect).pack(side=tk.RIGHT, padx=8, pady=2)

        self.canvas = tk.Canvas(self, bg='black', highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)

        self.canvas.bind('<Motion>', self._on_mouse_move)
        self.canvas.bind('<Button-1>', self._on_mouse_down)
        self.canvas.bind('<ButtonRelease-1>', self._on_mouse_up)
        self.canvas.bind('<Button-2>', self._on_mouse_down)
        self.canvas.bind('<ButtonRelease-2>', self._on_mouse_up)
        self.canvas.bind('<Button-3>', self._on_mouse_down)
        self.canvas.bind('<ButtonRelease-3>', self._on_mouse_up)
        self.canvas.bind('<MouseWheel>', self._on_mousewheel)
        self.canvas.bind('<Button-4>', self._on_scroll_up)
        self.canvas.bind('<Button-5>', self._on_scroll_down)
        self.canvas.bind('<Configure>', self._on_resize)

        self.canvas.focus_set()
        self.canvas.bind('<KeyPress>', self._on_key_press)
        self.canvas.bind('<KeyRelease>', self._on_key_release)

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
            self.master.title(f'Elink - {self.host}:{self.port} ({self.remote_w}x{self.remote_h})')

            self.running = True
            self.status_var.set('已连接')

            t = threading.Thread(target=self._screen_receiver, daemon=True)
            t.start()

            self.after(50, self._update_display)
            self.after(2000, self._send_ping)
        except Exception as e:
            self.status_var.set(f'连接失败: {e}')
            self.after(2000, self._on_disconnect)

    def _screen_receiver(self):
        while self.running:
            try:
                msg_type, data = recv_msg(self.sock)
                if msg_type == MSG_PNG:
                    resp = json.loads(data.decode('utf-8'))
                    self.latency = (time.monotonic() - resp['ts']) * 1000
                    if 'bw' in resp:
                        self.bandwidth = resp['bw'] / 1_000_000
                elif msg_type == MSG_SCR:
                    img = Image.open(io.BytesIO(data))
                    if self.img_queue.full():
                        try:
                            self.img_queue.get_nowait()
                        except queue.Empty:
                            pass
                    self.img_queue.put(img)
            except (ConnectionError, EOFError, OSError):
                self.running = False
                break
            except Exception:
                self.running = False
                break

    def _update_display(self):
        if not self.running:
            return

        now = time.monotonic()
        self.frame_count += 1
        dt = now - self.fps_timer
        if dt >= 1.0:
            self.fps = self.frame_count / dt
            self.frame_count = 0
            self.fps_timer = now

        try:
            img = self.img_queue.get_nowait()

            cw = self.canvas.winfo_width()
            ch = self.canvas.winfo_height()
            if cw > 1 and ch > 1:
                self.display_w, self.display_h = cw, ch
                sx = cw / self.remote_w
                sy = ch / self.remote_h
                self.scale = min(sx, sy)
                dw = int(self.remote_w * self.scale)
                dh = int(self.remote_h * self.scale)
                self.offset_x = (cw - dw) // 2
                self.offset_y = (ch - dh) // 2

                img_resized = img.resize((dw, dh), Image.LANCZOS)

                self.photo = ImageTk.PhotoImage(img_resized)
                self.canvas.delete('screen')
                self.canvas.create_image(
                    self.offset_x, self.offset_y,
                    anchor=tk.NW, image=self.photo, tag='screen'
                )
        except queue.Empty:
            pass

        self.fps_label.config(text=f'FPS: {self.fps:.0f}')
        self.latency_label.config(text=f'延迟: {self.latency:.0f}ms')
        self.bandwidth_label.config(text=f'带宽: {self.bandwidth:.1f}Mbps')

        self.after(50, self._update_display)

    def _send_ping(self):
        if not self.running or not self.sock:
            return
        try:
            send_cmd(self.sock, {'type': 'ping', 'ts': time.monotonic()})
        except OSError:
            pass
        self.after(2000, self._send_ping)

    def _to_remote(self, cx, cy):
        rx = int((cx - self.offset_x) / self.scale)
        ry = int((cy - self.offset_y) / self.scale)
        rx = max(0, min(rx, self.remote_w - 1))
        ry = max(0, min(ry, self.remote_h - 1))
        return rx, ry

    def _on_mouse_move(self, event):
        if not self.sock:
            return
        rx, ry = self._to_remote(event.x, event.y)
        try:
            send_cmd(self.sock, {'type': 'mouse_move', 'x': rx, 'y': ry})
        except OSError:
            pass

    def _on_mouse_down(self, event):
        if not self.sock:
            return
        self.mouse_held[event.num] = True
        rx, ry = self._to_remote(event.x, event.y)
        try:
            send_cmd(self.sock, {
                'type': 'mouse_down', 'x': rx, 'y': ry, 'button': event.num
            })
        except OSError:
            pass

    def _on_mouse_up(self, event):
        if not self.sock:
            return
        self.mouse_held.pop(event.num, None)
        rx, ry = self._to_remote(event.x, event.y)
        try:
            send_cmd(self.sock, {
                'type': 'mouse_up', 'x': rx, 'y': ry, 'button': event.num
            })
        except OSError:
            pass

    def _on_mousewheel(self, event):
        if not self.sock:
            return
        dy = event.delta // 120
        try:
            send_cmd(self.sock, {'type': 'scroll', 'dy': dy})
        except OSError:
            pass

    def _on_scroll_up(self, event):
        if not self.sock:
            return
        try:
            send_cmd(self.sock, {'type': 'scroll', 'dy': 1})
        except OSError:
            pass

    def _on_scroll_down(self, event):
        if not self.sock:
            return
        try:
            send_cmd(self.sock, {'type': 'scroll', 'dy': -1})
        except OSError:
            pass

    def _on_key_press(self, event):
        if not self.sock:
            return
        key = self._map_key(event)
        if key is None:
            return

        if key in CTRL_KEYS:
            if key not in self.modifiers_pressed:
                self.modifiers_pressed.add(key)
                try:
                    send_cmd(self.sock, {'type': 'key', 'action': 'down', 'key': key})
                except OSError:
                    pass
        else:
            try:
                send_cmd(self.sock, {'type': 'key', 'action': 'press', 'key': key})
            except OSError:
                pass

    def _on_key_release(self, event):
        if not self.sock:
            return
        key = self._map_key(event)
        if key is None:
            return

        if key in CTRL_KEYS:
            if key in self.modifiers_pressed:
                self.modifiers_pressed.discard(key)
                try:
                    send_cmd(self.sock, {'type': 'key', 'action': 'up', 'key': key})
                except OSError:
                    pass

    def _map_key(self, event):
        if event.keysym in TK_KEY_MAP:
            return TK_KEY_MAP[event.keysym]

        if event.char and ord(event.char) >= 32:
            return event.char

        ks = event.keysym.lower()
        if len(ks) == 1 and ks.isalnum():
            return ks

        return None

    def _on_resize(self, event):
        self.display_w = max(1, event.width)
        self.display_h = max(1, event.height)

    def _disconnect(self):
        self.running = False
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        self._on_disconnect()

    def _on_disconnect(self):
        if self.on_disconnect:
            self.on_disconnect()

    def stop(self):
        self.running = False
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
