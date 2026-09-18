from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import socket
import ssl
import time
from pathlib import Path

import aiohttp
from aiohttp import web
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription, RTCRtpSender

from ..models import Endpoint, ValidationError
from . import codecs
from .input import InputSession, WindowsInput, PAD_BACKENDS
from .media import AudioOutput, DesktopTrack, FrameMailbox, LoopbackTrack
from .security import Identity, PairingAuthority, TrustStore, pairing_proof
from .buffers import tune_receiver, tune_track


def video_preferences(pc):
    # Both Elink peers advertise level 5.2, sufficient for the 1080p120 ceiling.
    import copy
    preferences = copy.deepcopy([c for c in RTCRtpSender.getCapabilities("video").codecs if c.mimeType.lower() == "video/h264"])
    for codec in preferences:
        codec.parameters["profile-level-id"] = "42e034"
    for transceiver in pc.getTransceivers():
        if transceiver.kind == "video":
            transceiver.setCodecPreferences(preferences)


def install():
    codecs.install_factories()
    from aioice.ice import StunProtocol
    async def close_datagram(protocol):
        # Python's Windows proactor can wait indefinitely flushing queued UDP sends.
        # Media is already stopped; discard remaining datagrams when closing ICE.
        protocol.transport.abort()
        await protocol._StunProtocol__closed
    StunProtocol.close = close_datagram
    # aiortc validates preferences against its supported codec registry.
    from aiortc.codecs import CODECS
    for codec in CODECS["video"]:
        if codec.mimeType.lower() == "video/h264":
            codec.parameters["profile-level-id"] = "42e034"


def settings(value):
    if not isinstance(value, dict):
        raise ValidationError("串流设置无效。")
    result = {"width": 1920, "height": 1080, "fps": 60, "bitrate": 20, "audio": True}
    result.update({k: v for k, v in value.items() if k in result})
    for key, low, high in [("width", 640, 1920), ("height", 360, 1080), ("fps", 10, 120), ("bitrate", 1, 80)]:
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise ValidationError(f"{key} 超出当前版本范围（{low}–{high}）。")
    if result["width"] % 2 or result["height"] % 2 or type(result["audio"]) is not bool:
        raise ValidationError("分辨率必须为偶数，音频开关必须为布尔值。")
    return result


@web.middleware
async def errors(request, handler):
    try:
        return await handler(request)
    except (ValidationError, ValueError, TypeError, KeyError) as exc:
        return web.json_response({"error": str(exc)[:300]}, status=400)


class HostServer:
    def __init__(self, root: Path, *, synthetic=False, backend_factory=None, notify=lambda message: None, pad_backend="vigem"):
        if pad_backend not in PAD_BACKENDS:
            raise ValueError("未知手柄后端。")
        self.pad_backend = pad_backend
        install()
        self.identity = Identity(root)
        self.authority = PairingAuthority(root, self.identity.fingerprint)
        self.synthetic, self.backend_factory, self.notify = synthetic, backend_factory, notify
        self.runner = None
        self.pc = None
        self.input = None
        self.tracks = []
        self.session_id = self.device_id = ""
        self.lock = asyncio.Lock()
        self.watch_task = None
        self.closing = False
        self.ending = False
        self.end_task = None
        self.created = 0.0
        self.input_errors = set()
        self.control_channel = None

    async def start(self, bind=None, port=49200):
        if self.runner:
            raise ValidationError("本机被控已启动。")
        app = web.Application(middlewares=[errors], client_max_size=131072)
        app.add_routes([web.get("/v1/info", self.info), web.post("/v1/pair/start", self.pair_start),
                        web.post("/v1/pair/poll", self.pair_poll), web.get("/v1/apps", self.apps),
                        web.post("/v1/session", self.offer), web.delete("/v1/session/{id}", self.delete)])
        self.closing = False
        self.runner = web.AppRunner(app, access_log=None, shutdown_timeout=3)
        try:
            await self.runner.setup()
            site = web.TCPSite(self.runner, bind, port, ssl_context=self.identity.context)
            await site.start()
            self.port = site._server.sockets[0].getsockname()[1]
            self.watch_task = asyncio.create_task(self.watchdog())
        except BaseException:
            await self.stop()
            raise
        return self.port

    async def info(self, request):
        return web.json_response({"protocol": "elink", "version": 1, "name": socket.gethostname(), "codec": "H264"})

    async def body(self, request):
        data = await request.json()
        if not isinstance(data, dict):
            raise ValidationError("请求格式无效。")
        return data

    async def pair_start(self, request):
        data = await self.body(request)
        for key in ("nonce", "proof", "name"):
            if not isinstance(data.get(key), str) or not data[key].isascii() and key != "name":
                raise ValidationError("配对字段无效。")
        identifier = self.authority.begin(data["nonce"], data["proof"], data["name"], request.remote or "unknown")
        return web.json_response({"id": identifier})

    async def pair_poll(self, request):
        data = await self.body(request)
        if not all(isinstance(data.get(k), str) and data[k].isascii() for k in ("id", "proof")):
            raise ValidationError("配对字段无效。")
        return web.json_response(self.authority.poll(data["id"], data["proof"]))

    def authenticate(self, request):
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            raise web.HTTPUnauthorized()
        try:
            return self.authority.authenticate(header[7:])
        except ValidationError:
            raise web.HTTPUnauthorized() from None

    async def apps(self, request):
        self.authenticate(request)
        return web.json_response({"apps": [{"id": "desktop", "name": "当前桌面"}]})

    async def offer(self, request):
        device = self.authenticate(request)
        data = await self.body(request)
        config = settings(data.get("settings", {}))
        if data.get("type") != "offer" or not isinstance(data.get("sdp"), str) or len(data["sdp"]) > 100000:
            raise ValidationError("会话描述无效。")
        from aiortc.sdp import SessionDescription
        description = SessionDescription.parse(data["sdp"])
        kinds = [media.kind for media in description.media]
        if sorted(kinds) != sorted(["video", "application"] + (["audio"] if config["audio"] else [])):
            raise ValidationError("会话只能包含一个视频、可选音频与输入通道。")
        if any(media.direction != "recvonly" for media in description.media if media.kind in ("video", "audio")):
            raise ValidationError("控制端只能接收主机媒体。")
        async with self.lock:
            if self.pc or self.closing or self.ending:
                raise web.HTTPConflict(reason="主机已有会话或正在停止")
            # Recheck after acquiring the lock, in case approval was revoked meanwhile.
            self.authenticate(request)
            pc = self.pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
            self.device_id, self.session_id = device, secrets.token_hex(16)
            self.created = time.monotonic()
            self.input_errors.clear()
            codecs.encode_policy = codecs.CodecPolicy(config["fps"], config["bitrate"] * 1_000_000)
            loop = asyncio.get_running_loop()
            reliable_channel = None

            def feedback(event):
                def send():
                    if reliable_channel and reliable_channel.readyState == "open" and reliable_channel.bufferedAmount < 16384:
                        reliable_channel.send(json.dumps(event))
                loop.call_soon_threadsafe(send)

            self.input = InputSession(self.backend_factory() if self.backend_factory else WindowsInput(feedback, self.pad_backend, self.notify))
            input_session = self.input

            @pc.on("datachannel")
            def on_channel(channel):
                nonlocal reliable_channel
                reliable = channel.label == "control"
                if channel.label not in ("control", "motion") or reliable and reliable_channel is not None:
                    channel.close()
                    return
                if reliable:
                    reliable_channel = channel
                    self.control_channel = channel
                count, period = 0, time.monotonic()

                @channel.on("message")
                def on_message(raw):
                    nonlocal count, period
                    if self.pc is not pc or self.device_id not in self.authority.devices:
                        return
                    now = time.monotonic()
                    if now - period > 1:
                        count, period = 0, now
                    count += 1
                    if count > 2000 or not isinstance(raw, str) or len(raw) > 2048:
                        return
                    try:
                        event = json.loads(raw)
                        if reliable and isinstance(event, dict) and event.get("type") == "ping":
                            stamp = event.get("time")
                            if type(stamp) in (int, float):
                                feedback({"type": "pong", "time": stamp})
                            return
                        input_session.receive(raw, reliable)
                    except Exception as exc:
                        message = str(exc)[:200]
                        if message not in self.input_errors:
                            self.input_errors.add(message)
                            feedback({"type": "warning", "message": message})
                            self.notify(message)

            @pc.on("connectionstatechange")
            async def state():
                self.notify(f"被控会话：{pc.connectionState}")
                if pc.connectionState in ("failed", "closed") and self.pc is pc:
                    await self.end_session()

            try:
                await pc.setRemoteDescription(RTCSessionDescription(data["sdp"], "offer"))
                video = DesktopTrack(config["width"], config["height"], config["fps"], self.synthetic)
                self.tracks.append(video)
                pc.addTrack(video)
                if config["audio"]:
                    audio = LoopbackTrack(self.synthetic)
                    self.tracks.append(audio)
                    pc.addTrack(audio)
                video_preferences(pc)
                await pc.setLocalDescription(await pc.createAnswer())
                self.authenticate(request)
                if self.pc is not pc:
                    raise ValidationError("会话已取消。")
                return web.json_response({"id": self.session_id, "type": pc.localDescription.type, "sdp": pc.localDescription.sdp})
            except BaseException:
                await self.end_session()
                raise

    async def delete(self, request):
        device = self.authenticate(request)
        if device == self.device_id and request.match_info["id"] == self.session_id:
            await self.end_session()
        return web.json_response({"ok": True})

    async def revoke(self, device_id):
        self.authority.revoke(device_id)
        if self.device_id == device_id:
            await self.end_session()

    async def watchdog(self):
        while True:
            await asyncio.sleep(0.25)
            if self.input:
                self.input.watchdog()
            if self.pc:
                expired = time.monotonic() - self.created > 20 and (self.pc.connectionState != "connected" or time.monotonic() - self.input.heartbeat > 15)
                if expired:
                    self.notify("会话超时，已释放输入。")
                    await self.end_session()

    async def end_session(self):
        if self.pc:
            pc, self.pc = self.pc, None
            self.ending = True
            input_session, self.input = self.input, None
            tracks, self.tracks = self.tracks, []
            self.session_id = self.device_id = ""
            self.end_task = asyncio.create_task(self.finish_session(pc, input_session, tracks))
        if self.end_task:
            # Cancelling a watchdog/request must not interrupt native transport teardown.
            await asyncio.shield(self.end_task)

    async def finish_session(self, pc, input_session, tracks):
        try:
            if input_session:
                input_session.close()
            for track in tracks:
                track.stop()
            channel, self.control_channel = self.control_channel, None
            if channel and channel.readyState == "open":
                channel.send(json.dumps({"type": "ended"}))
                await asyncio.sleep(0.05)
            await pc.close()
        finally:
            for track in tracks:
                if isinstance(track, DesktopTrack) and getattr(track, "release_future", None):
                    await asyncio.wrap_future(track.release_future)
                if isinstance(track, LoopbackTrack):
                    await asyncio.to_thread(track.thread.join, 1)
            self.ending = False

    async def stop(self):
        self.closing = True
        self.authority.code = ""
        self.authority.expires = 0
        self.authority.pending.clear()
        if self.watch_task:
            self.watch_task.cancel()
            await asyncio.gather(self.watch_task, return_exceptions=True)
            self.watch_task = None
        # Wait for any offer handler to leave its negotiation section.
        async with self.lock:
            await self.end_session()
        if self.runner:
            runner, self.runner = self.runner, None
            await runner.cleanup()


async def fingerprint(endpoint):
    # Credential-free bootstrap only. Invitation proof is bound to this fingerprint.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    _, writer = await asyncio.wait_for(asyncio.open_connection(endpoint.hostname, endpoint.port, ssl=context), 6)
    try:
        certificate = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
        return hashlib.sha256(certificate).hexdigest()
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


async def request(endpoint, fingerprint_value, method, path, *, token="", payload=None):
    pin = aiohttp.Fingerprint(bytes.fromhex(fingerprint_value))
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20), trust_env=False) as session:
        async with session.request(method, endpoint.url(endpoint.port, path, "https"), ssl=pin,
                                   headers=headers, json=payload, allow_redirects=False) as response:
            content = bytearray()
            async for chunk in response.content.iter_chunked(16384):
                content.extend(chunk)
                if len(content) > 131072:
                    raise ValidationError("主机响应过大。")
            if response.status != 200:
                if response.status == 401:
                    raise ValidationError("配对授权无效，请重新配对。")
                if response.status == 409:
                    raise ValidationError("主机已有串流会话。")
                raise ValidationError(f"主机拒绝请求（{response.status}）：{content.decode('utf-8', errors='replace')[:240]}")
            result = json.loads(content)
            if not isinstance(result, dict):
                raise ValidationError("主机响应格式无效。")
            return result


class Client:
    def __init__(self, root, notify=lambda message: None, feedback=lambda event: None, *, play_audio=True):
        install()
        self.trust = TrustStore(root)
        self.notify, self.feedback = notify, feedback
        self.play_audio = play_audio
        self.pc = None
        self.session_id = ""
        self.endpoint = None
        self.mailbox = FrameMailbox()
        self.tasks = set()
        self.control = self.motion = None
        self.output = None
        self.audio_frames = 0
        self.rtt_ms = 0.0
        self.last_pong = 0.0
        self.active = False
        self.epoch = 0
        self.sequence = 0
        self.connected_at = 0.0
        self.lock = asyncio.Lock()
        self.close_task = None

    def schedule_disconnect(self):
        if self.close_task is None or self.close_task.done():
            self.close_task = asyncio.create_task(self.disconnect())

    async def pair(self, address, invitation, name=None):
        endpoint = Endpoint.parse(address)
        code = invitation.strip().upper().replace(" ", "")
        if not re.fullmatch(r"[A-Z2-7]{26}", code):
            raise ValidationError("请粘贴主机显示的完整 26 位临时邀请。")
        fp = await fingerprint(endpoint)
        nonce, name = secrets.token_hex(32), (name or socket.gethostname())[:80]
        result = await request(endpoint, fp, "POST", "/v1/pair/start", payload={"nonce": nonce, "name": name, "proof": pairing_proof(code, fp, nonce, name)})
        self.notify("配对请求已发送，请在主机 Elink 中批准。")
        for _ in range(175):
            response = await request(endpoint, fp, "POST", "/v1/pair/poll", payload={"id": result["id"], "proof": pairing_proof(code, fp, nonce, name, "poll")})
            if response.get("state") == "approved":
                self.trust.save(endpoint.authority, fp, response["token"], response["device_id"])
                return "配对成功，主机身份与设备凭据已保存。"
            await asyncio.sleep(1)
        raise ValidationError("等待配对批准超时。")

    async def connect(self, address, config, decoder="auto"):
        async with self.lock:
            if self.pc:
                raise ValidationError("请先断开当前串流。")
            config = settings(config)
            self.endpoint = Endpoint.parse(address)
            self.fp, self.token = self.trust.get(self.endpoint.authority)
            codecs.decode_policy = codecs.CodecPolicy(decoder=decoder)
            self.pc = pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
            self.mailbox = FrameMailbox()
            self.audio_frames = 0
            self.connected_at = time.monotonic()
            self.last_pong = self.connected_at
            self.control = pc.createDataChannel("control", ordered=True)
            self.motion = pc.createDataChannel("motion", ordered=False, maxRetransmits=0)

            @self.control.on("close")
            def control_closed():
                if self.pc is pc:
                    self.schedule_disconnect()

            @self.control.on("message")
            def message(raw):
                try:
                    if not isinstance(raw, str) or len(raw) > 2048:
                        return
                    event = json.loads(raw)
                    if not isinstance(event, dict):
                        return
                    if event.get("type") == "pong" and type(event.get("time")) in (int, float):
                        self.rtt_ms = max(0, (time.monotonic() - event["time"]) * 1000)
                        self.last_pong = time.monotonic()
                    elif event.get("type") == "ended":
                        self.schedule_disconnect()
                    elif event.get("type") == "warning":
                        self.notify(str(event.get("message", ""))[:200])
                    elif event.get("type") == "rumble":
                        from .input import integer
                        if integer(event.get("index"), 0, 3) and all(integer(event.get(k), 0, 65535) for k in ("left", "right")):
                            self.feedback(event)
                except (ValueError, TypeError):
                    pass

            @pc.on("track")
            def track_received(track):
                tune_track(track)
                self.task(self.consume(track))

            @pc.on("connectionstatechange")
            async def state():
                self.notify(f"控制端会话：{pc.connectionState}")
                if pc.connectionState in ("failed", "closed") and self.pc is pc:
                    self.schedule_disconnect()

            pc.addTransceiver("video", direction="recvonly")
            if config["audio"]:
                pc.addTransceiver("audio", direction="recvonly")
            for transceiver in pc.getTransceivers():
                tune_receiver(transceiver.receiver)
            video_preferences(pc)
            try:
                await pc.setLocalDescription(await pc.createOffer())
                response = await request(self.endpoint, self.fp, "POST", "/v1/session", token=self.token,
                                         payload={"type": pc.localDescription.type, "sdp": pc.localDescription.sdp, "settings": config})
                self.session_id = response["id"]
                await pc.setRemoteDescription(RTCSessionDescription(response["sdp"], response["type"]))
                self.task(self.heartbeat())
            except BaseException:
                await self._disconnect()
                raise

    def task(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def consume(self, track):
        try:
            if track.kind == "audio" and self.play_audio:
                try:
                    self.output = AudioOutput()
                except Exception as exc:
                    self.notify(f"本机音频输出不可用：{exc}")
            while True:
                frame = await track.recv()
                if track.kind == "video":
                    pixels = await asyncio.to_thread(frame.to_ndarray, format="rgb24")
                    self.mailbox.put(pixels)
                else:
                    self.audio_frames += 1
                    if self.output:
                        self.output.feed(frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self.pc and self.pc.connectionState != "closed":
                self.notify(f"{track.kind} 轨道结束：{exc}")
                if track.kind == "video":
                    self.schedule_disconnect()

    def send(self, event):
        if not self.pc:
            return
        channel = self.motion if event.get("type") in ("move", "pad") else self.control
        if not channel or channel.readyState != "open":
            return
        if channel.bufferedAmount > 65536:
            # Stop the session rather than dropping a reliable key-up indefinitely.
            if channel is self.control:
                self.schedule_disconnect()
            return
        self.sequence += 1
        channel.send(json.dumps(dict(event, epoch=self.epoch, seq=self.sequence)))

    def focus(self, active):
        self.active = active
        self.epoch += 1
        self.send({"type": "heartbeat", "active": active})
        if not active:
            self.send({"type": "release"})

    async def heartbeat(self):
        while self.pc:
            self.send({"type": "heartbeat", "active": self.active})
            self.send({"type": "ping", "time": time.monotonic()})
            now = time.monotonic()
            if self.control and self.control.readyState == "open" and now - self.last_pong > 5:
                self.notify("主机心跳响应超时，已断开会话。")
                self.schedule_disconnect()
                return
            if now - (self.mailbox.updated or self.connected_at) > 15:
                self.notify("15 秒未收到画面，已断开会话。")
                self.schedule_disconnect()
                return
            await asyncio.sleep(0.5)

    async def disconnect(self):
        async with self.lock:
            await self._disconnect()

    async def _disconnect(self):
        self.focus(False)
        pc, self.pc = self.pc, None
        current = asyncio.current_task()
        tasks = [t for t in self.tasks if t is not current]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.output:
            self.output.close()
            self.output = None
        if pc:
            await pc.close()
        if self.session_id:
            identifier, self.session_id = self.session_id, ""
            try:
                await asyncio.wait_for(request(self.endpoint, self.fp, "DELETE", f"/v1/session/{identifier}", token=self.token), 4)
            except Exception:
                pass
        self.control = self.motion = None
