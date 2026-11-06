"""D3D11 纹理封装。

Python 侧不持有裸 ``ID3D11Texture2D`` 指针的语义（生命周期、线程亲和都由 COM
决定），所以这里只做**描述符**：记录尺寸、格式、共享句柄与所属内存域，
真正创建/拷贝由加速层（或后续接入的 COM 绑定）完成。

这样 ``Frame`` 的值语义保持不变：GPU 帧也只是「一块数据 + 域标记」，
只是 ``data`` 里放的是共享句柄而不是像素。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from backend.errors.base import ElinkError, ErrorCode
from backend.media.frame import Frame, FrameDomain, PixelFormat

#: DXGI_FORMAT 常用值
DXGI_FORMAT_B8G8R8A8_UNORM = 87
DXGI_FORMAT_NV12 = 103
DXGI_FORMAT_P010 = 104


class TextureError(ElinkError):
    code = ErrorCode.NOT_SUPPORTED


@dataclass(frozen=True)
class TextureDesc:
    width: int
    height: int
    format: int = DXGI_FORMAT_B8G8R8A8_UNORM
    shared_handle: int = 0
    mip_levels: int = 1

    def validate(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise TextureError(f"非法纹理尺寸: {self.width}x{self.height}")


class D3D11Texture:
    """纹理句柄的包装，可无损装进 / 取出 ``Frame``。"""

    domain = FrameDomain.D3D11
    pixel_format = PixelFormat.BGRA

    def __init__(self, desc: TextureDesc) -> None:
        desc.validate()
        self.desc = desc

    def to_frame(self, pts: int, timebase) -> Frame:
        """把纹理包装成 GPU 域的 ``Frame``（data 为共享句柄的字节编码）。"""
        return Frame(
            data=struct.pack("<QII", self.desc.shared_handle, self.desc.width, self.desc.height),
            timebase=timebase,
            pts=pts,
            width=self.desc.width,
            height=self.desc.height,
            pixel_format=PixelFormat.BGRA,
            domain=FrameDomain.D3D11,
        )

    @classmethod
    def from_frame(cls, frame: Frame) -> "D3D11Texture":
        if frame.domain is not FrameDomain.D3D11:
            raise TextureError(
                "帧不在 D3D11 域，不能当成纹理使用", detail=f"domain={frame.domain.value}"
            )
        handle, width, height = struct.unpack("<QII", bytes(frame.data)[:16])
        return cls(TextureDesc(width=width, height=height, shared_handle=handle))

    def describe(self) -> str:
        return (
            f"D3D11Texture {self.desc.width}x{self.desc.height} "
            f"fmt={self.desc.format} handle=0x{self.desc.shared_handle:X}"
        )
