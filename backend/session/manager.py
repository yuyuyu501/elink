"""会话生命周期与能力协商。

协商只在这里发生一次，结果写进 ``SessionState``；pipeline 只消费协商结果，
不做二次选型（见 PROJECT_STRUCTURE 的「能力协商」）。

回退规则（老设备很常见的情况）：

1. 选定的硬件编码器打开失败 → 退回同编码的软件编码器**重试一次**；
2. 双方编码格式无交集 → 抛 ``CapabilityMismatchError``，由上层提示用户；
3. 重连复用已协商结果，不重新协商（避免对端负载突增导致选型漂移）。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from backend.config.schema import VideoEncodeConfig
from backend.errors.codec import CodecError
from backend.errors.network import CapabilityMismatchError
from backend.logging.logger import get_logger
from backend.media.stream import CodecId, StreamInfo
from backend.media.video.codec.base import BaseDecoder, BaseEncoder
from backend.registry.codec_registry import CodecRegistry
from backend.session.reconnect import BackoffPolicy, ReconnectManager
from backend.session.state import SessionState, SessionStateMachine

logger = get_logger(__name__)


@dataclass
class Capabilities:
    """一端的能力集合。"""

    codecs: tuple[str, ...] = ("h264",)
    encoders: tuple[str, ...] = ()
    max_width: int = 1920
    max_height: int = 1080
    max_fps: int = 60
    audio_codecs: tuple[str, ...] = ("aac",)
    hardware: bool = False

    def to_dict(self) -> dict:
        return {
            "codecs": list(self.codecs),
            "encoders": list(self.encoders),
            "max_width": self.max_width,
            "max_height": self.max_height,
            "max_fps": self.max_fps,
            "audio_codecs": list(self.audio_codecs),
            "hardware": self.hardware,
        }


@dataclass
class Negotiated:
    """协商结果。"""

    codec: CodecId
    encoder: str
    decoder: str
    width: int
    height: int
    fps: int
    audio_codec: str = ""
    session_id: str = ""

    def describe(self) -> str:
        return (
            f"{self.codec.value} {self.width}x{self.height}@{self.fps} "
            f"enc={self.encoder} dec={self.decoder}"
            + (f" audio={self.audio_codec}" if self.audio_codec else "")
        )


_CODEC_PREFERENCE = ("av1", "hevc", "h264")
_CODEC_MAP = {
    "h264": CodecId.H264,
    "avc": CodecId.H264,
    "hevc": CodecId.H265,
    "h265": CodecId.H265,
    "av1": CodecId.AV1,
}


@dataclass
class SessionManager:
    """一个会话的编排者。"""

    registry: CodecRegistry
    session_id: str = ""
    role: str = "publisher"
    encode_config: VideoEncodeConfig = field(default_factory=VideoEncodeConfig)
    reconnect: ReconnectManager = field(default_factory=ReconnectManager)
    state: SessionStateMachine = field(default_factory=SessionStateMachine)
    negotiated: Negotiated | None = None
    created_at: float = field(default_factory=time.time)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    # ---- 能力 ----
    def local_capabilities(self, max_width: int = 1920, max_height: int = 1080) -> Capabilities:
        """汇总本端可用的编码格式与后端。"""
        codecs: list[str] = []
        encoders: list[str] = []
        for codec_name in ("h264", "hevc", "av1"):
            names = self.registry.encoder_names(codec_name)
            if names:
                codecs.append(codec_name)
                encoders.extend(names)
        return Capabilities(
            codecs=tuple(codecs) or ("h264",),
            encoders=tuple(encoders),
            max_width=max_width,
            max_height=max_height,
            max_fps=60,
            audio_codecs=("aac",),
            hardware=bool(self.encode_config.encoder not in {"libx264", "libx265", "libsvtav1"}),
        )

    # ---- 协商 ----
    def negotiate(
        self,
        remote: Capabilities,
        *,
        preferred_size: tuple[int, int] = (1920, 1080),
        preferred_fps: int = 60,
    ) -> Negotiated:
        local = self.local_capabilities()
        common = {
            _CODEC_MAP[name.lower()]
            for name in remote.codecs
            if name.lower() in _CODEC_MAP
        } & {
            _CODEC_MAP[name.lower()]
            for name in local.codecs
            if name.lower() in _CODEC_MAP
        }
        if not common:
            raise CapabilityMismatchError(
                "双方没有共同支持的视频编码",
                detail=f"local={local.codecs} remote={remote.codecs}",
            )

        # 注意 CodecId 的成员名是 H264/H265/AV1，"hevc" 要通过 _CODEC_MAP 映射
        codec = next(
            (_CODEC_MAP[name] for name in _CODEC_PREFERENCE if _CODEC_MAP[name] in common),
            CodecId.H264,
        )

        encoders = self.registry.encoder_names(codec.value)
        if not encoders:
            raise CapabilityMismatchError(
                f"本端没有可用的 {codec.value} 编码器", detail=str(local.codecs)
            )
        decoder_name = (
            self.registry.decoder_names(codec.value) or [codec.value]
        )[0]

        width = min(local.max_width, remote.max_width, preferred_size[0]) & ~1
        height = min(local.max_height, remote.max_height, preferred_size[1]) & ~1
        fps = min(local.max_fps, remote.max_fps, preferred_fps)

        negotiated = Negotiated(
            codec=codec,
            encoder=encoders[0],
            decoder=decoder_name,
            width=max(2, width),
            height=max(2, height),
            fps=max(1, fps),
            audio_codec=(
                (set(local.audio_codecs) & set(remote.audio_codecs)) and
                sorted(set(local.audio_codecs) & set(remote.audio_codecs))[0]
            ) or "",
            session_id=self.session_id,
        )
        with self._lock:
            self.negotiated = negotiated
        logger.info("协商完成: %s", negotiated.describe())
        return negotiated

    # ---- 实现绑定 ----
    def create_encoder(self, stream: StreamInfo) -> BaseEncoder:
        """按协商结果创建编码器；硬件打开失败时回退软件一次。"""
        with self._lock:
            negotiated = self.negotiated
        if negotiated is None:
            raise CapabilityMismatchError("尚未协商，不能创建编码器")

        candidates = [negotiated.encoder]
        software = {
            CodecId.H264: "libx264",
            CodecId.H265: "libx265",
            CodecId.AV1: "libsvtav1",
        }.get(negotiated.codec, "libx264")
        if software != negotiated.encoder:
            candidates.append(software)

        last_error: Exception | None = None
        for name in candidates:
            encoder = self.registry.create_encoder(
                negotiated.codec.value, name, stream, self.encode_config
            )
            try:
                encoder.open()
            except CodecError as exc:
                last_error = exc
                logger.warning("编码器 %s 打开失败，尝试回退: %s", name, exc)
                encoder.close()
                continue
            if name != negotiated.encoder:
                logger.info("已回退到软件编码器: %s", name)
                with self._lock:
                    negotiated.encoder = name
            return encoder

        raise CodecError("所有编码器都打开失败", detail=str(last_error))

    def create_decoder(self, stream: StreamInfo) -> BaseDecoder:
        with self._lock:
            negotiated = self.negotiated
        if negotiated is None:
            raise CapabilityMismatchError("尚未协商，不能创建解码器")
        decoder = self.registry.create_decoder(
            negotiated.codec.value, negotiated.decoder, stream, self.encode_config
        )
        decoder.open()
        return decoder

    # ---- 生命周期 ----
    def open(self) -> None:
        self.state.transition(SessionState.CONNECTING, "建立连接")
        self.state.transition(SessionState.NEGOTIATING, "交换能力")

    def streaming(self) -> None:
        self.state.transition(SessionState.STREAMING, "媒体已就绪")

    def on_disconnected(self, reason: str = "") -> None:
        self.reconnect.on_disconnected()
        if self.state.can_transition(SessionState.RECONNECTING):
            self.state.transition(SessionState.RECONNECTING, reason or "断线")
        if not self.reconnect.should_retry():
            self.state.transition(SessionState.FAILED, "重连次数耗尽")

    def close(self, reason: str = "正常结束") -> None:
        if self.state.state is not SessionState.CLOSED:
            self.state.transition(SessionState.CLOSED, reason)
        logger.info("会话 %s 已关闭（存活 %.0fs）", self.session_id or "-", time.time() - self.created_at)

    @property
    def uptime_s(self) -> float:
        return time.time() - self.created_at


def default_reconnect_policy() -> ReconnectManager:
    return ReconnectManager(policy=BackoffPolicy(initial_s=0.5, max_s=15.0, multiplier=1.8))
