"""帧抽象：未编码的原始画面 / 音频数据。

采集与滤镜的输出、解码与渲染的输入都是 :class:`Frame`；它只描述「一块原始
数据 + 时间戳 + 内存域」，不携带任何编解码逻辑。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from backend.media.stream import MediaType
from backend.media.timebase import Timebase


class PixelFormat(str, enum.Enum):
    BGRA = "bgra"
    RGBA = "rgba"
    BGR = "bgr"
    RGB = "rgb"
    YUV420P = "yuv420p"
    NV12 = "nv12"

    @property
    def bytes_per_pixel(self) -> float:
        # 平面格式按 4:2:0 计算平均字节数
        return {
            PixelFormat.BGRA: 4.0,
            PixelFormat.RGBA: 4.0,
            PixelFormat.BGR: 3.0,
            PixelFormat.RGB: 3.0,
            PixelFormat.YUV420P: 1.5,
            PixelFormat.NV12: 1.5,
        }[self]


class FrameDomain(str, enum.Enum):
    """帧数据所在内存域；跨域必须显式 upload/download。"""

    CPU = "cpu"
    D3D11 = "d3d11"
    D3D12 = "d3d12"


@dataclass
class Frame:
    """一帧原始数据。"""

    data: bytes | bytearray | memoryview
    timebase: Timebase = field(default_factory=Timebase.mpegts)
    pts: int = 0
    media_type: MediaType = MediaType.VIDEO
    duration: int | None = None

    # 视频字段
    width: int = 0
    height: int = 0
    pixel_format: PixelFormat | None = PixelFormat.BGRA
    linesize: int = 0

    # 音频字段
    sample_rate: int = 0
    channels: int = 0
    sample_format: str = ""
    samples: int = 0

    domain: FrameDomain = FrameDomain.CPU
    stream_id: int = 0

    def __post_init__(self) -> None:
        if self.media_type is MediaType.VIDEO:
            if self.width <= 0 or self.height <= 0:
                raise ValueError(f"视频帧缺少有效尺寸: {self.width}x{self.height}")
            if not self.linesize:
                self.linesize = int(self.width * (self.pixel_format or PixelFormat.BGRA).bytes_per_pixel)

    @property
    def is_video(self) -> bool:
        return self.media_type is MediaType.VIDEO

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def seconds(self) -> float:
        return self.timebase.seconds(self.pts)

    @property
    def expected_size(self) -> int:
        """按当前分辨率/像素格式推算的字节数（用于校验采集输出）。"""
        if not self.is_video or self.pixel_format is None:
            return 0
        return int(self.linesize * self.height)

    def rescaled_pts(self, target: Timebase) -> int:
        return self.timebase.rescale(self.pts, target)

    def with_data(self, data: bytes, **changes) -> "Frame":
        """基于当前帧生成新帧（滤镜输出用），默认沿用时间戳。"""
        values = {
            "data": data,
            "timebase": self.timebase,
            "pts": self.pts,
            "media_type": self.media_type,
            "duration": self.duration,
            "width": self.width,
            "height": self.height,
            "pixel_format": self.pixel_format,
            "linesize": self.linesize,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "sample_format": self.sample_format,
            "samples": self.samples,
            "domain": self.domain,
            "stream_id": self.stream_id,
        }
        values.update(changes)
        if "linesize" in changes and not changes["linesize"]:
            values["linesize"] = 0
        return Frame(**values)
