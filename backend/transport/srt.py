"""SRT 字节通道。

Python 侧不做 SRT 协议实现，而是借 ffmpeg 的 libsrt 收发字节流：

- 发送：本项目封装的 TS 字节 -> ffmpeg stdin -> ``srt://``
- 接收：``srt://`` -> ffmpeg stdout -> 本项目解封装

关键是 ``-c copy``：ffmpeg 只搬字节、不重新编码，所以「封装/解封装/编解码」
仍然全部在 ``media/`` 里，网络问题与抽象问题能分开定位——把
``TransportConfig.kind`` 从 ``file`` 换成 ``srt``，管线其余部分完全不变。
"""

from __future__ import annotations

import subprocess
import threading
from collections import deque
from typing import BinaryIO

from backend.errors.network import TransportError
from backend.logging.logger import get_logger

logger = get_logger(__name__)

_DEFAULT_PORT = 9000


def build_srt_url(url: str, *, mode: str, latency_ms: int) -> str:
    """在用户给的 URL 上补齐 SRT 选项（mode / latency）。"""
    if not url.startswith("srt://"):
        url = f"srt://{url}"
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}mode={mode}&latency={latency_ms}"


class SrtTransport:
    """一对 stdin/stdout 管道，分别交给封装器与解封装器使用。"""

    def __init__(
        self,
        url: str,
        *,
        mode: str = "caller",
        latency_ms: int = 120,
        ffmpeg_path: str = "ffmpeg",
        loglevel: str = "error",
    ) -> None:
        if mode not in {"caller", "listener"}:
            raise TransportError(f"非法 SRT 模式: {mode}", detail=mode)
        self.url = build_srt_url(url, mode=mode, latency_ms=latency_ms)
        self.mode = mode
        self.ffmpeg_path = ffmpeg_path
        self.loglevel = loglevel
        self._proc: subprocess.Popen[bytes] | None = None
        self._stderr_tail: deque[str] = deque(maxlen=40)
        self._stderr_thread: threading.Thread | None = None

    # ---- 生命周期 ----
    def open(self) -> None:
        if self._proc is not None:
            return

        # ffmpeg 默认 probesize=5MB / analyzeduration=5s：短会话里它会先把整个
        # 流攒齐才开始转发，表现为「结束时才收到数据」。我们的 TS 开头就有
        # PAT/PMT，用最小探测窗口即可实时转发。
        # 注意：不要用 -fflags nobuffer / -avioflags direct，实测会让 mpegts
        # 解析拿不到完整数据（直接 0 输出）。
        probe_flags = ["-probesize", "32768", "-analyzeduration", "100000"]

        if self.mode == "caller":
            cmd = [
                self.ffmpeg_path,
                "-hide_banner",
                "-loglevel", self.loglevel,
                "-nostdin",
                *probe_flags,
                "-f", "mpegts",
                "-i", "pipe:0",
                "-c", "copy",
                "-flush_packets", "1",
                "-f", "mpegts",
                self.url,
            ]
        else:
            cmd = [
                self.ffmpeg_path,
                "-hide_banner",
                "-loglevel", self.loglevel,
                "-nostdin",
                *probe_flags,
                "-i", self.url,
                "-c", "copy",
                "-flush_packets", "1",
                "-f", "mpegts",
                "pipe:1",
            ]

        logger.info("启动 SRT 通道(%s): %s", self.mode, self.url)
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            raise TransportError("无法启动 SRT 通道", detail=str(exc)) from exc

        self._stderr_thread = threading.Thread(
            target=self._stderr_loop, name="srt-stderr", daemon=True
        )
        self._stderr_thread.start()

    def close(self) -> None:
        proc = self._proc
        if proc is None:
            return
        for stream in (proc.stdin, proc.stdout):
            try:
                if stream and not stream.closed:
                    stream.close()
            except (BrokenPipeError, OSError, ValueError):
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        self._proc = None

    # ---- 管道视图 ----
    @property
    def writer(self) -> BinaryIO:
        """封装器写入端（发送模式）。"""
        if self.mode != "caller":
            raise TransportError("只有 caller 模式有写入端")
        if self._proc is None or self._proc.stdin is None:
            raise TransportError("SRT 通道尚未打开")
        return self._proc.stdin

    @property
    def reader(self) -> BinaryIO:
        """解封装读取端（接收模式）。"""
        if self.mode != "listener":
            raise TransportError("只有 listener 模式有读取端")
        if self._proc is None or self._proc.stdout is None:
            raise TransportError("SRT 通道尚未打开")
        return self._proc.stdout

    def stderr_text(self) -> str:
        return " | ".join(self._stderr_tail) or "无 stderr 输出"

    def _stderr_loop(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        for raw in iter(self._proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if line:
                self._stderr_tail.append(line)
                logger.debug("srt: %s", line)

    def __enter__(self) -> "SrtTransport":
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
