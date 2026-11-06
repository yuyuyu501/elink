"""显存帧下载到内存（D3D11/D3D12 -> CPU）。

进入滤镜链或 CPU 渲染前必须显式下载：GPU 纹理不能直接在 Python 里按字节访问。
下载后 ``Frame.domain`` 变回 ``CPU``，下游按普通帧处理。

需要 CPU 处理却忘记下载的典型症状是「拿到一个看似正常的 Frame，但 data 是
纹理句柄而不是像素」——所以本滤镜对非 CPU 域、没有 downloader 的情况直接报错。
"""

from __future__ import annotations

from collections.abc import Callable

from backend.errors.base import ElinkError, ErrorCode
from backend.logging.logger import get_logger
from backend.media.frame import Frame, FrameDomain
from backend.media.video.filter.pixel_format import BaseVideoFilter

logger = get_logger(__name__)

Downloader = Callable[[Frame], Frame]


class HardwareError(ElinkError):
    code = ErrorCode.NOT_SUPPORTED


class HwDownload(BaseVideoFilter):
    name = "hwdownload"

    def __init__(self, downloader: Downloader | None = None) -> None:
        self.downloader = downloader

    def process(self, frame: Frame) -> Frame:
        if frame.domain is FrameDomain.CPU:
            return frame
        if self.downloader is None:
            raise HardwareError(
                f"没有注入 {frame.domain.value} 下载实现，无法回到 CPU 帧",
                detail="请由 hwaccel 层提供 downloader，或让解码器直接输出系统内存帧",
            )
        downloaded = self.downloader(frame)
        logger.debug("下载 %s 帧到 CPU 完成", frame.domain.value)
        return downloaded
