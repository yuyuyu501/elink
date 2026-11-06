"""D3D12 渲染端。

D3D12 的交换链（``IDXGISwapChain3`` + 命令队列 + 围栏）比 D3D11 更啰嗦，
Python 侧接入成本高；当前复用 D3D11 渲染端的窗口上屏实现，接口保持一致，
等交换链接入时只替换 ``render`` 内部，不影响管线。

``available()`` 会探测 ``d3d12.dll``，用于能力协商阶段的选型。
"""

from __future__ import annotations

import ctypes

from backend.media.video.render.windows.d3d11_renderer import WindowPresenter


def gpu_available() -> bool:
    try:
        ctypes.WinDLL("d3d12")
    except OSError:
        return False
    return True


class D3D12Renderer(WindowPresenter):
    name = "d3d12"

    @staticmethod
    def available() -> bool:
        return gpu_available()
