"""前端联调服务器：静态文件 + 控制通道 WebSocket。

一个进程、一个端口同时提供两件事：

1. ``GET /mobile/`` 等静态资源（移动端 PWA 源码）；
2. ``GET /control``（``Upgrade: websocket``）—— 与后端共用
   ``control_channel/protocol.py`` 的协议帧，前端因此不需要任何构建工具。

它只用于本地联调：没有鉴权、没有 TLS、默认**不注入**输入事件
（需要显式 ``--allow-inject``，避免调试时鼠标被自己带着跑）。

预览帧走 JSON + base64：体积比二进制大 33%，但省掉了前端解帧的复杂度，
在预览这种低频（默认 2fps、宽 480）场景完全可以接受。
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import socket
import struct
import sys
import threading
import time
import zlib
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))

from backend.control_channel import protocol  # noqa: E402
from backend.control_channel.base import ChannelState  # noqa: E402
from backend.control_channel.websocket import WebSocketServerChannel  # noqa: E402
from backend.logging.logger import get_logger, setup_logging  # noqa: E402
from backend.media.device.enumerate import DeviceKind, enumerate_devices  # noqa: E402
from backend.media.device.display import list_displays  # noqa: E402
from backend.platform import detect  # noqa: E402

logger = get_logger(__name__)

INDEX_FILES = ("index.html",)
MAX_REQUEST_HEADER = 32 * 1024


# ---------------------------------------------------------------- 预览编码
def _downscale_bgra(data: bytes, width: int, height: int, target_width: int) -> tuple[bytes, int, int]:
    """最近邻缩小（预览够用，纯 Python 也能跑到 2fps 以上）。"""
    if width <= target_width:
        return data, width, height
    ratio = width / target_width
    target_height = max(2, int(height / ratio) & ~1)
    target_width &= ~1
    out = bytearray(target_width * target_height * 4)
    for y in range(target_height):
        source_y = min(height - 1, int(y * ratio))
        source_row = source_y * width * 4
        target_row = y * target_width * 4
        for x in range(target_width):
            source_x = min(width - 1, int(x * ratio)) * 4
            out[target_row + x * 4 : target_row + x * 4 + 4] = data[
                source_row + source_x : source_row + source_x + 4
            ]
    return bytes(out), target_width, target_height


def _encode_png(width: int, height: int, bgra: bytes) -> bytes:
    """极简 PNG 编码（BGRX -> RGB，滤波 0）。"""
    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    rows = bytearray()
    stride = width * 4
    for y in range(height):
        rows.append(0)
        base = y * stride
        for x in range(width):
            index = base + x * 4
            rows += bytes((bgra[index + 2], bgra[index + 1], bgra[index]))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(rows), 1))
        + chunk(b"IEND", b"")
    )


# ---------------------------------------------------------------- 服务器
class DevServer:
    """静态资源 + WebSocket 控制通道。"""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8099,
        *,
        allow_inject: bool = False,
        preview_fps: float = 2.0,
        preview_width: int = 480,
    ) -> None:
        self.host = host
        self.port = port
        self.allow_inject = allow_inject
        self.preview_fps = preview_fps
        self.preview_width = preview_width

        self._listener: socket.socket | None = None
        self._clients: list[WebSocketServerChannel] = []
        self._clients_lock = threading.Lock()
        self._running = threading.Event()
        self._injector = None
        self._inject_enabled = False
        self.injected_events = 0
        self.preview_frames = 0
        self._frame_seq = 0

    # ---- 生命周期 ----
    def start(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.host, self.port))
        listener.listen(16)
        self._listener = listener
        self._running.set()

        threading.Thread(target=self._accept_loop, name="dev-accept", daemon=True).start()
        threading.Thread(target=self._preview_loop, name="dev-preview", daemon=True).start()
        logger.info("前端联调服务: http://%s:%d/mobile/", self.host, self.port)

    def stop(self) -> None:
        self._running.clear()
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        with self._clients_lock:
            for client in self._clients:
                client.close()
            self._clients.clear()
        if self._injector is not None:
            self._injector.close()
            self._injector = None

    # ---- 连接处理 ----
    def _accept_loop(self) -> None:
        assert self._listener is not None
        while self._running.is_set():
            try:
                conn, address = self._listener.accept()
            except OSError:
                break
            threading.Thread(
                target=self._handle_connection,
                args=(conn, address),
                name=f"dev-conn-{address[1]}",
                daemon=True,
            ).start()

    def _handle_connection(self, conn: socket.socket, address) -> None:
        try:
            request_line, headers = _read_http_request(conn)
        except (OSError, ValueError):
            conn.close()
            return
        if not request_line:
            conn.close()
            return

        try:
            method, target, _version = request_line.split(" ", 2)
        except ValueError:
            _send_response(conn, 400, "Bad Request", b"bad request line")
            conn.close()
            return

        path = unquote(urlparse(target).path)
        if headers.get("upgrade", "").lower() == "websocket":
            if path.rstrip("/") not in ("/control", "/ws"):
                _send_response(conn, 404, "Not Found", b"unknown websocket endpoint")
                conn.close()
                return
            self._upgrade(conn, headers, address)
            return

        if method not in ("GET", "HEAD"):
            _send_response(conn, 405, "Method Not Allowed", b"only GET")
            conn.close()
            return

        self._serve_static(conn, path, head_only=method == "HEAD")

    # ---- 静态资源 ----
    def _serve_static(self, conn: socket.socket, path: str, *, head_only: bool = False) -> None:
        if path in ("", "/"):
            _send_response(conn, 302, "Found", b"", extra_headers=("Location: /mobile/\r\n",))
            conn.close()
            return

        relative = path.lstrip("/")
        target = (ROOT / relative).resolve()
        if not str(target).startswith(str(ROOT)):
            _send_response(conn, 403, "Forbidden", b"forbidden")
            conn.close()
            return

        if target.is_dir():
            for index_name in INDEX_FILES:
                candidate = target / index_name
                if candidate.is_file():
                    target = candidate
                    break
            else:
                _send_response(conn, 404, "Not Found", b"index.html not found")
                conn.close()
                return

        if not target.is_file():
            _send_response(conn, 404, "Not Found", b"not found")
            conn.close()
            return

        body = target.read_bytes()
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if target.suffix == ".js":
            content_type = "text/javascript; charset=utf-8"
        elif target.suffix in (".html", ".css"):
            content_type = f"{content_type}; charset=utf-8"
        _send_response(
            conn,
            200,
            "OK",
            body,
            content_type=content_type,
            head_only=head_only,
            extra_headers=("Cache-Control: no-store\r\n",),
        )
        conn.close()

    # ---- WebSocket ----
    def _upgrade(self, conn: socket.socket, headers: dict[str, str], address) -> None:
        client_key = headers.get("sec-websocket-key", "")
        channel = WebSocketServerChannel()
        channel.on_message(self._on_message)
        try:
            channel.accept(conn, client_key, peer=f"{address[0]}:{address[1]}")
        except Exception:
            logger.exception("WebSocket 握手失败")
            conn.close()
            return

        with self._clients_lock:
            self._clients.append(channel)

        channel.send_command(
            protocol.ControlCommand(name="hello", args=self._hello_payload())
        )
        self._send_devices(channel)
        self._send_state(channel)

        # 等连接关闭后在读线程里自然退出；这里只是把引用挂到列表里管理
        def reap() -> None:
            thread = getattr(channel, "_thread", None)
            if thread is not None:
                thread.join()
            with self._clients_lock:
                if channel in self._clients:
                    self._clients.remove(channel)
            logger.info("前端连接已断开: %s", channel.peer)

        threading.Thread(target=reap, name="dev-reap", daemon=True).start()

    def _hello_payload(self) -> dict:
        info = detect()
        return {
            "platform": info.platform.value,
            "arch": info.arch,
            "interactive": info.interactive,
            "allow_inject": self.allow_inject,
            "preview_fps": self.preview_fps,
            "preview_width": self.preview_width,
            "version": "0.1.0",
        }

    def _send_devices(self, channel: WebSocketServerChannel) -> None:
        devices = []
        for kind in (DeviceKind.DISPLAY, DeviceKind.CAMERA, DeviceKind.MICROPHONE):
            try:
                for device in enumerate_devices(kind):
                    devices.append(
                        {
                            "kind": device.kind.value,
                            "id": device.identifier,
                            "name": device.name,
                            "default": device.is_default,
                        }
                    )
            except Exception:  # 单类设备枚举失败不影响其它类别
                logger.debug("枚举 %s 失败", kind, exc_info=True)

        channel.send_command(
            protocol.ControlCommand(
                name="devices",
                args={"items": devices, "displays": [display.describe() for display in list_displays()]},
            )
        )

    def _send_state(self, channel: WebSocketServerChannel) -> None:
        channel.send_command(
            protocol.ControlCommand(
                name="state",
                args={
                    "inject_enabled": self._inject_enabled,
                    "injected_events": self.injected_events,
                    "preview_frames": self.preview_frames,
                    "clients": len(self._clients),
                    "preview_fps": self.preview_fps,
                },
            )
        )

    # ---- 消息处理 ----
    def _on_message(self, message) -> None:
        if isinstance(message, protocol.InputEvent):
            self._handle_input(message)
            return
        if isinstance(message, protocol.ControlCommand):
            self._handle_command(message)
            return
        logger.debug("忽略未知消息: %r", type(message))

    def _handle_input(self, event: protocol.InputEvent) -> None:
        if not (self.allow_inject and self._inject_enabled):
            logger.debug("收到输入事件但未开启注入: %s", event.event_type.value)
            return
        if self._injector is None:
            from backend.input_injection.windows.send_input import SendInputInjector

            self._injector = SendInputInjector()
        if self._injector.inject(event):
            self.injected_events += 1
            logger.debug(
                "注入输入事件: %s (%d,%d)", event.event_type.value, event.x, event.y
            )

    def _handle_command(self, command: protocol.ControlCommand) -> None:
        name = command.name
        args = command.args or {}

        if name == "hello":
            self._broadcast(protocol.ControlCommand(name="hello", args=self._hello_payload()))
        elif name == "devices":
            with self._clients_lock:
                clients = list(self._clients)
            for client in clients:
                self._send_devices(client)
        elif name == "toggle_inject":
            enabled = bool(args.get("enabled"))
            if enabled and not self.allow_inject:
                self._broadcast(
                    protocol.ControlCommand(
                        name="error",
                        args={"message": "服务端未以 --allow-inject 启动，注入被拒绝"},
                    )
                )
                return
            self._inject_enabled = enabled
            self._broadcast(
                protocol.ControlCommand(name="state", args={"inject_enabled": enabled})
            )
            logger.info("输入注入: %s", "开启" if enabled else "关闭")
        elif name == "set_preview":
            self.preview_fps = float(args.get("fps", self.preview_fps) or self.preview_fps)
            self.preview_width = int(args.get("width", self.preview_width) or self.preview_width)
            self._broadcast(
                protocol.ControlCommand(
                    name="state",
                    args={"preview_fps": self.preview_fps, "preview_width": self.preview_width},
                )
            )
        elif name == "ping":
            self._broadcast(protocol.ControlCommand(name="pong", args={"t": time.time()}))
        else:
            logger.debug("未处理的控制指令: %s", name)

    def _broadcast(self, command: protocol.ControlCommand) -> None:
        with self._clients_lock:
            clients = [client for client in self._clients if client.state == ChannelState.OPEN]
        for client in clients:
            try:
                client.send_command(command)
            except Exception:
                logger.debug("广播失败，跳过该客户端", exc_info=True)

    def _drop_closed_clients(self) -> None:
        """清掉已经断开的通道，避免每帧都去尝试写死连接。"""
        with self._clients_lock:
            self._clients = [c for c in self._clients if c.state == ChannelState.OPEN]

    # ---- 预览 ----
    def _preview_loop(self) -> None:
        capture = None
        try:
            if detect().platform.value != "windows":
                logger.warning("预览仅实现了 Windows 采集，前端仍可收发控制消息")
                return

            from backend.config.schema import CaptureConfig
            from backend.media.video.capture.windows.dxgi_desktop_duplication import (
                DesktopCapture,
            )

            capture = DesktopCapture(CaptureConfig(fps=min(10.0, self.preview_fps)))
            capture.open()
        except Exception as exc:
            logger.warning("预览采集不可用: %s", exc)
            if capture is not None:
                capture.close()
            return

        try:
            while self._running.is_set():
                with self._clients_lock:
                    has_client = bool(self._clients)
                if not has_client:
                    time.sleep(0.2)
                    continue

                begin = time.perf_counter()
                frame = capture.read()
                small, width, height = _downscale_bgra(
                    bytes(frame.data), frame.width, frame.height, self.preview_width
                )
                payload = _encode_png(width, height, small)
                self._frame_seq += 1
                self.preview_frames += 1

                self._broadcast(
                    protocol.ControlCommand(
                        name="frame",
                        args={
                            "seq": self._frame_seq,
                            "width": width,
                            "height": height,
                            # 远端屏幕真实像素尺寸：前端靠它把点击坐标换算回去
                            "screen_width": frame.width,
                            "screen_height": frame.height,
                            "pts_ms": int(frame.pts / 90),
                            "image": base64.b64encode(payload).decode("ascii"),
                        },
                    )
                )

                elapsed = time.perf_counter() - begin
                time.sleep(max(0.0, 1.0 / max(0.2, self.preview_fps) - elapsed))
        finally:
            if capture is not None:
                capture.close()


# ---------------------------------------------------------------- HTTP 工具
def _read_http_request(conn: socket.socket) -> tuple[str, dict[str, str]]:
    buffer = bytearray()
    while b"\r\n\r\n" not in buffer:
        chunk = conn.recv(4096)
        if not chunk:
            return "", {}
        buffer += chunk
        if len(buffer) > MAX_REQUEST_HEADER:
            raise ValueError("请求头过大")

    head, _, _rest = buffer.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    request_line = lines[0] if lines else ""
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    return request_line, headers


def _send_response(
    conn: socket.socket,
    status: int,
    reason: str,
    body: bytes,
    *,
    content_type: str = "text/plain; charset=utf-8",
    head_only: bool = False,
    extra_headers: tuple[str, ...] = (),
) -> None:
    headers = [
        f"HTTP/1.1 {status} {reason}\r\n",
        f"Content-Type: {content_type}\r\n",
        f"Content-Length: {len(body)}\r\n",
        "Connection: close\r\n",
        *extra_headers,
        "\r\n",
    ]
    conn.sendall("".join(headers).encode("latin-1"))
    if not head_only:
        conn.sendall(body)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="elink 前端联调服务器")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument(
        "--allow-inject",
        action="store_true",
        help="允许前端控制本机输入（默认关闭，避免调试时误操作）",
    )
    parser.add_argument("--preview-fps", type=float, default=2.0)
    parser.add_argument("--preview-width", type=int, default=480)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    setup_logging(args.log_level)
    server = DevServer(
        args.host,
        args.port,
        allow_inject=args.allow_inject,
        preview_fps=args.preview_fps,
        preview_width=args.preview_width,
    )
    server.start()
    print(f"移动端: http://{args.host}:{args.port}/mobile/")
    print("（桌面端为 Qt 应用：python main.py --ui）")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n正在停止…")
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
