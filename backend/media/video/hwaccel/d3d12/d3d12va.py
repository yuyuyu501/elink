"""D3D12VA 硬件解码（``-hwaccel d3d12va``）。

D3D12VA 是 Windows 上更新的视频解码加速接口（与 D3D11VA 并存，Win10 1809+
支持）。作为加速层，它同样不改编码格式，只把解码放到 GPU 上。
"""

from __future__ import annotations

import sys

from backend.media.stream import CodecId
from backend.media.video.codec.base import FfmpegDecoderBase

SUPPORTED_CODECS = {
    CodecId.H264: "h264",
    CodecId.H265: "hevc",
    CodecId.AV1: "av1",
}


class D3D12VADecoder(FfmpegDecoderBase):
    name = "d3d12va"
    hardware = True

    input_args = ("-hwaccel", "d3d12va")

    def __init__(self, stream, config=None, codec: CodecId | None = None) -> None:
        super().__init__(stream, config)
        target = codec or stream.codec
        if target not in SUPPORTED_CODECS:
            raise ValueError(f"D3D12VA 不支持该编码: {target}")
        self.codec = target.value
        self.input_format = SUPPORTED_CODECS[target]

    @classmethod
    def available(cls, ffmpeg_path: str = "ffmpeg") -> bool:
        if not sys.platform.startswith("win"):
            return False
        from backend.media.video.codec.probe import probe_ffmpeg

        return probe_ffmpeg(ffmpeg_path).available
