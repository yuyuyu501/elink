"""配置结构与校验。

配置是纯数据，不做任何 I/O；校验集中在 :meth:`AppConfig.validate`，
避免各模块各自校验导致规则分散。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.errors.base import ElinkError, ErrorCode


class ConfigError(ElinkError):
    code = ErrorCode.INVALID_ARGUMENT


@dataclass
class CaptureConfig:
    """屏幕采集。``region`` 为 (x, y, width, height)，None 表示主显示器全屏。"""

    backend: str = "auto"
    region: tuple[int, int, int, int] | None = None
    fps: float = 30.0

    def clamped_region(self) -> tuple[int, int, int, int] | None:
        """H.264 要求偶数分辨率，这里统一向下取偶。"""
        if self.region is None:
            return None
        x, y, w, h = self.region
        return (x, y, max(2, w - w % 2), max(2, h - h % 2))


@dataclass
class VideoEncodeConfig:
    codec: str = "h264"
    encoder: str = "libx264"
    preset: str = "veryfast"
    bitrate_kbps: int = 4000
    gop: int = 60
    pix_fmt: str = "yuv420p"
    max_b_frames: int = 0
    ffmpeg_path: str = "ffmpeg"


@dataclass
class MuxConfig:
    """封装参数（MPEG-TS）。"""

    format: str = "mpegts"
    program_number: int = 1
    transport_stream_id: int = 1
    pmt_pid: int = 0x1000
    pcr_interval_ms: int = 40


@dataclass
class TransportConfig:
    """运输层。``kind`` 为 ``file`` 时退回本地闭环，验证抽象而不引入网络。"""

    kind: str = "file"
    path: str = "out/demo.ts"
    srt_url: str = "srt://127.0.0.1:9000"
    srt_mode: str = "caller"
    srt_latency_ms: int = 120


@dataclass
class PipelineConfig:
    queue_size: int = 4
    drop_oldest: bool = True


@dataclass
class AppConfig:
    mode: str = "loopback"
    duration_s: float = 3.0
    max_frames: int = 0
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    encode: VideoEncodeConfig = field(default_factory=VideoEncodeConfig)
    mux: MuxConfig = field(default_factory=MuxConfig)
    transport: TransportConfig = field(default_factory=TransportConfig)
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
    frames_dir: str = "out/frames"
    log_level: str = "INFO"

    def validate(self) -> None:
        if self.mode not in {"loopback", "send", "receive"}:
            raise ConfigError(f"未知运行模式: {self.mode}", detail=self.mode)
        if self.capture.fps <= 0:
            raise ConfigError("capture.fps 必须大于 0")
        if self.encode.bitrate_kbps <= 0:
            raise ConfigError("encode.bitrate_kbps 必须大于 0")
        if self.encode.gop <= 0:
            raise ConfigError("encode.gop 必须大于 0")
        if self.pipeline.queue_size <= 0:
            raise ConfigError("pipeline.queue_size 必须大于 0")
        if self.duration_s < 0:
            raise ConfigError("duration_s 不能为负")

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict

        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppConfig":
        def build(default: Any, values: dict[str, Any]) -> Any:
            allowed = {f for f in default.__dataclass_fields__}
            unknown = set(values) - allowed
            if unknown:
                raise ConfigError(
                    f"{type(default).__name__} 存在未知字段: {sorted(unknown)}"
                )
            if "region" in values and values["region"] is not None:
                values["region"] = tuple(values["region"])
            return type(default)(**values)

        config = cls()
        for key, value in data.items():
            current = getattr(config, key)
            if hasattr(current, "__dataclass_fields__") and isinstance(value, dict):
                setattr(config, key, build(current, value))
            elif hasattr(cls, key):
                setattr(config, key, value)
            else:
                raise ConfigError(f"AppConfig 存在未知字段: {key}")
        config.validate()
        return config
