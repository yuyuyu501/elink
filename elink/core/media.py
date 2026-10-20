from __future__ import annotations

import asyncio
import collections
import ctypes
import fractions
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import av
import numpy as np
from aiortc import MediaStreamTrack
from aiortc.mediastreams import MediaStreamError

from .codecs import metrics
from .profiling import PipelineProfile
from .pacing import FrameClock


log = logging.getLogger(__name__)


def bgr_pixels(frame):
    # PyAV/sws can skip matrix setup when source and destination both inherit
    # BT.709. RGB has no YUV matrix: an explicit 601 destination forces the
    # correct source coefficients while preserving the decoded color range.
    return frame.to_ndarray(format='bgr24', dst_colorspace='ITU601',
                            src_color_range=int(frame.color_range))


class DesktopTrack(MediaStreamTrack):
    kind = "video"

    def __init__(self, width: int, height: int, fps: int, synthetic: bool = False, profile=None, capture_backend='auto'):
        super().__init__()
        self.profile = profile or PipelineProfile()
        self.width, self.height, self.fps = width, height, fps
        self.synthetic = synthetic
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="elink-capture")
        self.camera = None
        self.cursor = None
        self.cursor_mode = 'remote'
        self.cursor_visible = None
        self.last = None
        self.start_time = None
        self.frame_clock = FrameClock(fps)
        self.reformatter = av.video.reformatter.VideoReformatter()
        self.last_pts = -1
        self.frame_count = 0
        self.capture_future = None
        self.network_budget = None
        self.capture_backend = capture_backend
        self.gpu_capture = None
        self.gpu_capture_disabled = capture_backend == 'dxcam' or os.name != 'nt'
        self.notify = lambda message: None

    def capture(self):
        started = time.perf_counter()
        if not self.synthetic and self.camera is None and not self.gpu_capture_disabled:
            try:
                from .gpu_capture import D3D11Capture
                from .cursor import WindowsCursor
                draw_mouse = self.cursor_mode != 'local'
                if self.gpu_capture and self.gpu_capture.draw_mouse != draw_mouse:
                    self.gpu_capture.close()
                    self.gpu_capture = None
                if self.gpu_capture is None:
                    self.gpu_capture = D3D11Capture(self.width, self.height, draw_mouse)
                    self.cursor = WindowsCursor()
                frame = self.gpu_capture.read()
                self.cursor_visible = self.cursor.visible()
                self.profile.elapsed('capture_gpu_readback', started)
                self.profile.describe(capture_backend='ddagrab-scale_d3d11', capture_format='nv12')
                return self.finish_capture(frame, started)
            except (av.FFmpegError, RuntimeError, ValueError, OSError) as exc:
                self.record_capture_error('capture', exc, self.width, self.height, self.fps)
                if self.gpu_capture:
                    self.gpu_capture.close()
                    self.gpu_capture = None
                if self.capture_backend == 'gpu':
                    self.notify(f'D3D11 采集失败：{exc}')
                    raise
                self.gpu_capture_disabled = True
                self.profile.describe(capture_fallback=type(exc).__name__)
                self.profile.event('capture_fallback', reason=type(exc).__name__)
                self.notify('D3D11 采集不可用，已回退兼容采集。')
        if self.synthetic:
            pixels = np.zeros((self.height, self.width, 3), dtype=np.uint8)
            pixels[:, :, 1] = self.frame_count % 255
            pixels[:, (self.frame_count * 8) % self.width:][:, :40, 2] = 255
        else:
            if self.camera is None:
                import dxcam
                if not getattr(self, "com_initialized", False):
                    self.com_initialized = ctypes.windll.ole32.CoInitializeEx(None, 2) in (0, 1)
                self.camera = dxcam.create(output_color="BGRA", max_buffer_len=2)
                # Let DXGI capture run continuously so a slow encode or color
                # conversion cannot stall the desktop producer. The sender
                # consumes the newest frame at the configured stream cadence.
                self.camera.start(target_fps=self.fps, video_mode=True)
                from .cursor import WindowsCursor
                self.cursor = WindowsCursor()
            if hasattr(self.camera, "get_latest_frame"):
                pixels = self.camera.get_latest_frame()
            else:
                # Keep lightweight fake cameras and older DXCam builds usable.
                pixels = self.camera.grab()
            if pixels is None and self.last is None:
                for _ in range(50):
                    time.sleep(0.01)
                    pixels = self.camera.grab()
                    if pixels is not None:
                        break
            if pixels is not None:
                self.last = pixels
            pixels = self.last
            if pixels is None:
                raise RuntimeError("无法采集当前桌面，请确认屏幕处于解锁状态。")
            output = self.camera._output.desc.DesktopCoordinates
            self.cursor_visible = self.cursor.visible() if hasattr(self.cursor, 'visible') else None
            if self.cursor_mode != 'local':
                pixels = self.cursor.composite(pixels, (output.left, output.top))
        self.profile.elapsed("capture_read_cursor", started)
        converted = time.perf_counter()
        # DXCam returns an owned array; PyAV holds its lifetime until conversion
        # finishes. Retain BGRA from DXGI instead of converting it to BGR first.
        pixels = np.ascontiguousarray(pixels)
        pixel_format = 'bgra' if pixels.shape[2] == 4 else 'bgr24'
        frame = av.VideoFrame.from_numpy_buffer(pixels, format=pixel_format)
        frame = self.reformatter.reformat(frame, width=self.width, height=self.height, format="yuv420p")
        self.profile.elapsed("capture_convert", converted)
        self.profile.describe(capture_backend='synthetic' if self.synthetic else 'dxcam', capture_format=pixel_format)
        return self.finish_capture(frame, started)

    def record_capture_error(self, stage, exc, width, height, fps):
        self.profile.exception(stage, exc, width=width, height=height, fps=fps,
                               requested_backend=self.capture_backend, draw_mouse=self.cursor_mode != 'local')
        log.error('D3D11 %s failed: output=%sx%s fps=%s requested_backend=%s',
                  stage, width, height, fps, self.capture_backend, exc_info=exc)

    def finish_capture(self, frame, started):
        frame.opaque = {"profile": self.profile, "captured_at": started, 'network_budget': self.network_budget,
                        'stream_fps': self.fps}
        self.profile.mark("capture_interval")
        self.profile.count('captured_frames')
        metrics["capture_ms"] = (time.perf_counter() - started) * 1000
        return frame

    async def reconfigure(self, width, height, fps):
        # All native capture objects belong to the single capture worker.
        # Already queued frames finish first and retain their original metadata.
        def apply():
            if self.readyState != 'live':
                raise MediaStreamError
            changed_size = (width, height) != (self.width, self.height)
            if changed_size and self.gpu_capture:
                from .gpu_capture import D3D11Capture
                draw_mouse = self.cursor_mode != 'local'
                previous, self.gpu_capture = self.gpu_capture, None
                previous.close()
                try:
                    self.gpu_capture = D3D11Capture(width, height, draw_mouse)
                    self.gpu_capture.read()
                except BaseException as exc:
                    if isinstance(exc, Exception):
                        self.record_capture_error('capture_reconfigure', exc, width, height, fps)
                    if self.gpu_capture:
                        self.gpu_capture.close()
                    try:
                        self.gpu_capture = D3D11Capture(self.width, self.height, draw_mouse)
                    except Exception as restore_error:
                        self.record_capture_error('capture_restore', restore_error, self.width, self.height, self.fps)
                        self.gpu_capture = None
                        self.gpu_capture_disabled = True
                        self.notify('D3D11 采集恢复失败，已回退兼容采集。')
                    raise
            if fps != self.fps and self.camera:
                self.camera.stop()
                try:
                    self.camera.start(target_fps=fps, video_mode=True)
                except BaseException:
                    self.camera.start(target_fps=self.fps, video_mode=True)
                    raise
            self.width, self.height, self.fps = width, height, fps
            self.frame_clock = FrameClock(fps)
        await asyncio.get_running_loop().run_in_executor(self.executor, apply)

    def scheduled_capture(self):
        late, skipped = self.frame_clock.wait()
        if self.readyState != 'live':
            raise MediaStreamError
        self.profile.observe('capture_deadline_late', late * 1000)
        self.profile.count('capture_slots_missed', skipped)
        return self.capture()

    async def recv(self):
        if self.readyState != "live":
            raise MediaStreamError
        now = time.perf_counter()
        if self.start_time is None:
            self.start_time = now
        loop = asyncio.get_running_loop()
        # Capture and color conversion can overlap the sender's H.264 encode:
        # while aiortc encodes the current frame, the single capture worker
        # prepares the next one. This removes the previous capture -> encode
        # bubble without allowing multiple capture calls to pile up.
        future = self.capture_future
        if future is None:
            future = loop.run_in_executor(self.executor, self.scheduled_capture)
        self.capture_future = None
        frame = await future
        if self.readyState != 'live':
            raise MediaStreamError
        self.capture_future = loop.run_in_executor(self.executor, self.scheduled_capture)
        # Timestamp the frame after the asynchronous capture completes, not
        # before a potentially long wait for the capture worker.
        frame.pts = max(self.last_pts + 1, int((time.perf_counter() - self.start_time) * 90000))
        self.last_pts = frame.pts
        frame.time_base = fractions.Fraction(1, 90000)
        self.frame_count += 1
        return frame

    def stop(self):
        if self.readyState != "live":
            return
        super().stop()
        if self.capture_future is not None:
            self.capture_future.cancel()
            self.capture_future = None
        def release():
            if self.gpu_capture:
                self.gpu_capture.close()
                self.gpu_capture = None
            if self.camera:
                self.camera.release()
                self.camera = None
            if getattr(self, "com_initialized", False):
                ctypes.windll.ole32.CoUninitialize()
        self.release_future = self.executor.submit(release)
        self.executor.shutdown(wait=False)


class LoopbackTrack(MediaStreamTrack):
    kind = "audio"

    def __init__(self, synthetic: bool = False, enabled=True):
        super().__init__()
        self.synthetic = synthetic
        self.samples = 0
        self.frames = collections.deque(maxlen=3)
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.error = ""
        self.enabled = threading.Event()
        self.set_enabled(enabled)
        self.thread = threading.Thread(target=self._capture, name="elink-loopback", daemon=True)
        self.thread.start()

    def _capture(self):
        try:
            if self.synthetic:
                while not self.done.wait(0.02):
                    if self.enabled.is_set():
                        self._put(np.zeros((960, 2), dtype=np.float32))
            else:
                while not self.done.is_set() and not self.enabled.wait(.1):
                    pass
                if self.done.is_set():
                    return
                import soundcard as sc
                # SoundCard initializes COM during its first import and rejects S_FALSE.
                # Balance our own initialization only after that import has succeeded.
                com_initialized = ctypes.windll.ole32.CoInitializeEx(None, 0) in (0, 1)
                speaker = sc.default_speaker()
                microphone = sc.get_microphone(id=speaker.id, include_loopback=True)
                while not self.done.is_set():
                    if not self.enabled.wait(.1):
                        continue
                    with microphone.recorder(samplerate=48000, channels=2, blocksize=960) as recorder:
                        while not self.done.is_set() and self.enabled.is_set():
                            self._put(recorder.record(numframes=960))
        except Exception as exc:
            self.error = f"系统声音采集失败：{exc}"
        finally:
            if not self.synthetic and locals().get("com_initialized", False):
                ctypes.windll.ole32.CoUninitialize()

    def _put(self, array):
        with self.lock:
            if self.enabled.is_set():
                self.frames.append(array)

    def set_enabled(self, enabled):
        self.enabled.set() if enabled else self.enabled.clear()
        with self.lock:
            self.frames.clear()

    async def recv(self):
        while self.readyState == "live":
            if self.error and self.enabled.is_set():
                raise RuntimeError(self.error)
            if not self.enabled.is_set():
                await asyncio.sleep(.02)
                item = np.zeros((960, 2), dtype=np.float32)
            else:
                with self.lock:
                    item = self.frames.popleft() if self.frames else None
            if item is not None:
                pts, array = self.samples, item
                self.samples += len(array)
                pcm = (np.clip(array, -1, 1) * 32767).astype(np.int16).reshape(1, -1)
                frame = av.AudioFrame.from_ndarray(pcm, format="s16", layout="stereo")
                frame.sample_rate, frame.pts = 48000, pts
                frame.time_base = fractions.Fraction(1, 48000)
                return frame
            await asyncio.sleep(0.004)
        raise MediaStreamError

    def stop(self):
        self.done.set()
        super().stop()


class AudioOutput:
    def __init__(self):
        import sounddevice as sd
        self.lock = threading.Lock()
        self.frames = collections.deque(maxlen=5)
        self.remainder = np.empty((0, 2), dtype=np.float32)
        self.muted = False
        self.resampler = av.AudioResampler(format="flt", layout="stereo", rate=48000)
        self.stream = sd.OutputStream(samplerate=48000, channels=2, dtype="float32", blocksize=480,
                                      latency="low", callback=self._callback)
        self.stream.start()

    def _callback(self, output, count, time_info, status):
        output.fill(0)
        offset = 0
        with self.lock:
            while offset < count:
                if not len(self.remainder):
                    if not self.frames:
                        break
                    self.remainder = self.frames.popleft()
                size = min(count - offset, len(self.remainder))
                output[offset:offset + size] = self.remainder[:size]
                self.remainder = self.remainder[size:]
                offset += size
            if self.muted:
                output.fill(0)

    def feed(self, frame):
        for converted in self.resampler.resample(frame):
            array = converted.to_ndarray().reshape(-1, 2)
            with self.lock:
                self.frames.append(array)

    def close(self):
        self.stream.stop()
        self.stream.close()


class FrameMailbox:
    """Only the newest decoded frame is retained, never a growing GUI event queue."""
    def __init__(self, profile=None):
        self.profile = profile or PipelineProfile()
        self.queued_at = 0.0
        self.lock = threading.Lock()
        self.frame = None
        self.received = 0
        self.dropped = 0
        self.updated = 0.0
        self.notifier = None

    def set_notifier(self, notifier):
        """Attach a nonblocking notification (e.g. an explicitly queued Qt signal).

        Invoke under the lock so detach guarantees no later calls into a deleted
        GUI object. The callback must never synchronously call take().
        """
        with self.lock:
            self.notifier = notifier
            if notifier is not None and self.frame is not None:
                notifier()

    def put(self, frame):
        with self.lock:
            was_empty = self.frame is None
            if self.frame is not None:
                self.dropped += 1
                self.profile.count("mailbox_dropped")
            self.frame = frame
            self.queued_at = time.perf_counter()
            self.received += 1
            self.updated = time.monotonic()
            if was_empty and self.notifier is not None:
                self.notifier()

    def take(self):
        with self.lock:
            frame, self.frame = self.frame, None
            if frame is not None:
                self.profile.elapsed("mailbox_wait", self.queued_at)
            return frame
