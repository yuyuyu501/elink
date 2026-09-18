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


encode_policy = CodecPolicy()
decode_policy = CodecPolicy()
metrics = {"encoder": "未启动", "decoder": "未启动", "encode_ms": 0.0, "decode_ms": 0.0,
           "encoded": 0, "decoded": 0, "decode_errors": 0, "capture_ms": 0.0, "bitrate": 0,
           "last_decode_error": ""}


def make_encoder(name: str, width: int, height: int, policy: CodecPolicy):
    codec = av.CodecContext.create(name, "w")
    codec.width, codec.height = width, height
    codec.pix_fmt = "yuv420p"
    codec.framerate = fractions.Fraction(policy.fps, 1)
    codec.time_base = fractions.Fraction(1, 90000)
    codec.bit_rate = policy.bitrate
    codec.gop_size = policy.fps
    codec.max_b_frames = 0
    codec.thread_count = 2
    if name == "h264_nvenc":
        codec.options = {"preset": "p1", "tune": "ull", "zerolatency": "1", "delay": "0",
                         "rc": "cbr", "rc-lookahead": "0", "forced-idr": "1", "profile": "baseline"}
    elif name == "h264_amf":
        codec.options = {"usage": "ultralowlatency", "quality": "speed", "rc": "cbr", "profile": "baseline"}
    elif name == "h264_qsv":
        codec.options = {"preset": "veryfast", "async_depth": "1", "look_ahead": "0", "profile": "baseline"}
    else:
        codec.options = {"preset": "ultrafast", "tune": "zerolatency", "profile": "baseline",
                         "x264-params": "scenecut=0:repeat-headers=1"}
    codec.open()
    return codec


class DesktopEncoder(H264Encoder):
    def __init__(self, policy: CodecPolicy):
        self.policy = CodecPolicy(**vars(policy))
        self._rate = self.policy.bitrate
        self.name = ""
        super().__init__()

    @property
    def target_bitrate(self):
        return self._rate

    @target_bitrate.setter
    def target_bitrate(self, value):
        # Respect REMB congestion feedback, bounded by the user's requested ceiling.
        self._rate = max(300_000, min(int(value), self.policy.bitrate))

    def _encode_frame(self, frame, force_keyframe):
        started = time.perf_counter()
        changed = self.codec and (self.codec.width != frame.width or self.codec.height != frame.height
                                  or abs(self._rate - self.codec.bit_rate) / self.codec.bit_rate > 0.25)
        if changed:
            self.codec = None
        if self.codec is None:
            choices = [self.name] if self.name else (["h264_nvenc", "h264_amf", "h264_qsv", "libx264"]
                                                    if self.policy.encoder == "auto" else [self.policy.encoder])
            for name in choices:
                try:
                    policy = CodecPolicy(self.policy.fps, self._rate, name)
                    self.codec = make_encoder(name, frame.width, frame.height, policy)
                    self.name = name
                    break
                except av.FFmpegError:
                    log.info("Encoder unavailable: %s", name)
            if self.codec is None:
                raise RuntimeError("没有可用的 H.264 编码器。")
            force_keyframe = True
        frame.pict_type = av.video.frame.PictureType.I if force_keyframe else av.video.frame.PictureType.NONE
        data = b"".join(bytes(packet) for packet in self.codec.encode(frame))
        metrics.update(encoder=self.name, encode_ms=(time.perf_counter() - started) * 1000,
                       encoded=metrics["encoded"] + 1, bitrate=self._rate)
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
        self.codec.thread_count = 1
        self.codec.flags |= 0x80000  # AV_CODEC_FLAG_LOW_DELAY

    def decode(self, encoded_frame):
        started = time.perf_counter()
        packet = av.Packet(encoded_frame.data)
        packet.pts = encoded_frame.timestamp
        packet.time_base = fractions.Fraction(1, 90000)
        try:
            frames = self.codec.decode(packet)
        except av.FFmpegError as exc:
            metrics["decode_errors"] += 1
            metrics["last_decode_error"] = f"{type(exc).__name__}: {exc}"[:500]
            if self.hardware and self.mode == "auto":
                self.hardware = False
                self.codec = av.CodecContext.create("h264", "r")
                self.codec.thread_count = 1
                try:
                    frames = self.codec.decode(packet)
                except av.FFmpegError:
                    return []
            else:
                return []
        metrics.update(decoder="D3D11VA" if self.hardware else "软件 H.264",
                       decode_ms=(time.perf_counter() - started) * 1000, decoded=metrics["decoded"] + len(frames))
        return frames


def install_factories():
    # aiortc 1.14 exposes factories at these module boundaries. One outbound host
    # session and one inbound client session are allowed; each codec copies its policy.
    rtcrtpsender.get_encoder = lambda codec: DesktopEncoder(encode_policy) if codec.mimeType.lower() == "video/h264" else get_encoder(codec)
    rtcrtpreceiver.get_decoder = lambda codec: DesktopDecoder(decode_policy) if codec.mimeType.lower() == "video/h264" else get_decoder(codec)
