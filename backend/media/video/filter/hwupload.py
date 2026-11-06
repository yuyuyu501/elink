"""内存帧上传到显存（CPU -> D3D11/D3D12）。

按项目约定，内存域转换必须**显式**发生，禁止隐式跨域拷贝。所以本滤镜不做
「偷偷上传」，而是要求注入一个真实的 uploader（由 hwaccel 层提供）；
拿不到 uploader 时明确报错，避免让上层以为帧已经在显存里。

``Frame.domain`` 就是这套约定的载体：上传成功后 domain 变成目标域，
下游 ``zerocopy`` 或硬件编码器据此决定能不能直通。
"""

from __future__ import annotations

from collections.abc import Callable

from backend.errors.base import ElinkError, ErrorCode
from backend.logging.logger import get_logger
from backend.media.frame import Frame, FrameDomain
from backend.media.video.filter.pixel_format import BaseVideoFilter

logger = get_logger(__name__)

Uploader = Callable[[Frame, FrameDomain], Frame]


class HardwareError(ElinkError):
    code = ErrorCode.NOT_SUPPORTED


class HwUpload(BaseVideoFilter):
    name = "hwupload"

    def __init__(
        self,
        target: FrameDomain = FrameDomain.D3D11,
        uploader: Uploader | None = None,
    ) -> None:
        self.target = target
        self.uploader = uploader

    def process(self, frame: Frame) -> Frame:
        if frame.domain is self.target:
            return frame
        if self.uploader is None:
            raise HardwareError(
                f"没有注入 {self.target.value} 上传实现，拒绝隐式跨域拷贝",
                detail="请由 hwaccel 层提供 uploader，或改用 CPU 渲染/编码路径",
            )
        uploaded = self.uploader(frame, self.target)
        logger.debug("上传帧到 %s 完成", self.target.value)
        return uploaded
