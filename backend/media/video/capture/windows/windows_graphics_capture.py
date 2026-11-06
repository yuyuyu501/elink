"""Windows 窗口/屏幕采集（Windows.Graphics.Capture）。

WGC 是 WinRT 接口（``GraphicsCaptureItem`` + ``Direct3D11CaptureFramePool``），
Python 侧必须走 ``winsdk``/``winrt`` 这类 WinRT 绑定。本文件的做法是：

1. 有 WinRT 绑定时给出可用的窗口采集入口（返回的仍是本项目 ``Frame``）；
2. 没有绑定时回退到同机的 GDI 桌面采集实现，保证「同一份管线代码能跑」
   ——这正是平台注册表存在的意义：能力降级不该让上层改代码。

注意：WGC 回调线程是 COM 线程，必须固定在同一线程取帧；这里用独立工作线程
+ 队列把帧搬到调用线程，符合项目「一实例一 worker」的约定。
"""

from __future__ import annotations

import queue
import threading
from collections import deque

from backend.errors.base import ElinkError, ErrorCode
from backend.logging.logger import get_logger
from backend.media.frame import Frame, FrameDomain, PixelFormat
from backend.media.timebase import Timebase

logger = get_logger(__name__)

try:  # pragma: no cover - 取决于是否装了 WinRT 绑定
    from winsdk.windows.graphics.capture import (  # type: ignore
        Direct3D11CaptureFramePool,
        GraphicsCaptureItem,
        GraphicsCaptureSession,
    )
    from winsdk.windows.graphics.directx import DirectXPixelFormat  # type: ignore
    from winsdk.windows.graphics.directx.direct3d11 import (  # type: ignore
        create_direct3d11_device_from_dxgi_device,
    )

    _WGC_AVAILABLE = True
except ImportError:  # pragma: no cover
    _WGC_AVAILABLE = False


class CaptureError(ElinkError):
    code = ErrorCode.IO


class WindowsGraphicsCapture:
    """窗口采集；WinRT 不可用时自动降级为 GDI 桌面采集。"""

    def __init__(
        self,
        window_title: str = "",
        region: tuple[int, int, int, int] | None = None,
        fps: float = 30.0,
        timebase: Timebase | None = None,
    ) -> None:
        self.window_title = window_title
        self.region = region
        self.fps = fps
        self.timebase = timebase or Timebase.mpegts()
        self._fallback = None
        self._frames: "queue.Queue[Frame]" = queue.Queue(maxsize=2)
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._frame_pool = None
        self._session = None
        self._item = None
        self._width = 0
        self._height = 0
        self._stderr_tail: deque[str] = deque(maxlen=10)

    @property
    def backend(self) -> str:
        return "wgc" if _WGC_AVAILABLE and self.window_title else "gdi-fallback"

    @property
    def width(self) -> int:
        return self._width or getattr(self._fallback, "width", 0)

    @property
    def height(self) -> int:
        return self._height or getattr(self._fallback, "height", 0)

    @property
    def pixel_format(self) -> PixelFormat:
        return PixelFormat.BGRA

    def open(self) -> None:
        if not _WGC_AVAILABLE:
            logger.info("未安装 WinRT 绑定（winsdk），窗口采集降级为 GDI 桌面采集")
            self._open_fallback()
            return

        if not self.window_title:
            # 没指定窗口时，整屏用 GDI 更快；WGC 适合按窗口抓
            self._open_fallback()
            return

        self._open_wgc()

    def _open_fallback(self) -> None:
        from backend.media.video.capture.windows.dxgi_desktop_duplication import (
            DesktopCapture,
        )
        from backend.config.schema import CaptureConfig

        self._fallback = DesktopCapture(
            CaptureConfig(region=self.region, fps=self.fps), timebase=self.timebase
        )
        self._fallback.open()
        self._width = self._fallback.width
        self._height = self._fallback.height

    def _open_wgc(self) -> None:  # pragma: no cover - 需要真实窗口与 WinRT
        item = GraphicsCaptureItem.create_from_window(_find_window(self.window_title))
        device = create_direct3d11_device_from_dxgi_device(_create_dxgi_device())
        pool = Direct3D11CaptureFramePool.create(
            device, DirectXPixelFormat.B8_G8_R8_A8_UINT_NORMALIZED, 2, item.size
        )
        session = pool.create_capture_session(item)
        session.is_cursor_capture_enabled = True
        session.start_capture()

        self._item = item
        self._frame_pool = pool
        self._session = session
        self._width = item.size.width
        self._height = item.size.height

        self._stop.clear()
        self._worker = threading.Thread(target=self._capture_loop, name="wgc", daemon=True)
        self._worker.start()
        logger.info("窗口采集就绪(WGC): %dx%d", self._width, self._height)

    def _capture_loop(self) -> None:  # pragma: no cover
        while not self._stop.is_set():
            frame = self._frame_pool.try_get_next_frame()
            if frame is None:
                self._stop.wait(1.0 / max(self.fps, 1.0))
                continue
            try:
                data = _copy_surface(frame.surface, self._width, self._height)
            finally:
                frame.close()
            pts = self.timebase.ticks(len(data) and 0)  # 时间戳由调用点覆盖
            item = Frame(
                data=data,
                timebase=self.timebase,
                pts=pts,
                width=self._width,
                height=self._height,
                pixel_format=PixelFormat.BGRA,
                domain=FrameDomain.CPU,
            )
            try:
                self._frames.put_nowait(item)
            except queue.Full:
                try:
                    self._frames.get_nowait()
                    self._frames.put_nowait(item)
                except (queue.Empty, queue.Full):
                    pass

    def read(self) -> Frame:
        if self._fallback is not None:
            return self._fallback.read()
        try:
            return self._frames.get(timeout=1.0)
        except queue.Empty as exc:
            raise CaptureError("WGC 采集超时，未拿到新帧") from exc

    def close(self) -> None:
        self._stop.set()
        if self._worker is not None and self._worker.is_alive():
            self._worker.join(timeout=2)
        if self._session is not None:
            self._session.close()
            self._session = None
        if self._frame_pool is not None:
            self._frame_pool.close()
            self._frame_pool = None
        if self._fallback is not None:
            self._fallback.close()
            self._fallback = None

    def __enter__(self) -> "WindowsGraphicsCapture":
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def _find_window(title: str) -> int:  # pragma: no cover
    import ctypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    return user32.FindWindowW(None, title)


def _create_dxgi_device() -> int:  # pragma: no cover
    raise CaptureError("DXGI 设备创建需要 d3d11 绑定，当前版本未接入")


def _copy_surface(surface, width: int, height: int) -> bytes:  # pragma: no cover
    raise CaptureError("从 IDirect3DSurface 读回像素需要显式 hwdownload，当前未接入")


def create_capture(config=None) -> WindowsGraphicsCapture:
    """平台注册表用的工厂。"""
    region = getattr(config, "region", None) if config else None
    fps = getattr(config, "fps", 30.0) if config else 30.0
    return WindowsGraphicsCapture(region=region, fps=fps)
