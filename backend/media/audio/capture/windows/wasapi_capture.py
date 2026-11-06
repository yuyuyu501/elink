"""Windows 音频采集。

文件名沿用规划里的 WASAPI，但当前实现走 **WinMM 的 WAVE 输入接口**
（``waveInOpen``）：纯 ctypes、无需 COM 初始化，任何交互会话都能录；WASAPI 的
``IMMDeviceEnumerator`` / ``IAudioCaptureClient`` 通路尚未接入。对外契约
（``open`` / ``read`` -> ``Frame`` / ``close``）保持不变，将来换实现不影响上层。

输出统一为 16bit 小端 PCM，每帧固定 ``frame_ms`` 毫秒，便于下游按帧计时。
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

from backend.errors.base import ElinkError, ErrorCode
from backend.logging.logger import get_logger
from backend.media.frame import Frame
from backend.media.stream import MediaType
from backend.media.timebase import Timebase

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


winmm.waveInOpen.argtypes = [
    ctypes.POINTER(ctypes.c_void_p),
    wintypes.UINT,
    ctypes.POINTER(_WaveFormatEx),
    ctypes.c_void_p,
    ctypes.c_void_p,
    wintypes.DWORD,
]
winmm.waveInPrepareHeader.argtypes = [ctypes.c_void_p, ctypes.POINTER(_WaveHdr), wintypes.UINT]
winmm.waveInAddBuffer.argtypes = [ctypes.c_void_p, ctypes.POINTER(_WaveHdr), wintypes.UINT]
winmm.waveInStart.argtypes = [ctypes.c_void_p]
winmm.waveInStop.argtypes = [ctypes.c_void_p]
winmm.waveInReset.argtypes = [ctypes.c_void_p]
winmm.waveInUnprepareHeader.argtypes = [
    ctypes.c_void_p,
    ctypes.POINTER(_WaveHdr),
    wintypes.UINT,
]
winmm.waveInClose.argtypes = [ctypes.c_void_p]


class AudioCaptureError(ElinkError):
    code = ErrorCode.IO


class WasapiCapture:
    """麦克风/环回采集：``read()`` 产出一帧 16bit PCM。"""

    backend = "winmm"

    def __init__(
        self,
        sample_rate: int = 48_000,
        channels: int = 2,
        frame_ms: int = 20,
        timebase: Timebase | None = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_ms = frame_ms
        self.timebase = timebase or Timebase.microseconds()
        self._handle: ctypes.c_void_p | None = None
        self._headers: list[tuple[ctypes.Array, _WaveHdr]] = []
        self._start_time = 0.0
        self._samples_read = 0

    @property
    def frame_bytes(self) -> int:
        return int(self.sample_rate * self.frame_ms / 1000) * self.channels * 2

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
        result = winmm.waveInOpen(
            ctypes.byref(handle),
            WAVE_MAPPER,
            ctypes.byref(format_ex),
            None,
            None,
            CALLBACK_NULL,
        )
        if result != MMSYSERR_NOERROR:
            raise AudioCaptureError(
                "waveInOpen 失败（没有可用录音设备或缺少麦克风权限）", detail=f"code={result}"
            )
        self._handle = handle

        for _ in range(4):
            buffer = ctypes.create_string_buffer(self.frame_bytes)
            header = _WaveHdr()
            header.lpData = ctypes.cast(buffer, ctypes.c_void_p)
            header.dwBufferLength = self.frame_bytes
            winmm.waveInPrepareHeader(handle, ctypes.byref(header), ctypes.sizeof(_WaveHdr))
            winmm.waveInAddBuffer(handle, ctypes.byref(header), ctypes.sizeof(_WaveHdr))
            self._headers.append((buffer, header))

        if winmm.waveInStart(handle) != MMSYSERR_NOERROR:
            self.close()
            raise AudioCaptureError("waveInStart 失败")

        self._start_time = time.perf_counter()
        self._samples_read = 0
        logger.info(
            "音频采集就绪: %dHz/%dch 每帧 %dms 后端=%s",
            self.sample_rate,
            self.channels,
            self.frame_ms,
            self.backend,
        )

    def close(self) -> None:
        handle = self._handle
        if handle is None:
            return
        winmm.waveInStop(handle)
        winmm.waveInReset(handle)
        for _, header in self._headers:
            winmm.waveInUnprepareHeader(handle, ctypes.byref(header), ctypes.sizeof(_WaveHdr))
        winmm.waveInClose(handle)
        self._headers.clear()
        self._handle = None

    def read(self, timeout_s: float = 1.0) -> Frame:
        if self._handle is None:
            raise AudioCaptureError("采集器未打开")

        deadline = time.perf_counter() + timeout_s
        while True:
            for index, (buffer, header) in enumerate(self._headers):
                if header.dwFlags & WHDR_DONE or header.dwBytesRecorded:
                    data = buffer.raw[: header.dwBytesRecorded]
                    samples = header.dwBytesRecorded // (self.channels * 2)
                    header.dwFlags = 0
                    header.dwBytesRecorded = 0
                    winmm.waveInAddBuffer(
                        self._handle, ctypes.byref(header), ctypes.sizeof(_WaveHdr)
                    )
                    self._headers[index] = (buffer, header)
                    if not data:
                        continue
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
            if time.perf_counter() >= deadline:
                raise AudioCaptureError("音频采集超时")
            time.sleep(0.002)

    def __enter__(self) -> "WasapiCapture":
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
