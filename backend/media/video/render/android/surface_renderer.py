"""Android 渲染端（``Surface`` + ``Canvas``）。

Android 上把帧画到 Surface 有两条路：

1. ``SurfaceView`` + ``lockCanvas``（CPU 绘制，简单稳定）——本文件实现这条；
2. ``Surface`` + GLES 纹理（零拷贝，需要 EGL 上下文）——留给后续优化。

依赖 ``pyjnius`` 与真实 Android 运行环境；桌面环境导入会被平台注册表跳过。
"""

from __future__ import annotations

from backend.errors.base import ElinkError, ErrorCode
from backend.logging.logger import get_logger
from backend.media.frame import Frame, PixelFormat

logger = get_logger(__name__)

try:  # pragma: no cover - 仅 Android 运行环境可用
    from jnius import autoclass  # type: ignore
except ImportError:  # pragma: no cover
    autoclass = None

ARGB_8888 = 1
BITMAP_CONFIG_ARGB_8888 = 1


class RenderError(ElinkError):
    code = ErrorCode.IO


class SurfaceRenderer:
    """``render(frame)`` 把一帧画进 Android Surface。"""

    name = "surface"

    def __init__(self, surface=None) -> None:
        if autoclass is None:
            raise RenderError("Android 渲染需要 pyjnius（pip install pyjnius）")
        if surface is None:
            raise RenderError(
                "缺少 Surface：请把 SurfaceView.getHolder().getSurface() 传进来"
            )
        self.surface = surface
        self._bitmap = None
        self._width = 0
        self._height = 0
        self._frames = 0

    def open(self) -> None:
        logger.info("Android Surface 渲染端就绪")

    def render(self, frame: Frame) -> bool:
        if frame.pixel_format is not PixelFormat.RGBA:
            raise RenderError(
                f"Surface 渲染需要 RGBA，收到 {frame.pixel_format}"
            )

        bitmap_class = autoclass("android.graphics.Bitmap")
        canvas_class = autoclass("android.graphics.Canvas")
        paint_class = autoclass("android.graphics.Paint")

        if self._bitmap is None or frame.width != self._width or frame.height != self._height:
            self._width, self._height = frame.width, frame.height
            self._bitmap = bitmap_class.createBitmap(
                frame.width, frame.height, BITMAP_CONFIG_ARGB_8888
            )
            self._bitmap.setHasAlpha(False)

        buffer = bytearray(bytes(frame.data))
        self._bitmap.copyPixelsFromBuffer(_byte_buffer(buffer))

        canvas = None
        try:
            canvas = self.surface.lockCanvas(None)
            if canvas is None:
                raise RenderError("lockCanvas 返回空，Surface 可能已失效")
            canvas.drawBitmap(self._bitmap, 0.0, 0.0, paint_class())
        finally:
            if canvas is not None:
                self.surface.unlockCanvasAndPost(canvas)
        del canvas_class
        self._frames += 1
        return True

    def close(self) -> None:
        if self._bitmap is not None:
            self._bitmap.recycle()
            self._bitmap = None
        logger.info("Android Surface 渲染端关闭，共显示 %d 帧", self._frames)

    def __enter__(self) -> "SurfaceRenderer":
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def _byte_buffer(payload: bytearray):  # pragma: no cover
    buffer_class = autoclass("java.nio.ByteBuffer")
    buffer = buffer_class.allocateDirect(len(payload))
    buffer.put(bytearray(payload))
    buffer.rewind()
    return buffer
