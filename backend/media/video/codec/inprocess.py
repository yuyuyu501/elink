"""In-process software and D3D11VA decoding through PyAV."""

from collections import deque
from fractions import Fraction
import sys
import time

import av
from av.codec.hwaccel import HWAccel

from backend.config.schema import VideoEncodeConfig
from backend.errors.codec import CodecOpenError, DecodeError
from backend.media.frame import Frame
from backend.media.timebase import Timebase
from .base import BaseDecoder


class InprocessDecoder(BaseDecoder):
    def __init__(self, stream, config: VideoEncodeConfig, *, hardware=False):
        super().__init__(stream)
        self.config = config
        self.codec = stream.codec.value
        self.hardware = hardware
        self.name = f"{self.codec}_pyav" + ("_d3d11va" if hardware else "")
        self._context = None
        self._proc = None
        self.output_callback = None
        self.decode_ms = deque(maxlen=4096)
        self.convert_ms = deque(maxlen=4096)

    def open(self):
        try:
            options = {}
            if self.hardware:
                if sys.platform != "win32":
                    raise ValueError("D3D11VA requires Windows")
                options["hwaccel"] = HWAccel("d3d11va", allow_software_fallback=False)
            implementation = self.codec
            if self.codec == "av1" and not self.hardware:
                implementation = "libdav1d" if "libdav1d" in av.codecs_available else "libaom-av1"
            context = av.CodecContext.create(implementation, "r", **options)
            if not self.hardware:
                context.thread_count = 4
                # Slice threading does not buffer future frames like frame threading.
                context.thread_type = "SLICE"
            context.flags |= 0x80000
            context.open()
            self._context = context
            self._opened = True
        except Exception as exc:
            raise CodecOpenError(f"无法打开 {self.name}", detail=str(exc)) from exc

    def decode(self, packet):
        if self._context is None:
            raise DecodeError("解码器尚未打开")
        encoded = av.Packet(packet.data)
        encoded.pts, encoded.dts = packet.pts, packet.dts
        encoded.time_base = Fraction(packet.timebase.num, packet.timebase.den)
        return self._decode(encoded)

    def _decode(self, packet):
        started = time.perf_counter()
        try:
            decoded = self._context.decode(packet)
        except av.FFmpegError as exc:
            raise DecodeError(f"{self.name} 解码失败", detail=str(exc)) from exc
        self.decode_ms.append((time.perf_counter() - started) * 1000)
        result = []
        for image in decoded:
            started = time.perf_counter()
            if self.hardware:
                if int(image.colorspace) == 2 and int(self._context.colorspace) != 2:
                    image.colorspace = self._context.colorspace
                if int(image.color_range) == 0 and int(self._context.color_range) in (1, 2):
                    image.color_range = self._context.color_range
            pixels = image.to_ndarray(format="bgra")
            pixels = memoryview(pixels).cast("B") if pixels.flags.c_contiguous else pixels.tobytes()
            timebase = Timebase.mpegts()
            pts = round((image.pts or 0) * (image.time_base or Fraction(1, 90000)) * 90000)
            frame = Frame(data=pixels, width=image.width, height=image.height,
                          pts=pts, timebase=timebase, stream_id=self.stream.stream_id)
            self.convert_ms.append((time.perf_counter() - started) * 1000)
            if self.output_callback is not None:
                self.output_callback(frame)
            else:
                result.append(frame)
        return result

    def flush(self):
        if self._context is None:
            return []
        return self._decode(None)

    def reset(self, packet):
        if self._context is not None:
            self._context.flush_buffers()

    def close(self):
        self._context = None
        self._opened = False

    def diagnostics(self):
        return {"backend": self.name, "hardware": self.hardware,
                "threads": self._context.thread_count if self._context is not None else None}

    def _stderr_text(self):
        return ""
