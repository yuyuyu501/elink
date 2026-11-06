"""Linux VA-API 硬件编码（``-c:v h264_vaapi``）。

VA-API 编码器只接受显存帧，CPU 帧必须先上传：``-vf format=nv12,hwupload``，
因此比其它后端多一步内存域转换。这里的输入仍然是 CPU 的 BGRA 帧，
由 ffmpeg 侧完成上传（管线层不做隐式跨域拷贝）。
"""

from __future__ import annotations

from backend.media.stream import CodecId
from backend.media.video.codec.base import FfmpegEncoderBase


class H264VaapiEncoder(FfmpegEncoderBase):
    name = "h264_vaapi"
    codec = CodecId.H264.value
    hardware = True

    ffmpeg_encoder = "h264_vaapi"
    preset_supported = False
    private_params = (("-vf", "format=nv12,hwupload"),)

    @classmethod
    def available(cls, ffmpeg_path: str = "ffmpeg") -> bool:
        from backend.media.video.codec.probe import probe_ffmpeg

        return probe_ffmpeg(ffmpeg_path).has_encoder("h264_vaapi")
