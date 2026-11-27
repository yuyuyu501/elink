"""In-process video codecs through PyAV."""

from collections import deque
from fractions import Fraction
from concurrent.futures import Future
import queue
import time

import av

from backend.config.schema import VideoEncodeConfig
from backend.errors.codec import CodecOpenError, DecodeError
from backend.media.frame import Frame
from backend.media.timebase import Timebase
from .base import BaseDecoder, BaseEncoder


class InprocessDecoder(BaseDecoder):
    def __init__(self, stream, config: VideoEncodeConfig | None = None, *, implementation=None):
        super().__init__(stream)
        self.config = config or VideoEncodeConfig()
        self.codec = stream.codec.value
        self.name = implementation or ("h264" if self.codec == "h264" else f"{self.codec}_pyav")
        self.hardware = implementation is not None
        self.implementation = implementation
        self._dimensions = (stream.width, stream.height)
        self._context = None
        self._proc = None
        self.output_callback = None
        self.native_output = False
        self.decode_ms = deque(maxlen=4096)
        self.convert_ms = deque(maxlen=4096)

    def open(self):
        try:
            implementation = self.implementation or self.codec
            if self.codec == "av1" and not self.hardware:
                implementation = "libdav1d" if "libdav1d" in av.codecs_available else "libaom-av1"
            context = av.CodecContext.create(implementation, "r")
            if not self.hardware:
                context.thread_count = 4
                context.thread_type = "SLICE"
            elif self.name.endswith("_cuvid"):
                context.options = {"surfaces": "8", "delay": "0", "deint": "weave"}
            context.flags |= 0x80000
            context.open()
            self._context = context
            self._opened = True
        except Exception as exc:
            raise CodecOpenError(f"无法打开 {self.name}", detail=str(exc)) from exc

    def decode(self, packet):
        if self._context is None:
            raise DecodeError("解码器尚未打开")
        if self.hardware and packet.keyframe:
            from .probe import detect_video_dimensions
            dimensions = detect_video_dimensions(packet.data, packet.codec)
            if dimensions and dimensions != self._dimensions:
                self._context = None
                self.open()
                self._dimensions = dimensions
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
    """Encode video in-process without rawvideo pipes."""

    def __init__(self, stream, config, *, amf_adapter=None, delegate=None, replacement=None):
        super().__init__(stream)
        self.config = config
        self.name = config.encoder
        self.codec = stream.codec.value
        self._context = None
        self._amf_adapter = amf_adapter
        self._delegate = delegate
        self._replacement = replacement
        self.force_keyframe = False
        self.target_bitrate = config.bitrate_kbps * 1000
        self._updates = queue.Queue(maxsize=1)

    def request_update(self, stream, config):
        future = Future()
        self._updates.put_nowait((stream, config, future))
        return future

    @property
    def _proc(self):
        return self._delegate._proc if self._delegate is not None else None

    @property
    def supports_on_demand_keyframes(self):
        return True

    def open(self):
        try:
            if self._delegate is not None:
                self._delegate.open()
                self._opened = True
                return
            accel = None
            if self._amf_adapter is not None:
                accel = av.codec.hwaccel.HWAccel("d3d11va", device=str(self._amf_adapter),
                                                 allow_software_fallback=False)
            context = av.CodecContext.create(self.name, "w", hwaccel=accel)
            context.width, context.height = self.stream.width, self.stream.height
            context.pix_fmt = "yuv420p"
            if (self.name.endswith(("_nvenc", "_qsv", "_amf"))
                    and any(fmt.name == "nv12" for fmt in context.codec.video_formats or [])):
                context.pix_fmt = "nv12"
            context.time_base = Fraction(1, 90000)
            context.framerate = Fraction(self.stream.framerate or 30).limit_denominator(1000)
            context.bit_rate = self.target_bitrate
            on_demand = self.config.on_demand_keyframes
            context.gop_size = 2**31 - 1 if on_demand else self.config.gop
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
            if on_demand:
                if self.name == "libx264":
                    options["x264-params"] += ":keyint=infinite"
                    options["forced-idr"] = "1"
                elif self.name == "libx265":
                    options["x265-params"] += ":keyint=-1:scenecut=0:open-gop=0:frame-threads=1"
                    options["forced-idr"] = "1"
                elif self.name == "libsvtav1":
                    context.gop_size = -1
                    options["svtav1-params"] += ":keyint=-1:scd=0:force-key-frames=1"
                elif self.name.endswith("_amf"):
                    options["preanalysis"] = "0"
                elif self.name.endswith("_qsv"):
                    options.update(adaptive_i="0", forced_idr="1")
            context.options = options
            self._context = context
            self._opened = True
        except Exception as exc:
            raise CodecOpenError(f"无法打开 {self.name}", detail=str(exc)) from exc

    def encode(self, frame):
        try:
            stream, config, future = self._updates.get_nowait()
        except queue.Empty:
            pass
        else:
            if future.set_running_or_notify_cancel():
                replacement = None
                try:
                    replacement = (self._replacement(stream, config) if self._replacement is not None
                                   else InprocessEncoder(stream, config))
                    replacement.open()
                    replacement.force_keyframe = True
                    packets = replacement.encode(frame)
                    if not packets or not any(p.keyframe for p in packets):
                        raise RuntimeError("新编码器未生成关键帧")
                except Exception as exc:
                    if replacement is not None:
                        replacement.close()
                    future.set_exception(exc)
                else:
                    self.close()
                    self._context = replacement._context
                    self._amf_adapter, self._delegate = replacement._amf_adapter, replacement._delegate
                    self.config, self.stream, self.name = config, stream, config.encoder
                    self.target_bitrate = config.bitrate_kbps * 1000
                    self.force_keyframe = False
                    self._opened = True
                    future.set_result(None)
                    return packets
        if self._delegate is not None:
            self._delegate.force_keyframe = self.force_keyframe
            self._delegate.target_bitrate = self.target_bitrate
            self.force_keyframe = False
            return self._delegate.encode(frame)
        image = frame.to_av().reformat(width=self.stream.width, height=self.stream.height,
                                       format=self._context.pix_fmt)
        if int(image.colorspace) in (1, 5, 6):
            self._context.colorspace = image.colorspace
        if int(image.color_range) in (1, 2):
            self._context.color_range = image.color_range
        if not self._context.is_open:
            self._context.open()
        requested = self.force_keyframe
        if requested:
            image.pict_type = av.video.frame.PictureType.I
            self.force_keyframe = False
        else:
            image.pict_type = av.video.frame.PictureType.NONE
        # NVENC reconfigures bitrate by forcing an IDR; combine it with a requested refresh.
        if requested or not (self.config.on_demand_keyframes and self.name.endswith("_nvenc")):
            self._context.bit_rate = min(self.config.bitrate_kbps * 1000, self.target_bitrate)
        return self._packets(self._context.encode(image))

    def _packets(self, packets):
        from backend.media.packet import Packet
        return [Packet(data=bytes(p), pts=p.pts, dts=p.dts, codec=self.stream.codec,
                       timebase=Timebase.mpegts(), keyframe=p.is_keyframe) for p in packets]

    def flush(self):
        if self._delegate is not None:
            return self._delegate.flush()
        return (self._packets(self._context.encode(None))
                if self._context is not None and self._context.is_open else [])

    def close(self):
        if self._delegate is not None:
            self._delegate.close()
        self._context = None
        self._opened = False
        while not self._updates.empty():
            _, _, future = self._updates.get_nowait()
            future.cancel()

    def diagnostics(self):
        diagnostic = self._delegate.diagnostics() if self._delegate is not None else {}
        return {**diagnostic, "backend": self.name, "bitrate": self.target_bitrate,
                "implementation": "ffmpeg" if self._delegate is not None else "pyav",
                "adapter": self._amf_adapter}

    def _stderr_text(self):
        return self._delegate._stderr_text() if self._delegate is not None else ""
