import socket
import threading
import io
import time
import hashlib
import pyautogui
from PIL import Image, ImageGrab
from protocol import send_cmd, recv_cmd, send_screen

MOUSE_BUTTON_MAP = {1: 'left', 2: 'middle', 3: 'right'}
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

pyautogui.FAILSAFE = False


class Server:
    def __init__(self, host='0.0.0.0', port=5901):
        self.host = host
        self.port = port
        self.server_sock = None
        self.client_sock = None
        self.running = False
        self.current_bps = 0

    def start(self, on_status=None, on_error=None):
        self.running = True
        self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_sock.bind((self.host, self.port))
        self.server_sock.listen(1)
        self.server_sock.settimeout(1.0)

        t = threading.Thread(target=self._accept_loop, args=(on_status, on_error), daemon=True)
        t.start()

    def _accept_loop(self, on_status, on_error):
        while self.running:
            try:
                client, addr = self.server_sock.accept()
                self.client_sock = client
                if on_status:
                    on_status(f'已连接: {addr[0]}:{addr[1]}')
                self._handle_client(client, on_status, on_error)
                if on_status:
                    on_status('等待连接...')
                self.client_sock = None
            except socket.timeout:
                continue
            except OSError:
                break
            except Exception as e:
                if on_error:
                    on_error(str(e))
                break

    def _handle_client(self, sock, on_status, on_error):
        try:
            w, h = pyautogui.size()
            send_cmd(sock, {'type': 'screen_info', 'width': w, 'height': h})
        except Exception as e:
            if on_error:
                on_error(str(e))
            return

        running = True

        def cmd_receiver():
            nonlocal running
            while running:
                try:
                    cmd = recv_cmd(sock)
                    self._execute_cmd(cmd, sock)
                except (ConnectionError, ValueError):
                    running = False
                    break
                except Exception as e:
                    if on_error:
                        on_error(f'cmd error: {e}')
                    running = False
                    break

        def screen_sender():
            nonlocal running
            prev_hash = None
            quality = 50
            TARGET_BPS = 8_000_000
            FRAME_INTERVAL = 1.0 / 60
            window_bytes = 0
            window_start = time.monotonic()
            while running:
                frame_start = time.monotonic()
                try:
                    img = ImageGrab.grab()
                    thumb = img.resize((32, 24), Image.LANCZOS)
                    h = hashlib.md5(thumb.tobytes()).hexdigest()
                    if h != prev_hash:
                        prev_hash = h
                        buf = io.BytesIO()
                        img.save(buf, format='JPEG', quality=quality)
                        data = buf.getvalue()
                        send_screen(sock, data)
                        window_bytes += len(data)

                    now = time.monotonic()
                    elapsed = now - window_start
                    if elapsed >= 1.0:
                        actual_bps = window_bytes * 8 / elapsed
                        self.current_bps = actual_bps
                        if actual_bps > TARGET_BPS * 1.1:
                            quality = max(10, quality - 5)
                        elif actual_bps < TARGET_BPS * 0.6 and quality < 85:
                            quality += 5
                        window_bytes = 0
                        window_start = now

                    sleep_time = FRAME_INTERVAL - (now - frame_start)
                    if sleep_time > 0:
                        time.sleep(sleep_time)
                except (ConnectionError, OSError):
                    running = False
                    break
                except Exception as e:
                    if on_error:
                        on_error(f'screen error: {e}')
                    running = False
                    break

        threads = [
            threading.Thread(target=cmd_receiver, daemon=True),
            threading.Thread(target=screen_sender, daemon=True),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        try:
            sock.close()
        except OSError:
            pass

    def _execute_cmd(self, cmd, sock=None):
        t = cmd.get('type')

        if t == 'ping' and sock:
            from protocol import send_png
            import json
            send_png(sock, json.dumps({
                'ts': cmd['ts'],
                'bw': self.current_bps,
            }).encode('utf-8'))

        elif t == 'mouse_move':
            pyautogui.moveTo(cmd['x'], cmd['y'])

        elif t == 'mouse_down':
            btn = MOUSE_BUTTON_MAP.get(cmd.get('button', 1), 'left')
            pyautogui.mouseDown(cmd['x'], cmd['y'], button=btn)

        elif t == 'mouse_up':
            btn = MOUSE_BUTTON_MAP.get(cmd.get('button', 1), 'left')
            pyautogui.mouseUp(cmd['x'], cmd['y'], button=btn)

        elif t == 'scroll':
            pyautogui.scroll(cmd.get('dy', 0))

        elif t == 'key':
            action = cmd.get('action')
            key = cmd.get('key')
            if action == 'press':
                pyautogui.press(key)
            elif action == 'down':
                pyautogui.keyDown(key)
            elif action == 'up':
                pyautogui.keyUp(key)

    def stop(self):
        self.running = False
        if self.client_sock:
            try:
                self.client_sock.close()
            except OSError:
                pass
        if self.server_sock:
            try:
                self.server_sock.close()
            except OSError:
                pass
