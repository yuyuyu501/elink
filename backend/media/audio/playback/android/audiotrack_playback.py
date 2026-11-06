"""Android 音频播放（``AudioTrack``）。

``AudioTrack.write`` 是阻塞的（默认 ``WRITE_BLOCKING``），写满内部缓冲会阻塞，
形成天然的播放背压：音频链路因此不需要丢帧，只靠限速。
"""

from __future__ import annotations

from backend.errors.base import ElinkError, ErrorCode
from backend.logging.logger import get_logger
from backend.media.frame import Frame

logger = get_logger(__name__)

try:  # pragma: no cover - 仅 Android 运行环境可用
    from jnius import autoclass  # type: ignore
except ImportError:  # pragma: no cover
    autoclass = None

STREAM_MUSIC = 3
CHANNEL_OUT_STEREO = 0x000C
CHANNEL_OUT_MONO = 0x0004
ENCODING_PCM_16BIT = 2
WRITE_BLOCKING = 0


class AudioPlaybackError(ElinkError):
    code = ErrorCode.IO


class AudioTrackPlayback:
    backend = "audiotrack"

    def __init__(self, sample_rate: int = 48_000, channels: int = 2) -> None:
        if autoclass is None:
            raise AudioPlaybackError(
                "Android 音频播放需要 pyjnius（pip install pyjnius）"
            )
        self.sample_rate = sample_rate
        self.channels = channels
        self._track = None

    def open(self) -> None:
        audio_track_cls = autoclass("android.media.AudioTrack")
        audio_attributes = autoclass("android.media.AudioAttributes")

        channel_mask = CHANNEL_OUT_STEREO if self.channels >= 2 else CHANNEL_OUT_MONO
        min_buffer = audio_track_cls.getMinBufferSize(
            self.sample_rate, channel_mask, ENCODING_PCM_16BIT
        )
        if min_buffer <= 0:
            raise AudioPlaybackError(
                "AudioTrack.getMinBufferSize 返回非法值", detail=str(min_buffer)
            )

        attributes = audio_attributes.Builder().setUsage(1).build()  # USAGE_MEDIA
        builder = audio_track_cls.Builder()
        builder.setAudioAttributes(attributes)
        builder.setAudioFormat(
            autoclass("android.media.AudioFormat").Builder()
            .setEncoding(ENCODING_PCM_16BIT)
            .setSampleRate(self.sample_rate)
            .setChannelMask(channel_mask)
            .build()
        )
        builder.setBufferSizeInBytes(min_buffer * 2)
        builder.setTransferMode(1)  # MODE_STREAM
        self._track = builder.build()
        self._track.play()
        logger.info("Android 音频播放就绪: %dHz/%dch", self.sample_rate, self.channels)

    def close(self) -> None:
        if self._track is not None:
            try:
                self._track.stop()
            finally:
                self._track.release()
                self._track = None

    def write(self, frame: Frame) -> None:
        if self._track is None:
            raise AudioPlaybackError("播放器未打开")
        if (frame.sample_format or "s16") != "s16":
            raise AudioPlaybackError("播放器只接受 s16 PCM")

        payload = bytes(frame.data)
        buffer = bytearray(payload)
        written = self._track.write(buffer, 0, len(buffer), WRITE_BLOCKING)
        if written < 0:
            raise AudioPlaybackError("AudioTrack.write 失败", detail=str(written))

    def __enter__(self) -> "AudioTrackPlayback":
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
