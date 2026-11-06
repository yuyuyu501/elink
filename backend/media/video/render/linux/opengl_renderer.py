"""Linux 渲染端（SDL 窗口，OpenGL 后端）。

实现方式：把 ``Frame`` 的 BGRA 原始数据喂给 ffmpeg 的 ``sdl`` 输出，
由 SDL 建窗口并用它的 GL 后端上屏。好处是不需要在 Python 里管理 GL 上下文
（必须在固定线程、且要处理 EGL/GLX 差异），坏处是多了进程边界——对
「验证链路 + 真上屏」这个阶段是划算的取舍。

需要 ffmpeg 编译时带 ``--enable-sdl2``；不带时 ``available()`` 返回 False，
能力协商阶段会回退到写图渲染端。
"""

from __future__ import annotations

import shutil
import subprocess
from collections import deque

from backend.errors.base import ElinkError, ErrorCode
from backend.logging.logger import get_logger
from backend.media.frame import Frame, PixelFormat
from backend.media.video.codec.base import write_all

logger = get_logger(__name__)


class RenderError(ElinkError):
    code = ErrorCode.IO


def sdl_available(ffmpeg_path: str = "ffmpeg") -> bool:
    ffmpeg = shutil.which(ffmpeg_path)
    if not ffmpeg:
        return False
    try:
        output = subprocess.run(
            [ffmpeg, "-hide_banner", "-muxers"], capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return any(line.split()[1:2] == ["sdl"] for line in output.splitlines() if line.strip())


class OpenGLRenderer:
    """``render(frame)`` 把帧推到 SDL 窗口。"""

    name = "opengl"

    def __init__(self, title: str = "elink", ffmpeg_path: str = "ffmpeg") -> None:
        self.title = title
        self.ffmpeg_path = ffmpeg_path
        self._proc: subprocess.Popen[bytes] | None = None
        self._width = 0
        self._height = 0
        self._stderr_tail: deque[str] = deque(maxlen=20)
        self._frames = 0

    @staticmethod
    def available() -> bool:
        return sdl_available()

    def open(self) -> None:
        if not sdl_available(self.ffmpeg_path):
            raise RenderError(
                "当前 ffmpeg 未启用 SDL 输出，无法开窗渲染；请改用写图渲染端"
            )
        logger.info("等待首帧以确定窗口尺寸…")

    def render(self, frame: Frame) -> bool:
        if frame.pixel_format not in (PixelFormat.BGRA, PixelFormat.RGBA):
            raise RenderError(f"SDL 渲染需要 32 位像素格式，收到 {frame.pixel_format}")

        if self._proc is None:
            self._start(frame.width, frame.height)

        assert self._proc is not None and self._proc.stdin is not None
        try:
            write_all(self._proc.stdin, bytes(frame.data))
        except (BrokenPipeError, OSError) as exc:
            raise RenderError(
                "写入 SDL 窗口失败", detail=" | ".join(self._stderr_tail) or str(exc)
            ) from exc
        self._frames += 1
        return True

    def _start(self, width: int, height: int) -> None:
        self._width, self._height = width, height
        command = [
            self.ffmpeg_path,
            "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "rawvideo",
            "-pix_fmt", "bgra",
            "-video_size", f"{width}x{height}",
            "-i", "pipe:0",
            "-f", "sdl",
            self.title,
        ]
        try:
            self._proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except OSError as exc:
            raise RenderError("启动 SDL 渲染进程失败", detail=str(exc)) from exc
        logger.info("SDL 渲染窗口就绪: %dx%d", width, height)

    def close(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
            proc.wait(timeout=3)
        except (subprocess.TimeoutExpired, OSError):
            proc.kill()
        self._proc = None
        logger.info("SDL 渲染窗口关闭，共显示 %d 帧", self._frames)

    def __enter__(self) -> "OpenGLRenderer":
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
