"""D3D12 纹理封装。

与 D3D11 版本同为**描述符**而非真实资源：D3D12 的资源还必须带「状态」
（``D3D12_RESOURCE_STATE_*``），状态错了会触发 GPU 校验错误，所以这里把
state 一并记在描述符里，供 zerocopy 通路检查。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from backend.errors.base import ElinkError, ErrorCode
from backend.media.frame import Frame, FrameDomain, PixelFormat

DXGI_FORMAT_B8G8R8A8_UNORM = 87
DXGI_FORMAT_NV12 = 103

#: 常用资源状态
RESOURCE_STATE_COMMON = 0x00000000
RESOURCE_STATE_COPY_DEST = 0x00000400
RESOURCE_STATE_COPY_SOURCE = 0x00000800
RESOURCE_STATE_RENDER_TARGET = 0x00000004
RESOURCE_STATE_PIXEL_SHADER_RESOURCE = 0x00000080


class TextureError(ElinkError):
    code = ErrorCode.NOT_SUPPORTED


@dataclass(frozen=True)
class TextureDesc:
    width: int
    height: int
    format: int = DXGI_FORMAT_B8G8R8A8_UNORM
    shared_handle: int = 0
    state: int = RESOURCE_STATE_COMMON

    def validate(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise TextureError(f"非法纹理尺寸: {self.width}x{self.height}")


class D3D12Texture:
    domain = FrameDomain.D3D12

    def __init__(self, desc: TextureDesc) -> None:
        desc.validate()
        self.desc = desc

    def to_frame(self, pts: int, timebase) -> Frame:
        return Frame(
            data=struct.pack(
                "<QIII", self.desc.shared_handle, self.desc.width, self.desc.height, self.desc.state
            ),
            timebase=timebase,
            pts=pts,
            width=self.desc.width,
            height=self.desc.height,
            pixel_format=PixelFormat.BGRA,
            domain=FrameDomain.D3D12,
        )

    @classmethod
    def from_frame(cls, frame: Frame) -> "D3D12Texture":
        if frame.domain is not FrameDomain.D3D12:
            raise TextureError(
                "帧不在 D3D12 域，不能当成纹理使用", detail=f"domain={frame.domain.value}"
            )
        handle, width, height, state = struct.unpack("<QIII", bytes(frame.data)[:20])
        return cls(
            TextureDesc(width=width, height=height, shared_handle=handle, state=state)
        )

    def describe(self) -> str:
        return (
            f"D3D12Texture {self.desc.width}x{self.desc.height} "
            f"fmt={self.desc.format} state=0x{self.desc.state:X}"
        )
