"""D3D11VA 硬件解码（``-hwaccel d3d11va``）。

这是**加速层，不是编解码器**：它不改编码格式，只是把解码工作交给 GPU。
ffmpeg 在 ``-hwaccel d3d11va`` 下默认把结果下载回系统内存，因此本后端直接
输出 CPU 帧，可以无缝接现有的 ``Frame`` 契约。

如果要走零拷贝（帧留在显存），需要配合 ``zerocopy.py`` 与 GPU 渲染端，
帧的 ``domain`` 会保持 ``D3D11``。
"""

from __future__ import annotations

from backend.media.stream import CodecId
from backend.media.video.codec.base import FfmpegDecoderBase

SUPPORTED_CODECS = {
    CodecId.H264: "h264",
    CodecId.H265: "hevc",
    CodecId.AV1: "av1",
}


class D3D11VADecoder(FfmpegDecoderBase):
    """用 D3D11VA 加速解码，输出系统内存 BGRA 帧。"""

    name = "d3d11va"
    hardware = True

    input_args = ("-hwaccel", "d3d11va")

    def __init__(self, stream, config=None, codec: CodecId | None = None) -> None:
        super().__init__(stream, config)
        target = codec or stream.codec
        if target not in SUPPORTED_CODECS:
            raise ValueError(f"D3D11VA 不支持该编码: {target}")
        self.codec = target.value
        self.input_format = SUPPORTED_CODECS[target]

    @classmethod
    def available(cls, ffmpeg_path: str = "ffmpeg") -> bool:
        import sys

        if not sys.platform.startswith("win"):
            return False
        from backend.media.video.codec.probe import probe_ffmpeg

        return probe_ffmpeg(ffmpeg_path).available
