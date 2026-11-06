"""音频编解码接口与通用后端基类。

数据契约与视频一致：

- 编码器：``Frame``（PCM）-> ``Packet``（AAC/Opus/FLAC 基本流）
- 解码器：``Packet`` -> ``Frame``（PCM）

与视频不同的是，音频包的边界由码流自己决定（AAC 的 ADTS 帧、Ogg 页、
FLAC 帧），所以时间戳按「累计采样数 / 采样率」推算，而不是复述输入帧的 pts ——
这样即使编码器内部有延迟，输出也能完全对齐。

所有后端同样借 ffmpeg 子进程做算法，Python 只负责搬运与切包。
"""

from __future__ import annotations

import queue
import subprocess
import threading
from abc import ABC, abstractmethod
from collections import deque

from backend.errors.codec import CodecOpenError, DecodeError, EncodeError
from backend.logging.logger import get_logger
from backend.media.frame import Frame
from backend.media.packet import Packet
from backend.media.stream import CodecId, MediaType, StreamInfo
from backend.media.timebase import Timebase
from backend.media.video.codec.base import write_all

logger = get_logger(__name__)

READ_CHUNK = 64 * 1024


class AudioEncoder(ABC):
    """音频编码器接口。"""

    name: str = ""
    codec: str = ""
    hardware: bool = False

    def __init__(self, stream: StreamInfo) -> None:
        self.stream = stream
        self._opened = False

    @abstractmethod
    def open(self) -> None:
        ...

    @abstractmethod
    def encode(self, frame: Frame) -> list[Packet]:
        ...

    @abstractmethod
    def flush(self) -> list[Packet]:
        ...

    def close(self) -> None:
        self._opened = False


class AudioDecoder(ABC):
    """音频解码器接口。"""

    name: str = ""
    codec: str = ""

    def __init__(self, stream: StreamInfo) -> None:
        self.stream = stream
        self._opened = False

    @abstractmethod
    def open(self) -> None:
        ...

    @abstractmethod
    def decode(self, packet: Packet) -> list[Frame]:
        ...

    @abstractmethod
    def flush(self) -> list[Frame]:
        ...

    def close(self) -> None:
        self._opened = False


class FfmpegAudioEncoderBase(AudioEncoder):
    """PCM -> ffmpeg -> 编码基本流 -> 按码流规则切包。"""

    #: ffmpeg 编码器名
    ffmpeg_encoder: str = ""
    #: 输出容器（决定包边界规则）
    output_format: str = "adts"
    #: 输入 PCM 采样格式
    sample_format: str = "s16le"
    #: 每包采样数（切包器可以逐包覆盖）
    samples_per_frame: int = 1024

    def __init__(
        self,
        stream: StreamInfo,
        bitrate_kbps: int = 128,
        ffmpeg_path: str = "ffmpeg",
    ) -> None:
        super().__init__(stream)
        self.bitrate_kbps = bitrate_kbps
        self.ffmpeg_path = ffmpeg_path
        self._proc: subprocess.Popen[bytes] | None = None
        self._buffer = bytearray()
        self._out: "queue.Queue[Packet]" = queue.Queue()
        self._reader: threading.Thread | None = None
        self._stderr_tail: deque[str] = deque(maxlen=30)
        self._stderr_thread: threading.Thread | None = None
        self._timebase = Timebase.microseconds()
        self._samples_out = 0
        self._reader_error: str | None = None

    # ---- 子类实现 ----
    def split_packets(self, buffer: bytearray) -> list[tuple[bytes, int]]:
        """从缓冲区切出完整包，返回 ``(字节, 采样数)``，并消费掉已切出的部分。"""
        raise NotImplementedError

    # ---- 生命周期 ----
    def open(self) -> None:
        stream = self.stream
        if stream.sample_rate <= 0 or stream.channels <= 0:
            raise CodecOpenError("音频编码器缺少采样率/声道数")

        cmd = [
            self.ffmpeg_path,
            "-hide_banner",
            "-loglevel", "error",
            "-nostdin",
            "-f", self.sample_format,
            "-ar", str(stream.sample_rate),
            "-ac", str(stream.channels),
            "-i", "pipe:0",
            "-c:a", self.ffmpeg_encoder,
            "-b:a", f"{self.bitrate_kbps}k",
            "-f", self.output_format,
            "pipe:1",
        ]
        logger.debug("启动音频编码后端(%s): %s", self.name, " ".join(cmd))
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
            raise CodecOpenError("无法启动 ffmpeg 音频编码", detail=str(exc)) from exc

        self._reader = threading.Thread(
            target=self._read_loop, name=f"{self.name}-aout", daemon=True
        )
        self._reader.start()
        self._stderr_thread = threading.Thread(
            target=self._stderr_loop, name=f"{self.name}-aerr", daemon=True
        )
        self._stderr_thread.start()
        self._opened = True

    def close(self) -> None:
        proc = self._proc
        if proc is None:
            self._opened = False
            return
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        self._proc = None
        self._opened = False

    # ---- 主路径 ----
    def encode(self, frame: Frame) -> list[Packet]:
        if self._proc is None or self._proc.stdin is None:
            raise EncodeError("音频编码器尚未打开")
        try:
            write_all(self._proc.stdin, bytes(frame.data))
        except (BrokenPipeError, OSError) as exc:
            raise EncodeError(
                "音频编码进程异常退出", detail=f"{exc}; stderr: {self._stderr_text()}"
            ) from exc
        return self._drain()

    def flush(self) -> list[Packet]:
        proc = self._proc
        if proc is None:
            return []
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        if self._reader is not None:
            self._reader.join(timeout=10)

        output = self._drain()
        if self._buffer:
            # 尾部不完整的一包也要交出去，否则末尾音频会丢
            remaining = bytes(self._buffer)
            self._buffer.clear()
            packet = self._build_packet(remaining, self.samples_per_frame)
            if packet is not None:
                output.append(packet)
        if self._reader_error:
            raise EncodeError(self._reader_error, detail=self._stderr_text())
        return output

    # ---- 内部 ----
    def _drain(self) -> list[Packet]:
        output: list[Packet] = []
        while True:
            try:
                output.append(self._out.get_nowait())
            except queue.Empty:
                break
        return output

    def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            while True:
                chunk = self._proc.stdout.read(READ_CHUNK)
                if not chunk:
                    break
                self._buffer += chunk
                for data, samples in self.split_packets(self._buffer):
                    packet = self._build_packet(data, samples)
                    if packet is not None:
                        self._out.put(packet)
        except Exception as exc:
            self._reader_error = f"读取音频编码输出失败: {exc}"
            logger.exception("%s 输出读取失败", self.name)

    def _build_packet(self, data: bytes, samples: int) -> Packet | None:
        if not data:
            return None
        sample_rate = self.stream.sample_rate or 48_000
        pts = self._timebase.ticks(self._samples_out / sample_rate)
        duration = self._timebase.ticks(samples / sample_rate)
        self._samples_out += samples
        return Packet(
            data=data,
            codec=CodecId(self.codec),
            timebase=self._timebase,
            pts=pts,
            dts=pts,
            duration=duration,
            stream_id=self.stream.stream_id,
        )

    def _stderr_loop(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        for raw in iter(self._proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if line:
                self._stderr_tail.append(line)

    def _stderr_text(self) -> str:
        return " | ".join(self._stderr_tail) or "无 stderr 输出"


class FfmpegAudioDecoderBase(AudioDecoder):
    """编码基本流 -> ffmpeg -> PCM -> Frame。

    输出固定 ``s16le``，按 ``samples_per_frame`` 切成等长帧（每帧对应一个播放单元）。
    """

    #: ffmpeg 解码器名；None 表示自动
    ffmpeg_decoder: str | None = None
    #: 输入容器
    input_format: str = "adts"
    #: 每帧采样数（AAC 1024 / Opus 960 / FLAC 4096）
    samples_per_frame: int = 1024
    #: 输出 PCM 采样格式
    output_format: str = "s16le"

    def __init__(
        self,
        stream: StreamInfo,
        bitrate_kbps: int = 128,
        ffmpeg_path: str = "ffmpeg",
    ) -> None:
        super().__init__(stream)
        self.bitrate_kbps = bitrate_kbps
        self.ffmpeg_path = ffmpeg_path
        self._proc: subprocess.Popen[bytes] | None = None
        self._frames: "queue.Queue[bytes]" = queue.Queue()
        self._reader: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stderr_tail: deque[str] = deque(maxlen=30)
        self._reader_error: str | None = None
        self._frame_size = 0
        self._samples_out = 0
        self._timebase = Timebase.microseconds()

    def open(self) -> None:
        stream = self.stream
        if stream.sample_rate <= 0 or stream.channels <= 0:
            raise CodecOpenError("音频解码器缺少采样率/声道数")
        self._frame_size = self.samples_per_frame * stream.channels * 2
        self._timebase = Timebase.microseconds()

        decode_args = ["-c:a", self.ffmpeg_decoder] if self.ffmpeg_decoder else []
        cmd = [
            self.ffmpeg_path,
            "-hide_banner",
            "-loglevel", "error",
            "-nostdin",
            "-f", self.input_format,
            "-i", "pipe:0",
            *decode_args,
            "-f", self.output_format,
            "pipe:1",
        ]
        logger.debug("启动音频解码后端(%s): %s", self.name, " ".join(cmd))
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
            raise CodecOpenError("无法启动 ffmpeg 音频解码", detail=str(exc)) from exc

        self._reader = threading.Thread(
            target=self._read_loop, name=f"{self.name}-adout", daemon=True
        )
        self._reader.start()
        self._stderr_thread = threading.Thread(
            target=self._stderr_loop, name=f"{self.name}-aderr", daemon=True
        )
        self._stderr_thread.start()
        self._opened = True

    def decode(self, packet: Packet) -> list[Frame]:
        if self._proc is None or self._proc.stdin is None:
            raise DecodeError("音频解码器尚未打开")
        try:
            write_all(self._proc.stdin, bytes(packet.data))
        except (BrokenPipeError, OSError) as exc:
            raise DecodeError(
                "音频解码进程异常退出", detail=f"{exc}; {self._stderr_text()}"
            ) from exc
        return self._drain(wait=0.02)

    def flush(self) -> list[Frame]:
        proc = self._proc
        if proc is None:
            return []
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        if self._reader is not None:
            self._reader.join(timeout=10)
        frames = self._drain(wait=None)
        if self._reader_error:
            raise DecodeError(self._reader_error, detail=self._stderr_text())
        return frames

    def close(self) -> None:
        proc = self._proc
        if proc is not None:
            try:
                if proc.stdin and not proc.stdin.closed:
                    proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        self._proc = None
        self._opened = False

    # ---- 内部 ----
    def _drain(self, wait: float | None) -> list[Frame]:
        frames: list[Frame] = []
        while True:
            try:
                if wait is None or frames:
                    data = self._frames.get_nowait()
                else:
                    data = self._frames.get(timeout=wait)
            except queue.Empty:
                break
            frames.append(self._build_frame(data))
        return frames

    def _build_frame(self, data: bytes) -> Frame:
        stream = self.stream
        sample_rate = stream.sample_rate or 48_000
        pts = self._timebase.ticks(self._samples_out / sample_rate)
        samples = len(data) // max(1, stream.channels * 2)
        self._samples_out += samples
        return Frame(
            data=data,
            timebase=self._timebase,
            pts=pts,
            media_type=MediaType.AUDIO,
            duration=self._timebase.ticks(samples / sample_rate),
            sample_rate=sample_rate,
            channels=stream.channels,
            sample_format="s16",
            samples=samples,
            stream_id=stream.stream_id,
        )

    def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        stdout = self._proc.stdout
        buffer = bytearray()
        try:
            while True:
                chunk = stdout.read(READ_CHUNK)
                if not chunk:
                    break
                buffer += chunk
                while len(buffer) >= self._frame_size:
                    self._frames.put(bytes(buffer[: self._frame_size]))
                    del buffer[: self._frame_size]
        except Exception as exc:
            self._reader_error = f"读取音频解码输出失败: {exc}"
            logger.exception("%s 输出读取失败", self.name)

    def _stderr_loop(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        for raw in iter(self._proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if line:
                self._stderr_tail.append(line)

    def _stderr_text(self) -> str:
        return " | ".join(self._stderr_tail) or "无 stderr 输出"
