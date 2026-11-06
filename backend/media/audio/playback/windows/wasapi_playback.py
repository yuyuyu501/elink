"""Windows 音频播放。

与采集侧对称：文件名沿用 WASAPI，实现走 **WinMM 的 WAVE 输出接口**
（``waveOutWrite``）。每次写入一个缓冲区并等待它播完，天然形成背压——
播放线程不会被内存里的队列撑爆，这正是「音频不丢帧、靠背压限速」的落点。
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

from backend.errors.base import ElinkError, ErrorCode
from backend.logging.logger import get_logger
from backend.media.frame import Frame

logger = get_logger(__name__)

WAVE_FORMAT_PCM = 1
CALLBACK_NULL = 0x00000000
WHDR_DONE = 0x00000001
WAVE_MAPPER = -1
MMSYSERR_NOERROR = 0

winmm = ctypes.WinDLL("winmm", use_last_error=True)


class _WaveFormatEx(ctypes.Structure):
    _fields_ = [
        ("wFormatTag", wintypes.WORD),
        ("nChannels", wintypes.WORD),
        ("nSamplesPerSec", wintypes.DWORD),
        ("nAvgBytesPerSec", wintypes.DWORD),
        ("nBlockAlign", wintypes.WORD),
        ("wBitsPerSample", wintypes.WORD),
        ("cbSize", wintypes.WORD),
    ]


class _WaveHdr(ctypes.Structure):
    _fields_ = [
        ("lpData", ctypes.c_void_p),
        ("dwBufferLength", wintypes.DWORD),
        ("dwBytesRecorded", wintypes.DWORD),
        ("dwUser", ctypes.c_void_p),
        ("dwFlags", wintypes.DWORD),
        ("dwLoops", wintypes.DWORD),
        ("lpNext", ctypes.c_void_p),
        ("reserved", ctypes.c_void_p),
    ]


winmm.waveOutOpen.argtypes = [
    ctypes.POINTER(ctypes.c_void_p),
    wintypes.UINT,
    ctypes.POINTER(_WaveFormatEx),
    ctypes.c_void_p,
    ctypes.c_void_p,
    wintypes.DWORD,
]
winmm.waveOutPrepareHeader.argtypes = [
    ctypes.c_void_p,
    ctypes.POINTER(_WaveHdr),
    wintypes.UINT,
]
winmm.waveOutWrite.argtypes = [ctypes.c_void_p, ctypes.POINTER(_WaveHdr), wintypes.UINT]
winmm.waveOutUnprepareHeader.argtypes = [
    ctypes.c_void_p,
    ctypes.POINTER(_WaveHdr),
    wintypes.UINT,
]
winmm.waveOutReset.argtypes = [ctypes.c_void_p]
winmm.waveOutClose.argtypes = [ctypes.c_void_p]


class AudioPlaybackError(ElinkError):
    code = ErrorCode.IO


class WasapiPlayback:
    """扬声器播放：``write(frame)`` 阻塞到这一帧播完（背压）。"""

    backend = "winmm"

    def __init__(self, sample_rate: int = 48_000, channels: int = 2) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self._handle: ctypes.c_void_p | None = None

    def open(self) -> None:
        format_ex = _WaveFormatEx(
            wFormatTag=WAVE_FORMAT_PCM,
            nChannels=self.channels,
            nSamplesPerSec=self.sample_rate,
            nAvgBytesPerSec=self.sample_rate * self.channels * 2,
            nBlockAlign=self.channels * 2,
            wBitsPerSample=16,
            cbSize=0,
        )
        handle = ctypes.c_void_p()
        result = winmm.waveOutOpen(
            ctypes.byref(handle),
            WAVE_MAPPER,
            ctypes.byref(format_ex),
            None,
            None,
            CALLBACK_NULL,
        )
        if result != MMSYSERR_NOERROR:
            raise AudioPlaybackError("waveOutOpen 失败（没有可用输出设备）", detail=f"code={result}")
        self._handle = handle
        logger.info("音频播放就绪: %dHz/%dch 后端=%s", self.sample_rate, self.channels, self.backend)

    def close(self) -> None:
        handle = self._handle
        if handle is None:
            return
        winmm.waveOutReset(handle)
        winmm.waveOutClose(handle)
        self._handle = None

    def write(self, frame: Frame) -> None:
        if self._handle is None:
            raise AudioPlaybackError("播放器未打开")
        if (frame.sample_format or "s16") != "s16":
            raise AudioPlaybackError("播放器只接受 s16 PCM")
        if frame.sample_rate != self.sample_rate or frame.channels != self.channels:
            raise AudioPlaybackError(
                "帧参数与播放器不一致",
                detail=(
                    f"{frame.sample_rate}Hz/{frame.channels}ch vs "
                    f"{self.sample_rate}Hz/{self.channels}ch"
                ),
            )

        data = bytes(frame.data)
        buffer = ctypes.create_string_buffer(data, len(data))
        header = _WaveHdr()
        header.lpData = ctypes.cast(buffer, ctypes.c_void_p)
        header.dwBufferLength = len(data)

        winmm.waveOutPrepareHeader(self._handle, ctypes.byref(header), ctypes.sizeof(_WaveHdr))
        if winmm.waveOutWrite(self._handle, ctypes.byref(header), ctypes.sizeof(_WaveHdr)) != MMSYSERR_NOERROR:
            raise AudioPlaybackError("waveOutWrite 失败")

        deadline = time.perf_counter() + max(2.0, len(data) / (self.sample_rate * 2) * 4)
        while not (header.dwFlags & WHDR_DONE):
            if time.perf_counter() > deadline:
                raise AudioPlaybackError("等缓冲区播完超时")
            time.sleep(0.002)

        winmm.waveOutUnprepareHeader(
            self._handle, ctypes.byref(header), ctypes.sizeof(_WaveHdr)
        )

    def __enter__(self) -> "WasapiPlayback":
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
