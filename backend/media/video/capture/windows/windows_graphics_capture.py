"""Windows Graphics Capture for the primary display and named windows."""

from __future__ import annotations

import time
from threading import Event

from backend.media.frame import Frame, PixelFormat
from backend.media.timebase import Timebase


class WindowsGraphicsCapture:
    backend = "wgc"
    pixel_format = PixelFormat.BGRA

    def __init__(self, window_title="", region=None, fps=30.0, timebase=None, *, draw_mouse=True):
        self.window_title = window_title
        self.region = region
        self.fps = fps
        self.timebase = timebase or Timebase.mpegts()
        self.draw_mouse = draw_mouse
        self._control = None
        self._pixels = None
        self._closed = False
        self._start = 0.0

    @property
    def width(self):
        return self._pixels.shape[1] if self._pixels is not None else 0

    @property
    def height(self):
        return self._pixels.shape[0] if self._pixels is not None else 0

    def open(self):
        from windows_capture import WindowsCapture
        from .dxgi_desktop_duplication import CaptureError

        self.close()
        self._closed = False
        self._start = time.perf_counter()
        ready = Event()
        capture = WindowsCapture(cursor_capture=self.draw_mouse, window_name=self.window_title or None)

        @capture.event
        def on_frame_arrived(frame, control):
            pixels = frame.frame_buffer
            if self.region is not None:
                x, y, width, height = self.region
                if x < 0 or y < 0 or x + width > pixels.shape[1] or y + height > pixels.shape[0]:
                    self._closed = True
                    ready.set()
                    control.stop()
                    return
                pixels = pixels[y:y + height, x:x + width]
            # WGC unmaps the staging texture as soon as the callback returns.
            self._pixels = pixels.copy(order="C")
            ready.set()

        @capture.event
        def on_closed():
            self._closed = True
            ready.set()

        self._control = capture.start_free_threaded()
        if not ready.wait(2) or self._pixels is None or self._closed:
            self.close()
            raise CaptureError("WGC 未能从主屏幕或指定窗口取得首帧")

    def read(self):
        from .dxgi_desktop_duplication import CaptureError

        pixels = self._pixels
        control = self._control
        if pixels is None or self._closed or control is None or control.is_finished():
            raise CaptureError("WGC 采集已停止")
        return Frame(
            data=memoryview(pixels).cast("B"), width=pixels.shape[1], height=pixels.shape[0],
            owner=pixels, timebase=self.timebase, pts=self.timebase.ticks(time.perf_counter() - self._start),
        )

    def close(self):
        control, self._control = self._control, None
        if control is not None and not control.is_finished():
            control.stop()
        self._pixels = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc_info):
        self.close()


def create_capture(config=None):
    return WindowsGraphicsCapture(
        region=getattr(config, "region", None), fps=getattr(config, "fps", 30.0),
    )
