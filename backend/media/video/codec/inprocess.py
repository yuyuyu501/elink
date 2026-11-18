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
from .base import BaseDecoder, BaseEncoder


class InprocessDecoder(BaseDecoder):
    def __init__(self, stream, config: VideoEncodeConfig | None = None, *, hardware=False):
        super().__init__(stream)
        self.config = config or VideoEncodeConfig()
        self.codec = stream.codec.value
        self.hardware = hardware
        self.name = (f"{self.codec}_pyav_d3d11va" if hardware else
                     "h264" if self.codec == "h264" else f"{self.codec}_pyav")
        self._context = None
        self._proc = None
        self.output_callback = None
        self.native_output = False
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
            frame = Frame.from_av(image, native=self.native_output)
            frame.stream_id = self.stream.stream_id
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


class InprocessEncoder(BaseEncoder):
    """Use the same selected codec for SRT and WebRTC without rawvideo pipes."""

    def __init__(self, stream, config):
        super().__init__(stream)
        self.config = config
        self.name = config.encoder
        self.codec = stream.codec.value
        self._context = self._proc = None
        self.force_keyframe = False
        self.target_bitrate = config.bitrate_kbps * 1000

    def open(self):
        try:
            context = av.CodecContext.create(self.name, "w")
            context.width, context.height = self.stream.width, self.stream.height
            context.pix_fmt = "yuv420p"
            if (self.name.endswith(("_nvenc", "_qsv", "_amf"))
                    and any(fmt.name == "nv12" for fmt in context.codec.video_formats or [])):
                context.pix_fmt = "nv12"
            context.time_base = Fraction(1, 90000)
            context.framerate = Fraction(self.stream.framerate or 30).limit_denominator(1000)
            context.bit_rate = self.target_bitrate
            context.gop_size = self.config.gop
            context.max_b_frames = 0
            rate = max(300000, round(2 * self.target_bitrate / float(context.framerate)))
            options = {"maxrate": str(self.target_bitrate), "bufsize": str(rate)}
            if self.codec == "h264":
                options.update(profile="high", level="5.2")
            if self.name.endswith("_nvenc"):
                options.update(preset="p1", tune="ull", zerolatency="1", delay="0",
                               rc="cbr", **{"rc-lookahead": "0", "forced-idr": "1"})
            elif self.name.endswith("_amf"):
                options.update(usage="ultralowlatency", quality="speed", rc="cbr")
            elif self.name.endswith("_qsv"):
                options.update(preset="veryfast", async_depth="1", look_ahead="0")
            elif self.name == "libx264":
                context.thread_count = 4
                options.update(preset="ultrafast", tune="zerolatency",
                               **{"x264-params": "scenecut=0:repeat-headers=1"})
            elif self.name == "libx265":
                context.thread_count = 4
                options.update(preset="ultrafast", tune="zerolatency",
                               **{"x265-params": "bframes=0:rc-lookahead=0:repeat-headers=1:pools=4"})
            elif self.name == "libsvtav1":
                context.thread_count = 4
                options.update(preset="12", **{"svtav1-params": "lp=4:pred-struct=1"})
            context.options = options
            self._context = context
            self._opened = True
        except Exception as exc:
            raise CodecOpenError(f"无法打开 {self.name}", detail=str(exc)) from exc

    def encode(self, frame):
        image = frame.to_av().reformat(width=self.stream.width, height=self.stream.height,
                                       format=self._context.pix_fmt)
        if int(image.colorspace) in (1, 5, 6):
            self._context.colorspace = image.colorspace
        if int(image.color_range) in (1, 2):
            self._context.color_range = image.color_range
        if not self._context.is_open:
            self._context.open()
        if self.force_keyframe:
            image.pict_type = av.video.frame.PictureType.I
            self.force_keyframe = False
        else:
            image.pict_type = av.video.frame.PictureType.NONE
        self._context.bit_rate = min(self.config.bitrate_kbps * 1000, self.target_bitrate)
        return self._packets(self._context.encode(image))

    def _packets(self, packets):
        from backend.media.packet import Packet
        return [Packet(data=bytes(p), pts=p.pts, dts=p.dts, codec=self.stream.codec,
                       timebase=Timebase.mpegts(), keyframe=p.is_keyframe) for p in packets]

    def flush(self):
        return (self._packets(self._context.encode(None))
                if self._context is not None and self._context.is_open else [])

    def close(self):
        self._context = None
        self._opened = False

    def diagnostics(self):
        return {"backend": self.name, "implementation": "pyav", "bitrate": self.target_bitrate}

    def _stderr_text(self):
        return ""
