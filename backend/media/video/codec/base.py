"""视频编解码接口与通用后端基类。

codec 层不感知平台框架，只依赖本文件的抽象：

- 编码器：``Frame`` -> ``Packet``
- 解码器：``Packet`` -> ``Frame``

具体后端（``libx264`` / ``h264_nvenc`` / ``h264_qsv`` ...）由
``registry/codec_registry.py`` 结合 ``codec/probe.py`` 的探测结果选型。

本文件同时提供 ``FfmpegEncoderBase`` / ``FfmpegDecoderBase``：它们把「用 ffmpeg
子进程做算法、用 Python 做搬运」的通用逻辑收在一处，各后端文件只声明自己
对应哪个 ffmpeg 编码器名，从而保证所有后端的行为（切包、时间戳、错误处理）一致。
"""

from __future__ import annotations

import queue
import subprocess
import threading
from abc import ABC, abstractmethod
from collections import deque

from backend.config.schema import VideoEncodeConfig
from backend.errors.codec import CodecOpenError, DecodeError, EncodeError
from backend.logging.logger import get_logger
from backend.media.frame import Frame, FrameDomain, PixelFormat
from backend.media.packet import Packet
from backend.media.stream import CodecId, StreamInfo
from backend.media.timebase import Timebase

logger = get_logger(__name__)

START_CODE = b"\x00\x00\x01"
START_CODE4 = b"\x00\x00\x00\x01"
READ_CHUNK = 64 * 1024
DEFAULT_DRAIN_TIMEOUT = 0.05
NAL_AUD = 9


# ---------------------------------------------------------------- 字节工具
def write_all(sink, data: bytes) -> None:
    """把 data 写满 sink。

    无缓冲管道（``Popen(buffsize=0)`` 的 stdin）上的 ``write`` 可能只写一部分，
    忽略返回值会静默丢字节，表现为「码流损坏 / TS 被截断」。
    """
    view = memoryview(data)
    while view:
        written = sink.write(view)
        if not written:
            raise OSError("管道写入返回 0")
        view = view[written:]


def unescape_nal(nal: bytes) -> bytes:
    """去掉防竞争字节（00 00 03 -> 00 00）。"""
    out = bytearray()
    zeros = 0
    for byte in nal:
        if zeros >= 2 and byte == 0x03:
            zeros = 0
            continue
        out.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return bytes(out)


def first_mb_in_slice(nal: bytes) -> int:
    """解析 slice_header 的 first_mb_in_slice（ue(v)）；失败返回 -1。"""
    if len(nal) < 2:
        return -1
    body = unescape_nal(nal[1:])
    if not body:
        return -1
    head = int.from_bytes(body[:8].ljust(8, b"\x00"), "big")

    leading = 0
    while leading < 32 and not (head >> (63 - leading)) & 1:
        leading += 1
    if leading >= 32:
        return -1

    suffix = 0
    for i in range(leading):
        suffix = (suffix << 1) | ((head >> (63 - (leading + 1 + i))) & 1)
    return (1 << leading) - 1 + suffix


class AnnexBParser:
    """流式 Annex-B 切分：管道里是连续字节流，不能假设一次 read 就是完整 NAL。"""

    def __init__(self) -> None:
        self._buf = bytearray()

    def push(self, chunk: bytes) -> list[bytes]:
        self._buf += chunk
        buf = self._buf

        starts: list[int] = []
        cursor = 0
        while True:
            index = buf.find(START_CODE, cursor)
            if index < 0:
                break
            starts.append(index)
            cursor = index + 3

        if len(starts) < 2:
            return []

        nals: list[bytes] = []
        for i in range(len(starts) - 1):
            begin = starts[i] + 3
            end = starts[i + 1]
            while end > begin and buf[end - 1] == 0:
                end -= 1
            if end > begin:
                nals.append(bytes(buf[begin:end]))

        del buf[: starts[-1]]
        return nals

    def flush(self) -> list[bytes]:
        """EOF 时把最后一个 NAL 也交出来。

        没有后续起始码就无法判定上一个 NAL 的结尾；漏掉它会让最后一帧少一个
        slice，解码侧报 corrupt frame。
        """
        buf = self._buf
        if not buf:
            return []
        index = buf.find(START_CODE)
        if index < 0:
            self._buf = bytearray()
            return []

        begin = index + 3
        end = len(buf)
        while end > begin and buf[end - 1] == 0:
            end -= 1
        self._buf = bytearray()
        return [bytes(buf[begin:end])] if end > begin else []


class AccessUnitSplitter:
    """把 NAL 聚成访问单元（一帧一个 AU）。

    有 AUD（NAL 9）时以 AUD 为界；没有时按「新的 slice 且 first_mb_in_slice==0」
    判定新 AU 开始。硬件编码器不一定插 AUD，两条规则都要有。
    """

    def __init__(self) -> None:
        self._current = bytearray()
        self._has_vcl = False

    def push(self, nal: bytes) -> list[bytes]:
        nal_type = nal[0] & 0x1F
        is_vcl = 1 <= nal_type <= 5

        boundary = False
        if nal_type == NAL_AUD and self._current:
            boundary = True
        elif is_vcl and self._has_vcl and first_mb_in_slice(nal) == 0:
            boundary = True

        out: list[bytes] = []
        if boundary:
            out.append(self._take())

        self._current += START_CODE4 + nal
        if is_vcl:
            self._has_vcl = True
        return out

    def take(self) -> bytes | None:
        return self._take() if self._current else None

    def _take(self) -> bytes:
        data = bytes(self._current)
        self._current.clear()
        self._has_vcl = False
        return data


# ---------------------------------------------------------------- 接口
class BaseEncoder(ABC):
    """编码器：一实例一 worker，禁止跨线程共享。"""

    name: str = ""
    codec: str = ""
    hardware: bool = False

    def __init__(self, stream: StreamInfo) -> None:
        self.stream = stream
        self._opened = False

    @classmethod
    def available(cls, ffmpeg_path: str = "ffmpeg") -> bool:
        """运行期可用性；后端不可用时由注册表回退。"""
        return True

    @abstractmethod
    def open(self) -> None:
        """分配后端资源；失败抛 ``CodecOpenError``。"""

    @abstractmethod
    def encode(self, frame: Frame) -> list[Packet]:
        """送入一帧，返回当前可输出的包（可能为空）。"""

    @abstractmethod
    def flush(self) -> list[Packet]:
        """冲刷内部缓冲，返回剩余包（结尾调用一次）。"""

    def close(self) -> None:
        self._opened = False

    def __enter__(self) -> "BaseEncoder":
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


class BaseDecoder(ABC):
    """解码器：一实例一 worker。"""

    name: str = ""
    codec: str = ""
    hardware: bool = False

    def __init__(self, stream: StreamInfo) -> None:
        self.stream = stream
        self._opened = False

    @classmethod
    def available(cls, ffmpeg_path: str = "ffmpeg") -> bool:
        return True

    @abstractmethod
    def open(self) -> None:
        """分配后端资源；失败抛 ``CodecOpenError``。"""

    @abstractmethod
    def decode(self, packet: Packet) -> list[Frame]:
        """送入一个包，返回当前可输出的帧（可能为空）。"""

    @abstractmethod
    def flush(self) -> list[Frame]:
        """冲刷内部缓冲，返回剩余帧（结尾调用一次）。"""

    def close(self) -> None:
        self._opened = False

    def __enter__(self) -> "BaseDecoder":
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


# ---------------------------------------------------------------- 通用后端
class FfmpegEncoderBase(BaseEncoder):
    """基于 ffmpeg 子进程的视频编码器基类。

    ``原始 Frame --stdin(rawvideo)--> ffmpeg(--c:v 后端) --stdout(ES)--> 切分 AU --> Packet``

    子类只需声明：

    - ``ffmpeg_encoder``：ffmpeg 的 ``-c:v`` 名字
    - ``codec`` / ``hardware``
    - 需要时覆盖 :meth:`video_args` 补充后端专属参数
    """

    ffmpeg_encoder: str = ""
    #: 该后端是否支持 ``-preset``
    preset_supported: bool = True
    #: 该后端是否支持 ``-tune``（仅 x264/x265 有）
    tune: str | None = None
    #: 额外的固定参数，例如 ``(("-x264-params", "aud=1:repeat-headers=1"),)``
    private_params: tuple[tuple[str, str], ...] = ()

    def __init__(
        self,
        stream: StreamInfo,
        config: VideoEncodeConfig | None = None,
    ) -> None:
        super().__init__(stream)
        self.config = config or VideoEncodeConfig()
        self._proc: subprocess.Popen[bytes] | None = None
        self._out: "queue.Queue[Packet]" = queue.Queue()
        self._pts_lock = threading.Lock()
        self._pending_pts: deque[int] = deque()
        self._reader: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stderr_tail: deque[str] = deque(maxlen=40)
        self._reader_error: str | None = None
        self._timestamp = Timebase.mpegts()

    # ---- 参数 ----
    def video_args(self) -> list[str]:
        """``-c:v`` 及之后的编码参数。"""
        cfg = self.config
        args = [
            "-c:v", self.ffmpeg_encoder or cfg.encoder,
            "-b:v", f"{cfg.bitrate_kbps}k",
            "-g", str(cfg.gop),
            "-bf", str(cfg.max_b_frames),
        ]
        if self.preset_supported:
            args += ["-preset", cfg.preset]
        if self.tune:
            args += ["-tune", self.tune]
        for key, value in self.private_params:
            args += [key, value]
        args += ["-pix_fmt", cfg.pix_fmt]
        return args

    def stream_format(self) -> str:
        """输出基本流格式（决定 ffmpeg 的 ``-f``）。"""
        return {
            CodecId.H264.value: "h264",
            CodecId.H265.value: "hevc",
            CodecId.AV1.value: "obu",
        }.get(self.codec, "h264")

    # ---- 生命周期 ----
    def open(self) -> None:
        cfg = self.config
        width, height = self.stream.width, self.stream.height
        if width <= 0 or height <= 0:
            raise CodecOpenError(f"编码器缺少有效分辨率: {width}x{height}")

        fps = self.stream.framerate or 30.0
        input_pix_fmt = str(self.stream.metadata.get("pixel_format", "bgra"))

        cmd = [
            cfg.ffmpeg_path,
            "-hide_banner",
            "-loglevel", "error",
            "-nostdin",
            "-f", "rawvideo",
            "-pix_fmt", input_pix_fmt,
            "-video_size", f"{width}x{height}",
            "-framerate", f"{fps:g}",
            "-i", "pipe:0",
            "-an",
            *self.video_args(),
            "-f", self.stream_format(),
            "pipe:1",
        ]
        logger.debug("启动编码后端(%s): %s", self.name, " ".join(cmd))
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
            raise CodecOpenError(
                "无法启动 ffmpeg，请检查 encode.ffmpeg_path", detail=str(exc)
            ) from exc

        self._reader = threading.Thread(
            target=self._read_loop, name=f"{self.name}-out", daemon=True
        )
        self._reader.start()
        self._stderr_thread = threading.Thread(
            target=self._stderr_loop, name=f"{self.name}-err", daemon=True
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
        for thread in (self._reader, self._stderr_thread):
            if thread is not None:
                thread.join(timeout=2)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()
        self._proc = None
        self._opened = False

    # ---- 主路径 ----
    def encode(self, frame: Frame) -> list[Packet]:
        if self._proc is None or self._proc.stdin is None:
            raise EncodeError("编码器尚未打开")

        if frame.is_video and (
            frame.width != self.stream.width or frame.height != self.stream.height
        ):
            raise EncodeError(
                "帧尺寸与编码器配置不一致",
                detail=f"{frame.width}x{frame.height} != {self.stream.width}x{self.stream.height}",
            )

        with self._pts_lock:
            self._pending_pts.append(frame.rescaled_pts(self._timestamp))

        try:
            write_all(self._proc.stdin, bytes(frame.data))
        except (BrokenPipeError, OSError) as exc:
            raise EncodeError(
                "编码进程异常退出", detail=f"{exc}; stderr: {self._stderr_text()}"
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
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=2)

        output = self._drain()
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
        parser = AnnexBParser()
        splitter = AccessUnitSplitter()
        frame_duration = self._timestamp.ticks(1.0 / (self.stream.framerate or 30.0))

        try:
            while True:
                chunk = self._proc.stdout.read(READ_CHUNK)
                if not chunk:
                    break
                for nal in parser.push(chunk):
                    for access_unit in splitter.push(nal):
                        self._emit(access_unit, frame_duration)
            for nal in parser.flush():
                for access_unit in splitter.push(nal):
                    self._emit(access_unit, frame_duration)
            tail = splitter.take()
            if tail:
                self._emit(tail, frame_duration)
        except Exception as exc:  # 读线程内异常留给 flush 抛出
            self._reader_error = f"读取编码输出失败: {exc}"
            logger.exception("%s 输出读取失败", self.name)

    def _stderr_loop(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        for raw in iter(self._proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if line:
                self._stderr_tail.append(line)

    def _emit(self, access_unit: bytes, duration: int) -> None:
        with self._pts_lock:
            pts = self._pending_pts.popleft() if self._pending_pts else 0
        self._out.put(
            Packet(
                data=access_unit,
                codec=CodecId(self.codec),
                timebase=self._timestamp,
                pts=pts,
                dts=pts,
                duration=duration,
                stream_id=self.stream.stream_id,
            )
        )

    def _stderr_text(self) -> str:
        return " | ".join(self._stderr_tail) or "无 stderr 输出"


class FfmpegDecoderBase(BaseDecoder):
    """基于 ffmpeg 子进程的视频解码器基类。

    ``Packet(Annex-B) --stdin--> ffmpeg(--c:v 后端) --stdout(rawvideo)--> Frame``

    输出像素格式固定 BGRA。时间戳按「第 N 个输出帧对应第 N 个输入包」映射，
    因此要求码流没有 B 帧重排序。
    """

    #: ffmpeg 解码器名；None 表示让 ffmpeg 自动选择
    ffmpeg_decoder: str | None = None
    #: 输入基本流格式
    input_format: str = "h264"
    #: 额外输入参数（例如硬件解码需要的 ``-hwaccel``）
    input_args: tuple[str, ...] = ()
    #: 输出侧滤镜链，例如 VA-API 的 ``("hwdownload", "format=bgra")``：
    #: 硬件解码出来的是显存帧，必须显式下载到 CPU 才能交给 ``Frame``
    video_filters: tuple[str, ...] = ()

    def __init__(
        self,
        stream: StreamInfo,
        config: VideoEncodeConfig | None = None,
    ) -> None:
        super().__init__(stream)
        self.config = config or VideoEncodeConfig()
        self._proc: subprocess.Popen[bytes] | None = None
        self._frames: "queue.Queue[bytes]" = queue.Queue()
        self._pts_lock = threading.Lock()
        self._pending_pts: deque[int] = deque()
        self._reader: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stderr_tail: deque[str] = deque(maxlen=40)
        self._reader_error: str | None = None
        self._frame_size = 0
        self._timebase = Timebase.mpegts()

    def open(self) -> None:
        width, height = self.stream.width, self.stream.height
        if width <= 0 or height <= 0:
            raise CodecOpenError(f"解码器缺少有效分辨率: {width}x{height}")
        self._frame_size = width * height * 4
        self._timebase = Timebase.mpegts()

        decode_args = ["-c:v", self.ffmpeg_decoder] if self.ffmpeg_decoder else []
        filter_args = (
            ["-vf", ",".join(self.video_filters)] if self.video_filters else []
        )
        cmd = [
            self.config.ffmpeg_path,
            "-hide_banner",
            "-loglevel", "error",
            "-nostdin",
            *self.input_args,
            "-f", self.input_format,
            "-i", "pipe:0",
            "-an",
            *decode_args,
            *filter_args,
            "-f", "rawvideo",
            "-pix_fmt", "bgra",
            "pipe:1",
        ]
        logger.debug("启动解码后端(%s): %s", self.name, " ".join(cmd))
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
            raise CodecOpenError(
                "无法启动 ffmpeg，请检查 encode.ffmpeg_path", detail=str(exc)
            ) from exc

        self._reader = threading.Thread(
            target=self._read_loop, name=f"{self.name}-out", daemon=True
        )
        self._reader.start()
        self._stderr_thread = threading.Thread(
            target=self._stderr_loop, name=f"{self.name}-err", daemon=True
        )
        self._stderr_thread.start()
        self._opened = True

    def decode(self, packet: Packet) -> list[Frame]:
        if self._proc is None or self._proc.stdin is None:
            raise DecodeError("解码器尚未打开")

        with self._pts_lock:
            self._pending_pts.append(packet.pts or 0)

        try:
            write_all(self._proc.stdin, bytes(packet.data))
        except (BrokenPipeError, OSError) as exc:
            raise DecodeError(
                "解码进程异常退出", detail=f"{exc}; stderr: {self._stderr_text()}"
            ) from exc

        return self._drain(wait=DEFAULT_DRAIN_TIMEOUT)

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
            for thread in (self._reader, self._stderr_thread):
                if thread is not None:
                    thread.join(timeout=2)
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()
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
        with self._pts_lock:
            pts = self._pending_pts.popleft() if self._pending_pts else 0
        return Frame(
            data=data,
            timebase=self._timebase,
            pts=pts,
            width=self.stream.width,
            height=self.stream.height,
            pixel_format=PixelFormat.BGRA,
            domain=FrameDomain.CPU,
            stream_id=self.stream.stream_id,
        )

    def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        stdout = self._proc.stdout
        buf = bytearray()
        try:
            while True:
                chunk = stdout.read(READ_CHUNK)
                if not chunk:
                    break
                buf += chunk
                while len(buf) >= self._frame_size:
                    self._frames.put(bytes(buf[: self._frame_size]))
                    del buf[: self._frame_size]
        except Exception as exc:
            self._reader_error = f"读取解码输出失败: {exc}"
            logger.exception("%s 解码输出读取失败", self.name)

    def _stderr_loop(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        for raw in iter(self._proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if line:
                self._stderr_tail.append(line)

    def _stderr_text(self) -> str:
        return " | ".join(self._stderr_tail) or "无 stderr 输出"
