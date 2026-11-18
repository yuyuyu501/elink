"""D3D11 capture/scaling, DXCam latest frames and GDI recovery."""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

from backend.config.schema import CaptureConfig
from backend.errors.base import ElinkError, ErrorCode
from backend.logging.logger import get_logger
from backend.media.frame import Frame, FrameDomain, PixelFormat
from backend.media.timebase import Timebase

logger = get_logger(__name__)

if not hasattr(ctypes, "WinDLL"):  # pragma: no cover - 非 Windows 平台直接拒绝
    raise ImportError("Windows 桌面采集只能在 Windows 上导入")

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000
DIB_RGB_COLORS = 0
SM_CXSCREEN = 0
SM_CYSCREEN = 1

HANDLE = wintypes.HANDLE
HDC = HANDLE
HBITMAP = HANDLE
HGDIOBJ = HANDLE


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BitmapInfo(ctypes.Structure):
    _fields_ = [("bmiHeader", _BitmapInfoHeader), ("bmiColors", wintypes.DWORD * 3)]


user32.GetDC.restype = HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.restype = ctypes.c_int
user32.ReleaseDC.argtypes = [wintypes.HWND, HDC]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.GetSystemMetrics.argtypes = [ctypes.c_int]

gdi32.CreateCompatibleDC.restype = HDC
gdi32.CreateCompatibleDC.argtypes = [HDC]
gdi32.CreateDIBSection.restype = HBITMAP
gdi32.CreateDIBSection.argtypes = [
    HDC,
    ctypes.POINTER(_BitmapInfo),
    wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p),
    HANDLE,
    wintypes.DWORD,
]
gdi32.SelectObject.restype = HGDIOBJ
gdi32.SelectObject.argtypes = [HDC, HGDIOBJ]
gdi32.BitBlt.restype = wintypes.BOOL
gdi32.BitBlt.argtypes = [
    HDC,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    HDC,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.DWORD,
]
gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.DeleteObject.argtypes = [HGDIOBJ]
gdi32.DeleteDC.restype = wintypes.BOOL
gdi32.DeleteDC.argtypes = [HDC]


class CaptureError(ElinkError):
    code = ErrorCode.IO


class GdiCapture:
    """屏幕采集：``read()`` 产出一帧 BGRA 的 :class:`Frame`。"""

    backend = "gdi"

    def __init__(
        self,
        config: CaptureConfig | None = None,
        timebase: Timebase | None = None,
    ) -> None:
        self.config = config or CaptureConfig()
        self.timebase = timebase or Timebase.mpegts()
        self._screen_dc: int | None = None
        self._mem_dc: int | None = None
        self._bitmap: int | None = None
        self._bits: ctypes.c_void_p | None = None
        self._origin = (0, 0)
        self._width = 0
        self._height = 0
        self._start = 0.0
        from backend.media.video.cursor.capture import CursorCapture
        self.cursor = CursorCapture()
        self.cursor_mode = "smart"
        self.cursor_applied_mode = "smart"
        self.cursor_visible = None
        self._previous_bitmap = None

    # ---- 属性 ----
    @property
    def width(self) -> int:
        return self._width

    @property
    def height(self) -> int:
        return self._height

    @property
    def pixel_format(self) -> PixelFormat:
        return PixelFormat.BGRA

    # ---- 生命周期 ----
    def open(self) -> None:
        self._screen_dc = user32.GetDC(None)
        if not self._screen_dc:
            raise CaptureError("GetDC 失败", detail=ctypes.get_last_error())

        region = self.config.clamped_region()
        if region is None:
            width = user32.GetSystemMetrics(SM_CXSCREEN)
            height = user32.GetSystemMetrics(SM_CYSCREEN)
            region = (0, 0, width, height)

        x, y, width, height = region
        if width <= 0 or height <= 0:
            self.close()
            raise CaptureError(f"非法采集区域: {region}")

        info = _BitmapInfo()
        info.bmiHeader.biSize = ctypes.sizeof(_BitmapInfoHeader)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height  # 负值 = 自上而下
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = 0  # BI_RGB

        self._mem_dc = gdi32.CreateCompatibleDC(self._screen_dc)
        if not self._mem_dc:
            self.close()
            raise CaptureError("CreateCompatibleDC 失败", detail=ctypes.get_last_error())

        bits = ctypes.c_void_p()
        self._bitmap = gdi32.CreateDIBSection(
            self._mem_dc, ctypes.byref(info), DIB_RGB_COLORS, ctypes.byref(bits), None, 0
        )
        if not self._bitmap or not bits:
            self.close()
            raise CaptureError("CreateDIBSection 失败", detail=ctypes.get_last_error())

        self._previous_bitmap = gdi32.SelectObject(self._mem_dc, self._bitmap)
        if not self._previous_bitmap:
            self.close()
            raise CaptureError("SelectObject 失败", detail=ctypes.get_last_error())

        self._bits = bits
        self._origin = (x, y)
        self._width = width
        self._height = height
        self._start = time.perf_counter()
        logger.info(
            "屏幕采集就绪: 区域=(%d,%d) %dx%d 后端=%s", x, y, width, height, self.backend
        )

    def close(self) -> None:
        if self._mem_dc and self._bitmap:
            gdi32.SelectObject(self._mem_dc, self._previous_bitmap)
        if self._bitmap:
            gdi32.DeleteObject(self._bitmap)
            self._bitmap = None
        if self._mem_dc:
            gdi32.DeleteDC(self._mem_dc)
            self._mem_dc = None
        if self._screen_dc:
            user32.ReleaseDC(None, self._screen_dc)
            self._screen_dc = None
        self._bits = None
        self.cursor.close()

    # ---- 取帧 ----
    def read(self) -> Frame:
        if not self._screen_dc or not self._mem_dc or not self._bits:
            raise CaptureError("采集器未打开")

        x, y = self._origin
        if not gdi32.BitBlt(
            self._mem_dc,
            0,
            0,
            self._width,
            self._height,
            self._screen_dc,
            x,
            y,
            SRCCOPY | CAPTUREBLT,
        ):
            raise CaptureError("BitBlt 失败", detail=ctypes.get_last_error())

        data = ctypes.string_at(self._bits, self._width * self._height * 4)
        pts = self.timebase.ticks(time.perf_counter() - self._start)
        frame = Frame(
            data=data,
            timebase=self.timebase,
            pts=pts,
            width=self._width,
            height=self._height,
            pixel_format=PixelFormat.BGRA,
            domain=FrameDomain.CPU,
        )
        from backend.media.video.cursor.overlay import CursorOverlay
        mode = self.cursor_mode
        self.cursor.include_image = mode != "local"
        state = self.cursor.read()
        self.cursor_visible = state.visible
        self.cursor_applied_mode = mode
        if mode != "local":
            frame = CursorOverlay(origin=self._origin).composite(frame, state) if not state.is_empty else frame
        return frame

    def __enter__(self) -> "DesktopCapture":
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


class DesktopCapture:
    """Prefer D3D11, then DXGI latest frames; recover an unavailable desktop."""

    def __init__(self, config=None, timebase=None):
        self.config = config or CaptureConfig()
        self.timebase = timebase or Timebase.mpegts()
        self.cursor_mode = self.cursor_applied_mode = "smart"
        self.cursor_visible = None
        self.backend = ""
        self.failures = []
        self._source = self._graph = self._sink = None
        self._last = None
        self._start = 0
        self._draw_mouse = False
        self._retry_at = 0.0
        self._gpu = None

    def open(self):
        self._start = time.perf_counter()
        self.close()
        if self.config.backend != "gdi":
            try:
                region = self.config.clamped_region()
                width = self.config.width or (region[2] if region else user32.GetSystemMetrics(SM_CXSCREEN))
                height = self.config.height or (region[3] if region else user32.GetSystemMetrics(SM_CYSCREEN))
                self._draw_mouse = self.cursor_mode != "local"
                self._gpu = _D3D11Source(width, height, self._draw_mouse, region)
                self._last = Frame.from_av(self._gpu.read(), native=True)
                self.backend = "d3d11"
                return
            except Exception as exc:
                self.failures.append(f"D3D11: {exc}")
                if self._gpu:
                    self._gpu.close()
                self._gpu = None
            try:
                import dxcam
                if not getattr(self, "_com_initialized", False):
                    self._com_initialized = ctypes.windll.ole32.CoInitializeEx(None, 2) in (0, 1)
                self._source = dxcam.create(output_color="BGRA")
                region = self.config.clamped_region()
                if region:
                    x, y, w, h = region
                    region = (x, y, x + w, y + h)
                self._source.start(region=region, target_fps=round(self.config.fps), video_mode=True)
                self.backend = "dxcam"
                return
            except Exception as exc:
                self.failures.append(f"DXCam: {exc}")
                self.close()
        self._source = GdiCapture(self.config, self.timebase)
        self._source.open()
        self.backend = "gdi"
        logger.info("采集回退 GDI: %s", self.failures[-2:])

    def read(self):
        import av
        import numpy as np
        try:
            if self._gpu is not None:
                if self._draw_mouse != (self.cursor_mode != "local"):
                    self.open()
                frame = Frame.from_av(self._gpu.read(), native=True)
            elif self.backend == "dxcam":
                pixels = self._source.get_latest_frame()
                if pixels is None:
                    raise CaptureError("DXGI 没有可用画面")
                pixels = np.ascontiguousarray(pixels)
                frame = Frame(data=memoryview(pixels).cast("B"), width=pixels.shape[1],
                              height=pixels.shape[0], owner=pixels)
            else:
                self._source.cursor_mode = self.cursor_mode
                frame = self._source.read()
                self.cursor_visible = self._source.cursor_visible
            if self.backend != "gdi":
                from backend.media.video.cursor.capture import CursorCapture
                from backend.media.video.cursor.overlay import CursorOverlay
                if not hasattr(self, "cursor"):
                    self.cursor = CursorCapture()
                self.cursor.include_image = self.cursor_mode != "local"
                state = self.cursor.read()
                self.cursor_visible = state.visible
                if self.backend != "d3d11" and not state.is_empty and self.cursor_mode != "local":
                    region = self.config.clamped_region()
                    # Overlay before resizing so desktop cursor coordinates stay correct.
                    if not self.config.width:
                        frame = CursorOverlay(origin=region[:2] if region else (0, 0)).composite(frame, state)
            frame.pts = self.timebase.ticks(time.perf_counter() - self._start)
            self.cursor_applied_mode = self.cursor_mode
            self._last = frame
            return frame
        except (OSError, av.FFmpegError, CaptureError) as exc:
            self.failures.append(str(exc))
            if time.monotonic() >= self._retry_at:
                self._retry_at = time.monotonic() + 1
                try:
                    self.open()
                except Exception as recovery:
                    self.failures.append(str(recovery))
            if self._last is not None:
                return self._last
            raise

    def close(self):
        source, self._source = self._source, None
        self._sink = self._graph = None
        if self._gpu is not None:
            self._gpu.close()
            self._gpu = None
        if source is not None:
            if hasattr(source, "stop"):
                source.stop()
            if hasattr(source, "release"):
                source.release()
            elif hasattr(source, "close"):
                source.close()
        if hasattr(self, "cursor"):
            self.cursor.close()
        if getattr(self, "_com_initialized", False):
            ctypes.windll.ole32.CoUninitialize()
            self._com_initialized = False


class _CalibrationPending(RuntimeError):
    pass


def _color_blocks(pixels):
    size = max(16, min(pixels.shape[:2]) // 48)
    h, w = pixels.shape[0] // size, pixels.shape[1] // size
    return pixels[:h * size, :w * size].reshape(h, size, w, size, 3).mean(axis=(1, 3))


def _calibrated_color(frame, reference):
    import numpy as np
    expected = _color_blocks(reference.reformat(width=frame.width, height=frame.height,
                                                format="rgb24").to_ndarray())
    colored = np.ptp(expected, axis=2) > 8
    if np.count_nonzero(colored) < 16:
        raise _CalibrationPending("桌面彩色样本不足")
    candidates = []
    for space, label in ((5, "ITU601"), (1, "ITU709")):
        for color_range in (1, 2):
            pixels = frame.to_ndarray(format="rgb24", src_colorspace=label,
                                     dst_colorspace="ITU601", src_color_range=color_range)
            error = float(np.mean(np.abs(_color_blocks(pixels) - expected)[colored]))
            candidates.append((error, space, color_range))
    candidates.sort()
    if candidates[0][0] > 6:
        raise RuntimeError("D3D11 颜色校准失败")
    if candidates[1][0] - candidates[0][0] < .25:
        raise _CalibrationPending("D3D11 颜色校准样本不明确")
    return candidates[0][1:]


class _D3D11Source:
    """Ported GPU scaling and measured color calibration from the old capture."""

    def __init__(self, width, height, draw_mouse, region):
        self.width, self.height, self.draw_mouse, self.region = width, height, draw_mouse, region
        self.pending = True
        self.next_calibration = 0
        self._build(reference=True)

    def _build(self, reference=False):
        import av
        self.graph = av.filter.Graph()
        args = f"framerate=1000:dup_frames=1:draw_mouse={int(self.draw_mouse)}"
        if self.region:
            x, y, width, height = self.region
            args += f":offset_x={x}:offset_y={y}:video_size={width}x{height}"
        source = self.graph.add("ddagrab", args)
        if reference:
            splitter = self.graph.add("split", "outputs=2")
            source.link_to(splitter)
            source = splitter
            download = self.graph.add("hwdownload")
            splitter.link_to(download, 1)
            fmt = self.graph.add("format", "pix_fmts=bgra")
            download.link_to(fmt)
            self.reference = self.graph.add("buffersink")
            fmt.link_to(self.reference)
        stages = [source,
                  self.graph.add("scale_d3d11", f"width={self.width}:height={self.height}:format=nv12"),
                  self.graph.add("hwdownload"), self.graph.add("format", "pix_fmts=nv12"),
                  self.graph.add("buffersink")]
        for left, right in zip(stages, stages[1:]):
            left.link_to(right)
        self.graph.configure()
        self.sink = stages[-1]

    def read(self):
        frame = self.sink.pull()
        if self.pending:
            reference = self.reference.pull()
            if frame.pts != reference.pts:
                raise RuntimeError("D3D11 校准画面时间戳不一致")
            if time.monotonic() >= self.next_calibration:
                try:
                    self.colorspace, self.color_range = _calibrated_color(frame, reference)
                except _CalibrationPending:
                    self.next_calibration = time.monotonic() + .25
                else:
                    self.close()
                    self._build()
                    self.pending = False
                    frame.colorspace, frame.color_range = self.colorspace, self.color_range
                    return frame
            frame = reference.reformat(width=self.width, height=self.height, format="nv12",
                                       dst_colorspace="ITU709", dst_color_range=1)
            frame.colorspace, frame.color_range = 1, 1
        else:
            frame.colorspace, frame.color_range = self.colorspace, self.color_range
        return frame

    def close(self):
        self.graph = self.sink = self.reference = None
