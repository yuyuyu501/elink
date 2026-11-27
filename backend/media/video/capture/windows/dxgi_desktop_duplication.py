"""Windows desktop capture backend selection."""

from __future__ import annotations

import ctypes
import time

from backend.config.schema import CaptureConfig, WINDOWS_CAPTURE_BACKENDS
from backend.errors.base import ElinkError, ErrorCode
from backend.logging.logger import get_logger
from backend.media.frame import Frame
from backend.media.timebase import Timebase

logger = get_logger(__name__)

if not hasattr(ctypes, "WinDLL"):  # pragma: no cover - 非 Windows 平台直接拒绝
    raise ImportError("Windows 桌面采集只能在 Windows 上导入")

user32 = ctypes.WinDLL("user32", use_last_error=True)
SM_CXSCREEN = 0
SM_CYSCREEN = 1
user32.GetSystemMetrics.restype = ctypes.c_int
user32.GetSystemMetrics.argtypes = [ctypes.c_int]


class CaptureError(ElinkError):
    code = ErrorCode.IO


class DesktopCapture:
    """Select a desktop capture backend and normalize its frames."""

    def __init__(self, config=None, timebase=None):
        self.config = config or CaptureConfig()
        self.timebase = timebase or Timebase.mpegts()
        self.cursor_mode = self.cursor_applied_mode = "smart"
        self.cursor_visible = None
        self.backend = ""
        self.failures = []
        self._source = None
        self._last = None
        self._start = 0
        self._draw_mouse = False
        self._retry_at = 0.0
        self._gpu = None

    def reconfigure(self, width, height, fps):
        from dataclasses import replace
        previous = self.config
        previous_start = self._start
        self.config = replace(previous, width=width, height=height, fps=fps)
        try:
            self.open()
        except Exception:
            self.config = previous
            self.open()
            raise
        finally:
            self._start = previous_start

    def open(self):
        self.close()
        self._start = time.perf_counter()
        self.failures.clear()
        backends = WINDOWS_CAPTURE_BACKENDS if self.config.backend == "auto" else (self.config.backend,)
        for backend in backends:
            try:
                self._open_backend(backend)
                logger.info("屏幕采集就绪: 后端=%s，先前尝试=%s", self.backend, self.failures)
                return
            except Exception as exc:
                self.close()
                self.failures.append(f"{backend}: {exc}")
        raise CaptureError(
            "桌面采集初始化失败",
            detail="; ".join(self.failures),
        )

    def _open_backend(self, backend):
        region = self.config.clamped_region()
        self._draw_mouse = self.cursor_mode != "local"
        if backend == "d3d11":
            width = self.config.width or (region[2] if region else user32.GetSystemMetrics(SM_CXSCREEN))
            height = self.config.height or (region[3] if region else user32.GetSystemMetrics(SM_CYSCREEN))
            self._gpu = _D3D11Source(width, height, self._draw_mouse, region)
            self._last = Frame.from_av(self._gpu.read(), native=True)
        elif backend == "dxcam":
            from .dxgi_capture import dxcam_output

            result = ctypes.windll.ole32.CoInitializeEx(None, 2)
            if result not in (0, 1, -2147417850):
                raise OSError(f"CoInitializeEx 失败: 0x{result & 0xffffffff:08x}")
            try:
                import dxcam

                device, output, left, top = dxcam_output(region)
                self._source = dxcam.create(device_idx=device, output_idx=output,
                                            output_color="BGRA", max_buffer_len=2)
                dxcam_region = None
                if region:
                    x, y, width, height = region
                    dxcam_region = (x - left, y - top, x - left + width, y - top + height)
                self._source.start(region=dxcam_region, target_fps=round(self.config.fps),
                                   video_mode=True)
            finally:
                if result in (0, 1):
                    ctypes.windll.ole32.CoUninitialize()
        elif backend == "dxgi":
            from .dxgi_capture import DxgiCapture
            self._source = DxgiCapture(region)
            self._source.open()
        else:
            raise CaptureError(f"未知采集方式: {backend}")
        self.backend = backend

    def read(self):
        import av
        import numpy as np
        try:
            if self.backend == "d3d11":
                if self._draw_mouse != (self.cursor_mode != "local"):
                    start = self._start
                    try:
                        self.open()
                    finally:
                        self._start = start
                    return self.read()
                frame = Frame.from_av(self._gpu.read(), native=True)
            elif self.backend == "dxcam":
                pixels = self._source.get_latest_frame()
                if pixels is None:
                    raise CaptureError("DXCam 没有可用画面")
                pixels = np.ascontiguousarray(pixels)
                frame = Frame(data=memoryview(pixels).cast("B"), width=pixels.shape[1],
                              height=pixels.shape[0], owner=pixels)
            elif self.backend == "dxgi":
                frame = self._source.read()
            else:
                raise CaptureError("采集器未打开")
            if self.backend in {"dxcam", "dxgi"}:
                from backend.media.video.cursor.capture import CursorCapture
                from backend.media.video.cursor.overlay import CursorOverlay
                if not hasattr(self, "cursor"):
                    self.cursor = CursorCapture()
                self.cursor.include_image = self.cursor_mode != "local"
                state = self.cursor.read()
                self.cursor_visible = state.visible
                if not state.is_empty and self.cursor_mode != "local":
                    region = self.config.clamped_region()
                    frame = CursorOverlay(origin=region[:2] if region else (0, 0)).composite(frame, state)
            frame.pts = self.timebase.ticks(time.perf_counter() - self._start)
            self.cursor_applied_mode = self.cursor_mode
            self._last = frame
            return frame
        except (OSError, av.FFmpegError, CaptureError):
            last, start = self._last, self._start
            if time.monotonic() >= self._retry_at:
                self._retry_at = time.monotonic() + 1
                try:
                    self.open()
                except Exception:
                    self._last = last
                finally:
                    self._start = start
            if self._last is not None:
                return self._last
            raise

    def close(self):
        source, self._source = self._source, None
        self.backend = ""
        self._last = None
        if self._gpu is not None:
            self._gpu.close()
            self._gpu = None
        if source is not None:
            if hasattr(source, "release"):
                source.release()
            else:
                source.close()
        if hasattr(self, "cursor"):
            self.cursor.close()


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
