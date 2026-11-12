"""NVIDIA NVENC 硬件编码（``-c:v h264_nvenc``）。

可用性必须在运行时探测：驱动缺失或显卡不支持时 ffmpeg 会在打开阶段失败，
由注册表回退到 ``libx264``。
"""

from __future__ import annotations

from backend.media.stream import CodecId
from backend.media.video.codec.base import FfmpegEncoderBase


class H264NvencEncoder(FfmpegEncoderBase):
    name = "h264_nvenc"
    codec = CodecId.H264.value
    hardware = True

    ffmpeg_encoder = "h264_nvenc"
    preset_supported = False
    #: NVENC 的低延迟开关；preset 走 p1~p7，这里用质量参数保持通用
    private_params = (("-preset", "p1"), ("-tune", "ull"), ("-rc", "cbr"),
                      ("-rc-lookahead", "0"), ("-zerolatency", "1"))

    def video_args(self) -> list[str]:
        return [*super().video_args(), "-bufsize", f"{max(1, self.config.bitrate_kbps // 10)}k"]

    @classmethod
    def available(cls, ffmpeg_path: str = "ffmpeg") -> bool:
        from backend.media.video.codec.probe import probe_ffmpeg

        return probe_ffmpeg(ffmpeg_path).has_encoder("h264_nvenc")
