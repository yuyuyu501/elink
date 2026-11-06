"""Android 屏幕采集（``MediaProjection`` + ``ImageReader``）。

流程：先由 Activity 拿到 ``MediaProjection`` 授权 → 创建 ``ImageReader`` →
``VirtualDisplay`` 把屏幕投射到 ImageReader 的 Surface 上 → 每帧 acquire 一张
Image，把 ``ByteBuffer`` 拷成 RGBA 后组装成 ``Frame``。

依赖：``pyjnius`` 且运行在带 JVM 的 Android 环境中；桌面环境导入会被平台
注册表跳过。
"""

from __future__ import annotations

from backend.errors.base import ElinkError, ErrorCode
from backend.errors.permission import PermissionDeniedError
from backend.logging.logger import get_logger
from backend.media.frame import Frame, FrameDomain, PixelFormat
from backend.media.timebase import Timebase

logger = get_logger(__name__)

try:  # pragma: no cover - 仅 Android 运行环境可用
    from jnius import autoclass, cast  # type: ignore
except ImportError:  # pragma: no cover
    autoclass = None
    cast = None

PIXEL_FORMAT_RGBA_8888 = 1


class CaptureError(ElinkError):
    code = ErrorCode.IO


class MediaProjectionCapture:
    """``read()`` 产出一帧 RGBA 的 ``Frame``。"""

    backend = "media_projection"
    pixel_format = PixelFormat.RGBA

    def __init__(
        self,
        projection=None,
        width: int = 0,
        height: int = 0,
        fps: float = 30.0,
        timebase: Timebase | None = None,
    ) -> None:
        if autoclass is None:
            raise CaptureError("Android 屏幕采集需要 pyjnius（pip install pyjnius）")
        if projection is None:
            raise PermissionDeniedError(
                "缺少 MediaProjection 授权：需先由 Activity 通过 "
                "MediaProjectionManager.createScreenCaptureIntent() 申请并回调传入"
            )
        self.projection = projection
        self.width = width
        self.height = height
        self.fps = fps
        self.timebase = timebase or Timebase.mpegts()
        self._reader = None
        self._virtual_display = None
        self._start = 0.0

    def open(self) -> None:
        import time

        image_reader = autoclass("android.media.ImageReader")
        display_manager = autoclass("android.hardware.display.DisplayManager")
        media_projection = autoclass("android.media.projection.MediaProjection")

        if not self.width or not self.height:
            metrics = autoclass("android.util.DisplayMetrics")()
            window_manager = cast(
                "android.view.WindowManager",
                autoclass("android.app.ActivityThread")
                .currentApplication()
                .getSystemService("window"),
            )
            window_manager.getDefaultDisplay().getRealMetrics(metrics)
            self.width = metrics.widthPixels
            self.height = metrics.heightPixels

        self._reader = image_reader.newInstance(
            self.width, self.height, PIXEL_FORMAT_RGBA_8888, 3
        )
        manager = autoclass("android.hardware.display.DisplayManager")
        self._virtual_display = self.projection.createVirtualDisplay(
            "elink-screen",
            self.width,
            self.height,
            manager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
            self._reader.getSurface(),
            None,
            None,
        )
        del display_manager, media_projection
        self._start = time.perf_counter()
        logger.info("Android 屏幕采集就绪: %dx%d 后端=%s", self.width, self.height, self.backend)

    def close(self) -> None:
        if self._virtual_display is not None:
            self._virtual_display.release()
            self._virtual_display = None
        if self._reader is not None:
            self._reader.close()
            self._reader = None

    def read(self, timeout_ms: int = 1000) -> Frame:
        import time

        if self._reader is None:
            raise CaptureError("采集器未打开")

        image = self._reader.acquireLatestImage()
        if image is None:
            time.sleep(1.0 / max(self.fps, 1.0))
            raise CaptureError("本帧未就绪（ImageReader 还没有新图像）", detail="retry")

        try:
            plane = image.getPlanes()[0]
            buffer = plane.getBuffer()
            row_stride = plane.getRowStride()
            pixel_stride = plane.getPixelStride()
            width = image.getWidth()
            height = image.getHeight()

            payload = bytearray(height * width * 4)
            row_bytes = width * 4
            if row_stride == row_bytes and pixel_stride == 4:
                buffer.rewind()
                buffer.get(payload)
            else:
                # 行间有 padding：逐行拷贝
                for row in range(height):
                    buffer.position(row * row_stride)
                    row_data = bytearray(row_bytes)
                    buffer.get(row_data)
                    payload[row * row_bytes : (row + 1) * row_bytes] = row_data
        finally:
            image.close()

        pts = self.timebase.ticks(time.perf_counter() - self._start)
        return Frame(
            data=bytes(payload),
            timebase=self.timebase,
            pts=pts,
            width=width,
            height=height,
            pixel_format=PixelFormat.RGBA,
            domain=FrameDomain.CPU,
        )

    def __enter__(self) -> "MediaProjectionCapture":
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def create_capture(config=None, projection=None):
    """平台注册表用的工厂。"""
    width = getattr(config, "width", 0) if config else 0
    height = getattr(config, "height", 0) if config else 0
    fps = getattr(config, "fps", 30.0) if config else 30.0
    return MediaProjectionCapture(projection=projection, width=width, height=height, fps=fps)
