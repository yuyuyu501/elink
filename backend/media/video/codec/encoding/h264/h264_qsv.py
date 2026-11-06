"""Intel Quick Sync 硬件编码（``-c:v h264_qsv``）。

QSV 在 Linux 上需要 VA-API 后端与已授权的渲染节点；探测只看 ffmpeg 是否编进
了该编码器，实际可用性由打开阶段的错误决定（失败自动回退）。
"""

from __future__ import annotations

from backend.media.stream import CodecId
from backend.media.video.codec.base import FfmpegEncoderBase


class H264QsvEncoder(FfmpegEncoderBase):
    name = "h264_qsv"
    codec = CodecId.H264.value
    hardware = True

    ffmpeg_encoder = "h264_qsv"
    preset_supported = True
    private_params = (("-look_ahead", "0"),)

    @classmethod
    def available(cls, ffmpeg_path: str = "ffmpeg") -> bool:
        from backend.media.video.codec.probe import probe_ffmpeg

        return probe_ffmpeg(ffmpeg_path).has_encoder("h264_qsv")
