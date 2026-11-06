"""Linux 音频播放（PipeWire）。

``pw-cat --playback`` 收 PCM 流；写入时靠管道背压自然限速：管道满时
``write`` 会阻塞，播放线程因此不会把队列撑爆（音频不丢帧的落点）。
"""

from __future__ import annotations

import shutil
import subprocess

from backend.errors.base import ElinkError, ErrorCode
from backend.logging.logger import get_logger
from backend.media.frame import Frame
from backend.media.video.codec.base import write_all

logger = get_logger(__name__)


class AudioPlaybackError(ElinkError):
    code = ErrorCode.IO


class PipeWirePlayback:
    backend = "pipewire"

    def __init__(
        self,
        sample_rate: int = 48_000,
        channels: int = 2,
        target: str = "",
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.target = target
        self._proc: subprocess.Popen[bytes] | None = None

    def _build_command(self) -> list[str]:
        if shutil.which("pw-cat"):
            self.backend = "pw-cat"
            command = [
                "pw-cat",
                "--playback",
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
            raise AudioPlaybackError("既没有 pw-cat 也没有 ffmpeg，无法播放音频")
        self.backend = "ffmpeg-pulse"
        return [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "s16le",
            "-ar", str(self.sample_rate),
            "-ac", str(self.channels),
            "-i", "pipe:0",
            "-f", "pulse",
            self.target or "default",
        ]

    def open(self) -> None:
        command = self._build_command()
        try:
            self._proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except OSError as exc:
            raise AudioPlaybackError("启动音频播放失败", detail=str(exc)) from exc
        logger.info("音频播放就绪(%s): %dHz/%dch", self.backend, self.sample_rate, self.channels)

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

    def write(self, frame: Frame) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise AudioPlaybackError("播放器未打开")
        if (frame.sample_format or "s16") != "s16":
            raise AudioPlaybackError("播放器只接受 s16 PCM")
        try:
            write_all(proc.stdin, bytes(frame.data))
        except (BrokenPipeError, OSError) as exc:
            raise AudioPlaybackError("写入播放进程失败", detail=str(exc)) from exc

    def __enter__(self) -> "PipeWirePlayback":
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
