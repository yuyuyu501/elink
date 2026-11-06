"""Linux 屏幕采集（X11 / Wayland）。

当前实现用 ffmpeg 抓屏并输出 rawvideo，再由本类切帧成 ``Frame``：

- X11：``-f x11grab``，最直接、延迟最低；
- Wayland：``x11grab`` 拿不到内容，需要 PipeWire 屏幕共享（portal 授权后
  通过 ``pipewire`` 设备抓），这一步依赖桌面环境的 portal 实现，暂未接入，
  会在 ``open()`` 时给出明确错误。

选 ffmpeg 而不是 ctypes 绑 XGetImage：X11 的大块像素传输用 XGetImage 会让
服务端/客户端同步等待，ffmpeg 的 x11grab 已经处理了共享内存等优化。
"""

from __future__ import annotations

import os
import shutil
import subprocess

from backend.errors.base import ElinkError, ErrorCode
from backend.logging.logger import get_logger
from backend.media.frame import Frame, FrameDomain, PixelFormat
from backend.media.timebase import Timebase

logger = get_logger(__name__)


class CaptureError(ElinkError):
    code = ErrorCode.IO


def _is_wayland() -> bool:
    return bool(os.environ.get("WAYLAND_DISPLAY")) or (
        os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
    )


class X11GrabCapture:
    """``read()`` 产出一帧 BGRA 的 ``Frame``。"""

    backend = "x11grab"
    pixel_format = PixelFormat.BGRA

    def __init__(
        self,
        region: tuple[int, int, int, int] | None = None,
        fps: float = 30.0,
        display: str = "",
        timebase: Timebase | None = None,
        ffmpeg_path: str = "ffmpeg",
    ) -> None:
        self.region = region
        self.fps = fps
        self.display = display or os.environ.get("DISPLAY", ":0")
        self.timebase = timebase or Timebase.mpegts()
        self.ffmpeg_path = ffmpeg_path
        self._proc: subprocess.Popen[bytes] | None = None
        self._frame_bytes = 0
        self._width = 0
        self._height = 0
        self._origin = (0, 0)
        self._start = 0.0

    @property
    def width(self) -> int:
        return self._width

    @property
    def height(self) -> int:
        return self._height

    def open(self) -> None:
        if _is_wayland():
            raise CaptureError(
                "Wayland 会话需要 PipeWire 屏幕共享（portal 授权）通路，当前未接入；"
                "可临时切到 X11 会话或在 XWayland 下抓取"
            )

        ffmpeg = shutil.which(self.ffmpeg_path)
        if not ffmpeg:
            raise CaptureError("未找到 ffmpeg，无法抓屏")

        x, y, width, height = self.region or (0, 0, 0, 0)
        if width <= 0 or height <= 0:
            # 未指定区域时抓整个屏幕：先向 ffmpeg 要默认尺寸由 x11grab 决定
            width, height = _query_screen_size(self.display) or (1920, 1080)
        width -= width % 2
        height -= height % 2

        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel", "error",
            "-nostdin",
            "-f", "x11grab",
            "-framerate", f"{self.fps:g}",
            "-video_size", f"{width}x{height}",
            "-i", f"{self.display}+{x},{y}",
            "-pix_fmt", "bgra",
            "-f", "rawvideo",
            "pipe:1",
        ]
        try:
            self._proc = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except OSError as exc:
            raise CaptureError("启动 x11grab 失败", detail=str(exc)) from exc

        self._width = width
        self._height = height
        self._origin = (x, y)
        self._frame_bytes = width * height * 4
        self._start = 0.0
        logger.info("屏幕采集就绪(X11): 区域=(%d,%d) %dx%d", x, y, width, height)

    def close(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except (subprocess.TimeoutExpired, OSError):
            proc.kill()
        self._proc = None

    def read(self) -> Frame:
        import time

        proc = self._proc
        if proc is None or proc.stdout is None:
            raise CaptureError("采集器未打开")

        buffer = bytearray()
        while len(buffer) < self._frame_bytes:
            chunk = proc.stdout.read(self._frame_bytes - len(buffer))
            if not chunk:
                raise CaptureError("x11grab 进程已退出")
            buffer += chunk

        if not self._start:
            self._start = time.perf_counter()
        pts = self.timebase.ticks(time.perf_counter() - self._start)
        return Frame(
            data=bytes(buffer),
            timebase=self.timebase,
            pts=pts,
            width=self._width,
            height=self._height,
            pixel_format=PixelFormat.BGRA,
            domain=FrameDomain.CPU,
        )

    def __enter__(self) -> "X11GrabCapture":
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def _query_screen_size(display: str) -> tuple[int, int] | None:
    """用 xdpyinfo 取屏幕尺寸；拿不到就交给调用方兜底。"""
    xdpyinfo = shutil.which("xdpyinfo")
    if not xdpyinfo:
        return None
    try:
        output = subprocess.run(
            [xdpyinfo, "-display", display],
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in output.splitlines():
        if "dimensions:" in line:
            parts = line.split()[1].split("x")
            try:
                return int(parts[0]), int(parts[1])
            except (IndexError, ValueError):
                return None
    return None


def create_capture(config=None) -> X11GrabCapture:
    """平台注册表用的工厂。"""
    region = getattr(config, "region", None) if config else None
    fps = getattr(config, "fps", 30.0) if config else 30.0
    return X11GrabCapture(region=region, fps=fps)
