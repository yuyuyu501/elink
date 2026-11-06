"""D3D11 零拷贝通路。

零拷贝的判定条件只有一条：**上下游的 ``Frame.domain`` 一致**。只要解码输出
（``D3D11``）、滤镜链、编码器输入都在同一域，就完全跳过 CPU 往返。

本模块提供的是「检查 + 直通」的骨架：

- 域一致 → 原帧返回（0 拷贝）；
- 域不一致 → 明确报错，提示插入 ``hwupload``/``hwdownload``，
  而不是偷偷做一次纹理下载（隐式跨域拷贝是本项目明令禁止的）。
"""

from __future__ import annotations

from backend.errors.base import ElinkError, ErrorCode
from backend.logging.logger import get_logger
from backend.media.frame import Frame, FrameDomain

logger = get_logger(__name__)


class ZeroCopyError(ElinkError):
    code = ErrorCode.NOT_SUPPORTED


class D3D11ZeroCopyPath:
    """把 D3D11 域的帧从解码器直送渲染器/编码器。"""

    name = "d3d11_zerocopy"

    def __init__(self, require_domain: FrameDomain = FrameDomain.D3D11) -> None:
        self.require_domain = require_domain
        self._passed = 0

    @property
    def passed_frames(self) -> int:
        return self._passed

    def route(self, frame: Frame) -> Frame:
        if frame.domain is not self.require_domain:
            raise ZeroCopyError(
                f"帧不在 {self.require_domain.value} 域，无法走零拷贝",
                detail=f"实际 domain={frame.domain.value}；请显式插入 hwupload/hwdownload",
            )
        self._passed += 1
        return frame

    def can_route(self, frame: Frame) -> bool:
        return frame.domain is self.require_domain

    def stats(self) -> dict[str, int]:
        return {"passed": self._passed}
