"""Android 音频采集（``AudioRecord``）。

Python 侧通过 pyjnius 绑定 Java API。``AudioRecord`` 是阻塞读的：``read`` 把
PCM 填进 ByteBuffer 再拷出来，正好对上本项目的 ``read() -> Frame`` 契约。

依赖：``pip install pyjnius``，且必须运行在 Chaquopy/BeeWare/Kivy 这类带 JVM
的 Android 运行环境里；桌面环境导入会失败（由平台注册表跳过）。
"""

from __future__ import annotations

from backend.errors.base import ElinkError, ErrorCode
from backend.errors.permission import PermissionDeniedError
from backend.logging.logger import get_logger
from backend.media.frame import Frame
from backend.media.stream import MediaType
from backend.media.timebase import Timebase

logger = get_logger(__name__)

try:  # pragma: no cover - 仅 Android 运行环境可用
    from jnius import autoclass  # type: ignore
except ImportError:  # pragma: no cover
    autoclass = None

MEDIA_RECORD_AUDIO = 1
CHANNEL_IN_MONO = 0x0010
CHANNEL_IN_STEREO = 0x000C
ENCODING_PCM_16BIT = 2
RECORD_STATE_RECORDING = 3


class AudioCaptureError(ElinkError):
    code = ErrorCode.IO


class AudioRecordCapture:
    backend = "audiorecord"

    def __init__(
        self,
        sample_rate: int = 48_000,
        channels: int = 1,
        frame_ms: int = 20,
        timebase: Timebase | None = None,
    ) -> None:
        if autoclass is None:
            raise AudioCaptureError(
                "Android 音频采集需要 pyjnius（pip install pyjnius）"
            )
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_ms = frame_ms
        self.timebase = timebase or Timebase.microseconds()
        self._record = None
        self._buffer = None
        self._samples_read = 0

    @property
    def frame_bytes(self) -> int:
        return int(self.sample_rate * self.frame_ms / 1000) * self.channels * 2

    def open(self) -> None:
        audio_record = autoclass("android.media.AudioRecord")
        audio_format = autoclass("android.media.AudioFormat")
        byte_buffer = autoclass("java.nio.ByteBuffer")

        channel_mask = CHANNEL_IN_STEREO if self.channels >= 2 else CHANNEL_IN_MONO
        min_buffer = audio_record.getMinBufferSize(
            self.sample_rate, channel_mask, ENCODING_PCM_16BIT
        )
        if min_buffer <= 0:
            raise AudioCaptureError(
                "AudioRecord.getMinBufferSize 返回非法值", detail=str(min_buffer)
            )

        buffer_size = max(min_buffer, self.frame_bytes * 4)
        self._record = audio_record(
            MEDIA_RECORD_AUDIO,
            self.sample_rate,
            channel_mask,
            ENCODING_PCM_16BIT,
            buffer_size,
        )
        if self._record.getState() != RECORD_STATE_RECORDING:
            raise PermissionDeniedError(
                "AudioRecord 初始化失败，通常是缺少 RECORD_AUDIO 权限"
            )

        self._buffer = byte_buffer.allocateDirect(self.frame_bytes)
        self._buffer.order(autoclass("java.nio.ByteOrder").nativeOrder())
        self._record.startRecording()
        if self._record.getRecordingState() != RECORD_STATE_RECORDING:
            raise AudioCaptureError("AudioRecord.startRecording 失败")
        del audio_format
        self._samples_read = 0
        logger.info("Android 音频采集就绪: %dHz/%dch", self.sample_rate, self.channels)

    def close(self) -> None:
        if self._record is not None:
            try:
                self._record.stop()
            finally:
                self._record.release()
                self._record = None

    def read(self) -> Frame:
        if self._record is None or self._buffer is None:
            raise AudioCaptureError("采集器未打开")

        self._buffer.rewind()
        read_bytes = self._record.read(self._buffer, self.frame_bytes)
        if read_bytes <= 0:
            raise AudioCaptureError("AudioRecord.read 返回错误", detail=str(read_bytes))

        payload = bytearray(read_bytes)
        self._buffer.rewind()
        self._buffer.get(payload)

        samples = read_bytes // (self.channels * 2)
        pts = self.timebase.ticks(self._samples_read / self.sample_rate)
        self._samples_read += samples
        return Frame(
            data=bytes(payload),
            timebase=self.timebase,
            pts=pts,
            media_type=MediaType.AUDIO,
            sample_rate=self.sample_rate,
            channels=self.channels,
            sample_format="s16",
            samples=samples,
        )

    def __enter__(self) -> "AudioRecordCapture":
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
