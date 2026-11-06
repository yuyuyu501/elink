"""D3D12 零拷贝通路。

与 D3D11 版本规则一致，但多一条约束：D3D12 资源有「状态」，直通前必须
确认纹理处于可被下游读取的状态（例如 ``PIXEL_SHADER_RESOURCE``）；
状态不匹配时同样报错，由调用方插入一次显式的状态转换屏障。
"""

from __future__ import annotations

from backend.errors.base import ElinkError, ErrorCode
from backend.logging.logger import get_logger
from backend.media.frame import Frame, FrameDomain
from backend.media.video.hwaccel.d3d12.texture import (
    RESOURCE_STATE_COMMON,
    RESOURCE_STATE_PIXEL_SHADER_RESOURCE,
    D3D12Texture,
    TextureError,
)

logger = get_logger(__name__)


class ZeroCopyError(ElinkError):
    code = ErrorCode.NOT_SUPPORTED


class D3D12ZeroCopyPath:
    name = "d3d12_zerocopy"

    def __init__(
        self,
        require_domain: FrameDomain = FrameDomain.D3D12,
        readable_states: tuple[int, ...] = (
            RESOURCE_STATE_COMMON,
            RESOURCE_STATE_PIXEL_SHADER_RESOURCE,
        ),
    ) -> None:
        self.require_domain = require_domain
        self.readable_states = readable_states
        self._passed = 0

    def route(self, frame: Frame) -> Frame:
        if frame.domain is not self.require_domain:
            raise ZeroCopyError(
                f"帧不在 {self.require_domain.value} 域，无法走零拷贝",
                detail=f"实际 domain={frame.domain.value}",
            )
        try:
            texture = D3D12Texture.from_frame(frame)
        except TextureError as exc:
            raise ZeroCopyError("帧不是合法的 D3D12 纹理描述", detail=str(exc)) from exc

        if texture.desc.state not in self.readable_states:
            raise ZeroCopyError(
                "纹理状态不可读，需先做状态转换屏障",
                detail=f"state=0x{texture.desc.state:X}",
            )
        self._passed += 1
        return frame

    def can_route(self, frame: Frame) -> bool:
        return frame.domain is self.require_domain

    def stats(self) -> dict[str, int]:
        return {"passed": self._passed}
