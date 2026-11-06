"""Linux 音频采集（PipeWire）。

优先用 ``pw-cat``（PipeWire 官方命令行，延迟低、无需绑定库），没有时回退到
ffmpeg 的 ``pulse`` 输入设备。两条路径都输出 16bit 小端 PCM，所以上层拿到的
是同样的 ``Frame``。

选 ``pw-cat`` 而不是 ctypes 绑 libpipewire：PipeWire 的接口是面向回调的
异步 C API，用子进程拿 PCM 流反而更稳、更容易排障。
"""

from __future__ import annotations

import shutil
import subprocess
import time
from collections import deque

from backend.errors.base import ElinkError, ErrorCode
from backend.logging.logger import get_logger
from backend.media.frame import Frame
from backend.media.stream import MediaType
from backend.media.timebase import Timebase
from backend.media.video.codec.base import write_all

logger = get_logger(__name__)


class AudioCaptureError(ElinkError):
    code = ErrorCode.IO


class PipeWireCapture:
    """``read()`` 产出一帧 16bit PCM。"""

    backend = "pipewire"

    def __init__(
        self,
        sample_rate: int = 48_000,
        channels: int = 2,
        frame_ms: int = 20,
        target: str = "",
        timebase: Timebase | None = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_ms = frame_ms
        self.target = target
        self.timebase = timebase or Timebase.microseconds()
        self._proc: subprocess.Popen[bytes] | None = None
        self._stderr_tail: deque[str] = deque(maxlen=20)
        self._buffer = bytearray()
        self._samples_read = 0

    @property
    def frame_bytes(self) -> int:
        return int(self.sample_rate * self.frame_ms / 1000) * self.channels * 2

    def _build_command(self) -> list[str]:
        if shutil.which("pw-cat"):
            self.backend = "pw-cat"
            command = [
                "pw-cat",
                "--record",
                "--format", "s16",
                "--rate", str(self.sample_rate),
                "--channels", str(self.channels),
                "--raw",
            ]
            if self.target:
                command += ["--target", self.target]
            return command + ["-"]

        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise AudioCaptureError("既没有 pw-cat 也没有 ffmpeg，无法采集音频")
        self.backend = "ffmpeg-pulse"
        return [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "pulse", "-i", self.target or "default",
            "-f", "s16le",
            "-ar", str(self.sample_rate),
            "-ac", str(self.channels),
            "pipe:1",
        ]

    def open(self) -> None:
        command = self._build_command()
        logger.info("音频采集就绪(%s): %dHz/%dch", self.backend, self.sample_rate, self.channels)
        try:
            self._proc = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except OSError as exc:
            raise AudioCaptureError("启动音频采集失败", detail=str(exc)) from exc

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
        proc = self._proc
        if proc is None or proc.stdout is None:
            raise AudioCaptureError("采集器未打开")

        while len(self._buffer) < self.frame_bytes:
            chunk = proc.stdout.read(self.frame_bytes - len(self._buffer))
            if not chunk:
                raise AudioCaptureError("音频采集进程已退出")
            self._buffer += chunk

        data = bytes(self._buffer[: self.frame_bytes])
        del self._buffer[: self.frame_bytes]
        samples = self.frame_bytes // (self.channels * 2)
        pts = self.timebase.ticks(self._samples_read / self.sample_rate)
        self._samples_read += samples
        return Frame(
            data=data,
            timebase=self.timebase,
            pts=pts,
            media_type=MediaType.AUDIO,
            sample_rate=self.sample_rate,
            channels=self.channels,
            sample_format="s16",
            samples=samples,
        )

    def __enter__(self) -> "PipeWireCapture":
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def create_capture(config=None) -> PipeWireCapture:
    """平台注册表用的工厂。"""
    sample_rate = getattr(config, "sample_rate", 48_000) if config else 48_000
    channels = getattr(config, "channels", 2) if config else 2
    return PipeWireCapture(sample_rate=sample_rate, channels=channels)
