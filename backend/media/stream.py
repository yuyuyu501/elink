"""流抽象：描述一路媒体的编码格式与时间基。"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from backend.media.timebase import Timebase


class MediaType(str, enum.Enum):
    VIDEO = "video"
    AUDIO = "audio"


class CodecId(str, enum.Enum):
    H264 = "h264"
    H265 = "hevc"
    AV1 = "av1"
    AAC = "aac"
    OPUS = "opus"
    FLAC = "flac"

    @property
    def media_type(self) -> MediaType:
        return MediaType.AUDIO if self in _AUDIO_CODECS else MediaType.VIDEO


_AUDIO_CODECS = frozenset({CodecId.AAC, CodecId.OPUS, CodecId.FLAC})


@dataclass
class StreamInfo:
    """一路流的描述信息。

    ``stream_id`` 在本端内唯一；``extra_data`` 存放解码所需参数集
    （H.264/HEVC 的 Annex-B 参数集、AV1 的 sequence header OBU 等）。
    """

    stream_id: int = 0
    media_type: MediaType = MediaType.VIDEO
    codec: CodecId = CodecId.H264
    timebase: Timebase = field(default_factory=Timebase.mpegts)
    width: int = 0
    height: int = 0
    framerate: float = 0.0
    sample_rate: int = 0
    channels: int = 0
    extra_data: bytes | None = None
    metadata: dict = field(default_factory=dict)

    @property
    def is_video(self) -> bool:
        return self.media_type is MediaType.VIDEO

    @property
    def frame_duration(self) -> int:
        """一帧在本地时间基下的 tick 数（仅视频、已知帧率时有效）。"""
        if not self.is_video or self.framerate <= 0:
            return 0
        return self.timebase.ticks(1.0 / self.framerate)

    def copy_with(self, **changes) -> "StreamInfo":
        data = {
            "stream_id": self.stream_id,
            "media_type": self.media_type,
            "codec": self.codec,
            "timebase": self.timebase,
            "width": self.width,
            "height": self.height,
            "framerate": self.framerate,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "extra_data": self.extra_data,
            "metadata": dict(self.metadata),
        }
        data.update(changes)
        return StreamInfo(**data)
