"""SDP test adapter and real aiortc desktop sessions with native media."""

from __future__ import annotations

import time
import threading
from dataclasses import dataclass, field

from backend.errors.network import NetworkError
from backend.logging.logger import get_logger
from backend.media.packet import Packet
from backend.signaling.sdp import SdpSession, build_answer, build_offer
from backend.transport.webrtc.data_channel import DataChannel, DataChannelConfig
from backend.transport.webrtc.ice import IceTransport
from backend.transport.webrtc.media_track import MediaTrack

logger = get_logger(__name__)
_capability_lock = threading.Lock()


class ConnectionState(str):
    NEW = "new"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    FAILED = "failed"
    CLOSED = "closed"


@dataclass
class PeerConnectionConfig:
    video_payload_type: int = 96
    audio_payload_type: int = 111
    ssrc: int = 0x12345678
    stun_servers: list[tuple[str, int]] = field(default_factory=list)
    data_channel: DataChannelConfig = field(default_factory=DataChannelConfig)


class PeerConnection:
    """一个对端连接。"""

    def __init__(
        self,
        config: PeerConnectionConfig | None = None,
        *,
        local_fingerprint: str = "",
        ice_ufrag: str = "",
        ice_pwd: str = "",
    ) -> None:
        self.config = config or PeerConnectionConfig()
        self.state = ConnectionState.NEW
        self.local_fingerprint = local_fingerprint
        self.ice_ufrag = ice_ufrag
        self.ice_pwd = ice_pwd

        self.ice = IceTransport()
        self.tracks: dict[str, MediaTrack] = {}
        self.data_channel: DataChannel | None = None
        self.remote_sdp: SdpSession | None = None
        self.local_sdp: SdpSession | None = None
        self._created_at = time.time()

    # ---- 协商 ----
    def create_offer(self) -> SdpSession:
        """生成 offer（只调用 ``sdp.py``，不碰字符串）。"""
        self.state = ConnectionState.CONNECTING
        self.ice.gather(self.config.stun_servers)

        from backend.security.dtls_srtp import DtlsSrtpParameters

        dtls = DtlsSrtpParameters(
            fingerprint=self.local_fingerprint or "sha-256 " + "0" * 95,
            setup="actpass",
            ice_ufrag=self.ice_ufrag,
            ice_pwd=self.ice_pwd,
        )
        session = build_offer(
            video_codecs={str(self.config.video_payload_type): "H264/90000"},
            audio_codecs={str(self.config.audio_payload_type): "opus/48000/2"},
            dtls=dtls,
            candidates=list(self.ice.agent.local),
        )
        self.local_sdp = session
        logger.info("已生成 offer: %s", session.describe())
        return session

    def set_remote_description(self, session: SdpSession) -> None:
        """记录对端 SDP 并启动连通性检查。"""
        self.remote_sdp = session
        pair = self.ice.start_checks(session)
        if pair is None:
            self.state = ConnectionState.FAILED
            raise NetworkError("ICE 未能建立连通", detail=self.ice.describe())
        self.state = ConnectionState.CONNECTED
        logger.info("PeerConnection 已连通: %s", self.ice.describe())

    def create_answer(self) -> SdpSession:
        if self.remote_sdp is None:
            raise NetworkError("收到 offer 之前不能生成 answer")
        from backend.security.dtls_srtp import DtlsSrtpParameters

        dtls = DtlsSrtpParameters(
            fingerprint=self.local_fingerprint or "sha-256 " + "0" * 95,
            setup="active",
            ice_ufrag=self.ice_ufrag,
            ice_pwd=self.ice_pwd,
        )
        session = build_answer(self.remote_sdp, dtls=dtls)
        self.ice.attach_local_candidates(session)
        self.local_sdp = session
        self.state = ConnectionState.CONNECTED
        return session

    # ---- 轨道 ----
    def add_track(self, mid: str = "0", codec=None) -> MediaTrack:
        from backend.media.stream import CodecId

        track = MediaTrack(
            payload_type=self.config.video_payload_type,
            ssrc=self.config.ssrc,
            codec=codec or CodecId.H264,
            mid=mid,
        )
        self.tracks[mid] = track
        logger.debug("新增轨道: %s", track.describe())
        return track

    def send_packet(self, packet: Packet, mid: str = "0") -> list[bytes]:
        track = self.tracks.get(mid)
        if track is None:
            raise NetworkError(f"轨道不存在: {mid}")
        return [rtp.pack() for rtp in track.send_packet(packet)]

    def receive_rtp(self, data: bytes, mid: str = "0") -> Packet | None:
        track = self.tracks.get(mid)
        if track is None:
            return None
        return track.receive_rtp(data)

    # ---- DataChannel ----
    def create_data_channel(self, config: DataChannelConfig | None = None) -> DataChannel:
        self.data_channel = DataChannel(config or self.config.data_channel)
        logger.debug("创建 DataChannel: %s", self.data_channel.describe())
        return self.data_channel

    # ---- 生命周期 ----
    def close(self) -> None:
        self.ice.stop()
        for track in self.tracks.values():
            track.close()
        self.tracks.clear()
        if self.data_channel is not None:
            self.data_channel.close()
            self.data_channel = None
        self.state = ConnectionState.CLOSED
        logger.info("PeerConnection 已关闭（存活 %.1fs）", time.time() - self._created_at)

    def describe(self) -> str:
        return (
            f"PeerConnection[{self.state}] tracks={len(self.tracks)} "
            f"ice={self.ice.state} uptime={time.time() - self._created_at:.1f}s"
        )


class RtcSession:
    """Actual aiortc session, signaled over the existing desktop connection."""

    def __init__(self, service, config, settings, renderer=None):
        import asyncio
        from aiortc import RTCConfiguration, RTCPeerConnection
        from backend.control_channel.datachannel import RtcControlChannel
        self.service, self.config, self.settings = service, config, settings
        self.renderer = renderer
        self.loop = asyncio.get_running_loop()
        self.pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.control = RtcControlChannel(self.loop, self.fail)
        self.tracks = []
        self.tasks = set()
        self.adapter = self.encoder = None
        self._failure = None
        self._loss = {}
        self._last_stats = time.monotonic()
        self._last_bytes = self._last_frames = 0
        self._connected = False
        self._closing = False
        self.pc.on("datachannel", self.control.attach)
        self.pc.on("track", self._track)
        self.pc.on("connectionstatechange", self._state)

    def fail(self, exc):
        if (self._closing or self.service._stop.is_set()
                or self.service._expected_stop.is_set()):
            return
        self._failure = exc
        self.service._stop.set()

    def _task(self, coroutine):
        import asyncio
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _state(self):
        if self.pc.connectionState == "connected":
            self._connected = True
            from backend.transport.webrtc.ice import describe_runtime_ice
            self.service.stats.extra["ice_routes"] = describe_runtime_ice(self.pc)
        elif self.pc.connectionState in {"failed", "closed"} and not self._closing:
            self.fail(NetworkError("WebRTC 连接已断开"))

    def _track(self, track):
        from backend.transport.webrtc.media_track import LatestQueue
        track._queue = LatestQueue(maxsize=1 if track.kind == "video" else 3)
        self._task(self._consume(track))

    async def _consume(self, track):
        import asyncio
        import av
        from aiortc.mediastreams import MediaStreamError
        from backend.media.frame import Frame
        try:
            if track.kind == "audio":
                from backend.media.audio.playback.windows.wasapi_playback import WasapiPlayback
                self.service._audio_output = WasapiPlayback()
                try:
                    self.service._audio_output.open()
                except Exception as exc:
                    self.service.log.emit(f"系统声音播放不可用：{exc}", "warn")
                    return
                resampler = av.AudioResampler(format="fltp", layout="stereo", rate=48000)
            while not self.service._stop.is_set():
                image = await track.recv()
                if track.kind == "video":
                    self.renderer.render(Frame.from_av(image, native=True))
                else:
                    output = self.service._audio_output
                    output.muted = self.settings.muted
                    for audio in resampler.resample(image):
                        output.write_samples(audio.to_ndarray().T.copy())
                        stats = self.service.stats.extra
                        stats["audio_frames"] = stats.get("audio_frames", 0) + 1
        except (MediaStreamError, asyncio.CancelledError):
            if not self._closing and not self.service._stop.is_set():
                self.fail(NetworkError("WebRTC 媒体轨道已结束"))
        except Exception as exc:
            self.fail(exc)

    async def prepare(self):
        from aiortc import RTCRtpSender
        from aiortc.codecs import CODECS
        import copy
        from types import SimpleNamespace
        from backend.bootstrap import build_capture, prepare_environment
        from backend.pipeline.video_pipeline import VideoPipelineStats
        from backend.pipeline.receive_pipeline import ReceivePipelineStats
        from backend.media.stream import StreamInfo
        from backend.media.video.codec.inprocess import InprocessEncoder
        from backend.transport.webrtc.media_track import DesktopTrack, LoopbackTrack, SelectedEncoder, tune_receiver
        from backend.qos.pacing import install_rtp_pacer
        # Keep the negotiated H.264 level consistent with 1080p120/4K60.
        with _capability_lock:
            if not any(c.mimeType.lower() == "video/h264"
                       and c.parameters.get("profile-level-id") == "640034" for c in CODECS["video"]):
                template = copy.deepcopy(next(c for c in CODECS["video"] if c.mimeType.lower() == "video/h264"))
                template.payloadType = max(c.payloadType for c in CODECS["video"]) + 1
                template.parameters["profile-level-id"] = "640034"
                CODECS["video"].append(template)
        _, registry = prepare_environment(self.config)
        stream = StreamInfo(width=self.config.encode.width, height=self.config.encode.height,
                            framerate=self.config.capture.fps)
        host = self.service.mode == "host"
        stats = VideoPipelineStats() if host else ReceivePipelineStats()
        self.service._pipeline = SimpleNamespace(stats=stats, stream=stream, capture=None)
        self.service._control = self.control
        if host:
            from backend.input_injection.session import InputSession
            self.service._input_session = InputSession(self.service._inject_remote_input)
            self.control.on_message(lambda event: self.service._on_remote_input(event, self.settings))
            capture = build_capture(self.config)
            self.service._pipeline.capture = capture
            video = DesktopTrack(capture, self.fail)
            self.tracks.append(video)
            self.encoder = registry.create_encoder("h264", self.settings.encoder, stream, self.config.encode)
            if not isinstance(self.encoder, InprocessEncoder):
                raise NetworkError("WebRTC 编码器需要可用的进程内实现")
            sender = self.pc.addTrack(video)
            sender._RTCRtpSender__encoder = SelectedEncoder(self.encoder, stats, self.fail)
            self.tracks.append(LoopbackTrack(
                lambda exc: self.service.log.emit(f"系统声音采集不可用：{exc}", "warn")))
            self.pc.addTrack(self.tracks[-1])
            self.control.attach(self.pc.createDataChannel("control", ordered=True))
            self.control.attach(self.pc.createDataChannel("motion", ordered=False, maxRetransmits=0))
        else:
            self.control.on_message(lambda event: self.service._on_remote_event(event, self.settings))
            transceiver = self.pc.addTransceiver("video", direction="recvonly")
            self.pc.addTransceiver("audio", direction="recvonly")
            self.adapter = registry.create_decoder("h264", self.settings.decoder, stream, self.config.encode)
            self.adapter.output_callback = None
            self._receiver_cleanup = tune_receiver(transceiver.receiver, self.adapter, self.fail, stats)
            self.renderer.open()
            if hasattr(self.renderer, "prepare_ms"):
                stats.extra["display_prepare_ms"] = self.renderer.prepare_ms
        for transceiver in self.pc.getTransceivers():
            if transceiver.kind == "video":
                preferences = [
                    c for c in RTCRtpSender.getCapabilities("video").codecs
                    if c.mimeType.lower() == "video/h264"]
                preferences.sort(key=lambda c: c.parameters.get("profile-level-id") != "640034")
                transceiver.setCodecPreferences(preferences)
        stats.extra["encoder" if host else "decoder"] = (
            self.settings.encoder if host else self.settings.decoder)
        if host:
            stats.extra["capture_ms"] = video.capture_ms
        install_rtp_pacer(self.pc, self.config.encode.bitrate_kbps * 1000, self.config.capture.fps)
        for transport in {t.sender.transport for t in self.pc.getTransceivers()}:
            original = transport._send_rtp

            async def send(data, original=original):
                try:
                    await original(data)
                except Exception as exc:
                    self.fail(exc)
                    raise

            transport._send_rtp = send
        if host:
            self.service.host_prepared.emit()
            await self.pc.setLocalDescription(await self.pc.createOffer())
            self.service.rtc_signal.emit({"type": "offer", "sdp": self.pc.localDescription.sdp})

    async def signal(self, value):
        from aiortc import RTCSessionDescription
        if (not isinstance(value, dict) or value.get("type") not in {"offer", "answer"}
                or not isinstance(value.get("sdp"), str) or len(value["sdp"]) > 100000):
            raise NetworkError("WebRTC 会话描述无效")
        expected = "answer" if self.service.mode == "host" else "offer"
        if value["type"] != expected:
            raise NetworkError("WebRTC 会话描述角色不匹配")
        await self.pc.setRemoteDescription(RTCSessionDescription(**value))
        if value["type"] == "offer":
            await self.pc.setLocalDescription(await self.pc.createAnswer())
            self.service.rtc_signal.emit({"type": "answer", "sdp": self.pc.localDescription.sdp})

    async def run(self):
        import asyncio
        from backend.control_channel.base import ChannelState
        from backend.control_channel.protocol import ControlCommand
        await self.prepare()
        self.service._rtc_ready.set()
        available = False
        try:
            while not self.service._stop.is_set():
                while not self.service._rtc_inbox.empty():
                    await self.signal(self.service._rtc_inbox.get_nowait())
                if self.control.state == ChannelState.OPEN:
                    if not available:
                        available = True
                        self.service.input_available.emit(True)
                        self.service.control_changed.emit("WebRTC 控制通道已连接")
                    if self.service.mode == "host":
                        capture = self.service._pipeline.capture
                        self.control.send_command(ControlCommand("cursor", getattr(capture, "cursor_state", {})))
                        if self.service._input_session:
                            self.service._input_session.watchdog()
                    elif self.service._input_captured:
                        self.control.send_command(ControlCommand("input_focus", {
                            "active": True, "epoch": self.service._input_epoch}))
                elif available:
                    raise NetworkError("WebRTC 控制通道已断开")
                if time.monotonic() - self._last_stats >= 1:
                    await self.sample_stats()
                await asyncio.sleep(.05)
            if self._failure:
                raise self._failure
        finally:
            await self.close()

    async def sample_stats(self):
        from backend.qos.congestion_control import CongestionController
        from backend.qos.stats import QualitySample
        from backend.control_channel.protocol import ControlCommand
        stats = self.service.stats
        extra = stats.extra
        extra.pop("packet_loss_percent", None)
        report = await self.pc.getStats()
        network_bytes = 0
        for item in report.values():
            if item.type == "transport":
                network_bytes += item.bytesSent if self.service.mode == "host" else item.bytesReceived
            if item.type in {"inbound-rtp", "remote-inbound-rtp"} and item.kind == "video":
                previous = self._loss.get(item.id)
                total = item.packetsReceived + item.packetsLost
                if previous and total > previous[0]:
                    loss = max(0, item.packetsLost - previous[1]) / (total - previous[0])
                    extra["packet_loss_percent"] = min(100, loss * 100)
                self._loss[item.id] = (total, item.packetsLost)
                if item.type == "remote-inbound-rtp":
                    if item.roundTripTime is not None:
                        extra.update(rtt_ms=item.roundTripTime * 1000, rtt_sampled_at=time.monotonic())
                    if not hasattr(self, "_congestion"):
                        limit = self.config.encode.bitrate_kbps
                        self._congestion = CongestionController(initial_kbps=limit, max_kbps=limit)
                    self._congestion.on_sample(QualitySample(
                        loss_rate=item.fractionLost / 256, rtt_ms=(item.roundTripTime or 0) * 1000,
                        jitter_ms=item.jitter / 90))
                    rate = round(self._congestion.state.target_kbps * 1000)
                    if self.encoder:
                        self.encoder.target_bitrate = rate
                    for transceiver in self.pc.getTransceivers():
                        budget = getattr(transceiver.sender.transport, "_elink_budget", None)
                        if budget:
                            budget["rate"] = rate * 1.08
        now = time.monotonic()
        interval = now - self._last_stats
        pipeline_stats = self.service._pipeline.stats
        host = self.service.mode == "host"
        frames = pipeline_stats.frames_encoded if host else self.service._presented
        stats.fps = (frames - self._last_frames) / interval
        stats.bitrate_kbps = max(0, network_bytes - self._last_bytes) * 8 / interval / 1000
        stats.bytes_out = network_bytes
        stats.frames_out = frames
        stats.elapsed_s = now - self.service._started_at
        self._last_frames, self._last_bytes, self._last_stats = frames, network_bytes, now
        extra.update(sampled_at=now, codec="h264", **pipeline_stats.extra)
        if self.adapter and hasattr(self.adapter, "decode_ms") and self.adapter.decode_ms:
            extra["decode_ms"] = sum(self.adapter.decode_ms) / len(self.adapter.decode_ms)
            extra["decode_label"] = "解码"
            pipeline_stats.extra["decode_call_ms"] = self.adapter.decode_ms
            pipeline_stats.extra["pixel_convert_ms"] = self.adapter.convert_ms
        if host and self.control.state == "open":
            self.control.send_command(ControlCommand("host_stats", {
                "encoder": self.settings.encoder,
                "rtt_ms": extra.get("rtt_ms"),
            }))
        self.service.stats_changed.emit(stats)

    async def close(self):
        import asyncio
        self._closing = True
        if hasattr(self, "_receiver_cleanup"):
            self._receiver_cleanup()
        self.service.set_input_focus(False)
        for track in self.tracks:
            track.stop()
        for task in tuple(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
        try:
            await asyncio.wait_for(self.pc.close(), timeout=5)
        finally:
            for track in self.tracks:
                await track.close()
            if self.adapter:
                self.adapter.close()
            if self.encoder:
                self.encoder.close()
            if self.renderer:
                self.renderer.close()
            if self.service._audio_output:
                self.service._audio_output.close()
                self.service._audio_output = None
