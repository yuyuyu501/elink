"""Low-delay hardware codec adapters for the pinned aiortc codec factory API."""
from __future__ import annotations

import fractions
import logging
import time
from dataclasses import dataclass

import av
from aiortc import rtcrtpreceiver, rtcrtpsender
from aiortc.codecs import get_decoder, get_encoder
from aiortc.codecs.h264 import H264Decoder, H264Encoder
from av.codec.hwaccel import HWAccel


log = logging.getLogger(__name__)


@dataclass
class CodecPolicy:
    fps: int = 60
    bitrate: int = 20_000_000
    encoder: str = "auto"
    decoder: str = "auto"
    fixed_priority: bool = True


encode_policy = CodecPolicy()
decode_policy = CodecPolicy()
metrics = {"encoder": "未启动", "decoder": "未启动", "encode_ms": 0.0, "decode_ms": 0.0,
           "encoded": 0, "decoded": 0, "decode_errors": 0, "capture_ms": 0.0, "bitrate": 0,
           "convert_ms": 0.0, "present_ms": 0.0, "last_decode_error": ""}


def rate_control_options(policy: CodecPolicy) -> dict[str, str]:
    """Return a low-latency CBR/VBV budget shared by supported encoders."""
    return {
        "maxrate": str(policy.bitrate),
        # Request a small VBV budget rather than half a second. This is a rate
        # control hint, not a hard per-frame size cap (NVENC IDRs can exceed it).
        "bufsize": str(max(2 * policy.bitrate // policy.fps, 300_000)),
    }


def make_encoder(name: str, width: int, height: int, policy: CodecPolicy, pixel_format='yuv420p',
                 colorspace=None, color_range=None):
    codec = av.CodecContext.create(name, "w")
    codec.width, codec.height = width, height
    codec.pix_fmt = pixel_format if name in ('h264_nvenc', 'h264_amf', 'h264_qsv') else 'yuv420p'
    if colorspace in (1, 5, 6):
        codec.colorspace = colorspace
    if color_range in (1, 2):
        codec.color_range = color_range
    codec.framerate = fractions.Fraction(policy.fps, 1)
    codec.time_base = fractions.Fraction(1, 90000)
    codec.bit_rate = policy.bitrate
    # NVENC supports explicit IDRs for RTCP PLI/FIR. Avoid a large mandatory
    # IDR every second, which stalls a paced stream on high-detail scenes.
    # Retain a 10-second recovery fallback; other encoders keep their tested GOP.
    codec.gop_size = policy.fps * (10 if name == 'h264_nvenc' else 1)
    codec.max_b_frames = 0
    # Software H.264 benefits from a small amount of frame/slice parallelism.
    # Hardware encoders have their own scheduler and should keep the driver
    # default rather than receiving a software thread hint.
    if name not in ("h264_nvenc", "h264_amf", "h264_qsv"):
        codec.thread_count = 4
    common = rate_control_options(policy)
    if name == "h264_nvenc":
        codec.options = {**common, "preset": "p1", "tune": "ull", "zerolatency": "1", "delay": "0",
                         "rc": "cbr", "rc-lookahead": "0", "forced-idr": "1", "strict_gop": "1",
                         "profile": "high"}
    elif name == "h264_amf":
        codec.options = {**common, "usage": "ultralowlatency", "quality": "speed", "rc": "cbr", "profile": "high"}
    elif name == "h264_qsv":
        codec.options = {**common, "preset": "veryfast", "async_depth": "1", "look_ahead": "0", "profile": "high"}
    else:
        codec.options = {**common, "preset": "ultrafast", "tune": "zerolatency", "profile": "high",
                         "x264-params": "scenecut=0:repeat-headers=1:cabac=1"}
    codec.open()
    return codec


class DesktopEncoder(H264Encoder):
    def __init__(self, policy: CodecPolicy):
        self.policy = CodecPolicy(**vars(policy))
        self._rate = self.policy.bitrate
        self.name = ""
        self.input_layout = None
        super().__init__()

    @property
    def target_bitrate(self):
        return self._rate

    @target_bitrate.setter
    def target_bitrate(self, value):
        # aiortc's REMB is an estimate of *observed media throughput*. On a
        # mostly static desktop that estimate can be a few hundred kbps even
        # when the path has ample capacity. Treating it as the encoder target
        # creates a feedback loop: REMB lowers the codec, then the pacer lowers
        # the frame rate, making the next estimate even smaller. Fixed priority
        # keeps the user's configured target; the encoder's CBR settings keep
        # quality anchored to that budget.
        if self.policy.fixed_priority:
            self._rate = self.policy.bitrate
            return
        self._rate = max(300_000, min(int(value), self.policy.bitrate))

    def _encode_frame(self, frame, force_keyframe):
        # aiortc invokes encoders sequentially. Network pacing is applied at
        # the RTP transport, so the encoder must return promptly and avoid a
        # second frame-level delay that would halve the effective frame rate.
        started = time.perf_counter()
        trace = frame.opaque if isinstance(frame.opaque, dict) else {}
        profile = trace.get('profile')
        if profile:
            profile.elapsed('capture_to_encode', trace['captured_at'])
        budget = trace.get('network_budget')
        rate = min(self._rate, budget.media_bps) if budget else self._rate
        layout = frame.format.name, int(frame.colorspace), int(frame.color_range)
        changed = self.codec and (self.codec.width != frame.width or self.codec.height != frame.height
                                  or rate != self.codec.bit_rate
                                  or self.input_layout is not None and self.input_layout != layout)
        if changed:
            self.codec = None
        if self.codec is None:
            choices = [self.name] if self.name else (["h264_nvenc", "h264_amf", "h264_qsv", "libx264"]
                                                    if self.policy.encoder == "auto" else [self.policy.encoder])
            for name in choices:
                try:
                    policy = CodecPolicy(self.policy.fps, rate, name)
                    self.codec = make_encoder(name, frame.width, frame.height, policy, frame.format.name,
                                              int(frame.colorspace), int(frame.color_range))
                    self.name = name
                    self.input_layout = layout
                    break
                except (av.FFmpegError, ValueError):
                    log.info("Encoder unavailable: %s", name)
            if self.codec is None:
                raise RuntimeError("没有可用的 H.264 编码器。")
            force_keyframe = True
        frame.pict_type = av.video.frame.PictureType.I if force_keyframe else av.video.frame.PictureType.NONE
        data = b"".join(bytes(packet) for packet in self.codec.encode(frame))
        metrics.update(encoder=self.name, encode_ms=(time.perf_counter() - started) * 1000,
                       encoded=metrics["encoded"] + 1, bitrate=rate)
        if profile:
            profile.elapsed('encode', started)
            profile.mark('encode_interval')
            profile.describe(encoder=self.name)
            profile.count('encoded_frames')
            profile.count('encoded_bytes', len(data))
            media_ms = len(data) * 8 * 1000 / rate
            profile.observe('encoded_media_time', media_ms)
            if media_ms > 2000 / self.policy.fps:
                profile.event('large_encoded_frame', bytes=len(data), media_ms=round(media_ms, 3), keyframe=bool(force_keyframe))
            if force_keyframe:
                profile.count('keyframe_requests')
        if data:
            yield from self._split_bitstream(data)


class DesktopDecoder(H264Decoder):
    def __init__(self, policy: CodecPolicy):
        self.mode = policy.decoder
        self.hardware = False
        if self.mode != "software":
            try:
                self.codec = av.CodecContext.create("h264", "r", hwaccel=HWAccel("d3d11va", allow_software_fallback=False))
                self.hardware = True
            except (av.FFmpegError, ValueError):
                if self.mode == "hardware":
                    raise
                self.codec = av.CodecContext.create("h264", "r")
        else:
            self.codec = av.CodecContext.create("h264", "r")
        # Software decoding can use four worker threads. D3D11VA keeps its
        # driver-managed scheduling and must not receive this hint.
        if not self.hardware:
            self.codec.thread_count = 4
        self.codec.flags |= 0x80000  # AV_CODEC_FLAG_LOW_DELAY

    def decode(self, encoded_frame):
        started = time.perf_counter()
        profile = getattr(encoded_frame, '_elink_profile', None)
        if profile:
            profile.elapsed('decode_queue_wait', encoded_frame._elink_queued_at)
        packet = av.Packet(encoded_frame.data)
        packet.pts = encoded_frame.timestamp
        packet.time_base = fractions.Fraction(1, 90000)
        try:
            frames = self.codec.decode(packet)
        except av.FFmpegError as exc:
            metrics["decode_errors"] += 1
            metrics["last_decode_error"] = f"{type(exc).__name__}: {exc}"[:500]
            if profile:
                profile.count("decode_errors")
                profile.describe(last_decode_error=metrics["last_decode_error"])
            if self.hardware and self.mode == "auto":
                self.hardware = False
                self.codec = av.CodecContext.create("h264", "r")
                self.codec.thread_count = 4
                try:
                    frames = self.codec.decode(packet)
                except av.FFmpegError:
                    return []
            else:
                return []
        if self.hardware:
            # PyAV's hardware download copies pixels/PTS but can drop color
            # properties. Restore missing H.264 VUI metadata from the decoder;
            # otherwise full-range NV12 is expanded as limited-range video.
            for frame in frames:
                if int(frame.colorspace) == 2 and int(self.codec.colorspace) != 2:
                    frame.colorspace = self.codec.colorspace
                if int(frame.color_range) == 0 and int(self.codec.color_range) in (1, 2):
                    frame.color_range = self.codec.color_range
        metrics.update(decoder="D3D11VA" if self.hardware else "软件 H.264",
                       decode_ms=(time.perf_counter() - started) * 1000, decoded=metrics["decoded"] + len(frames))
        if profile:
            profile.elapsed('decode', started)
            profile.describe(decoder='D3D11VA' if self.hardware else 'software-h264')
            if frames:
                profile.describe(decoder_colorspace=int(frames[-1].colorspace),
                                 decoder_color_range=int(frames[-1].color_range),
                                 decoder_pixel_format=frames[-1].format.name)
            for frame in frames:
                frame.opaque = {'profile': profile, 'decoded_at': time.perf_counter()}
                profile.count('decoded_frames')
        return frames


def install_factories():
    # aiortc 1.14 exposes factories at these module boundaries. One outbound host
    # session and one inbound client session are allowed; each codec copies its policy.
    rtcrtpsender.get_encoder = lambda codec: DesktopEncoder(encode_policy) if codec.mimeType.lower() == "video/h264" else get_encoder(codec)
    rtcrtpreceiver.get_decoder = lambda codec: DesktopDecoder(decode_policy) if codec.mimeType.lower() == "video/h264" else get_decoder(codec)
