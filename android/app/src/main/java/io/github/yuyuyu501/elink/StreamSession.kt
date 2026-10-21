package io.github.yuyuyu501.elink

import android.content.Context
import android.content.SharedPreferences
import kotlinx.coroutines.*
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import org.json.JSONObject
import org.webrtc.*
import org.webrtc.audio.JavaAudioDeviceModule
import java.nio.ByteBuffer
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

class StreamSession(
    context: Context, private val preferences: SharedPreferences,
    private val renderer: SurfaceViewRenderer,
    private val changed: (String) -> Unit, private val failed: (String) -> Unit,
    private val ended: () -> Unit
) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val mutex = Mutex()
    private val egl = EglBase.create()
    private lateinit var audio: JavaAudioDeviceModule
    private lateinit var factory: PeerConnectionFactory
    private var peer: PeerConnection? = null
    private var control: DataChannel? = null
    private var motion: DataChannel? = null
    private var signal: Signaling? = null
    private var sessionId: String? = null
    private var timer: Job? = null
    private var epoch = 0L
    private var sequence = 0L
    private var active = false
    private var closing = false
    private val disposed = AtomicBoolean(false)
    private var lastPong = 0L
    private var rtt = 0.0
    private var encoder = "--"
    private val frameCount = AtomicInteger()
    @Volatile var videoWidth = 1920
    @Volatile var videoHeight = 1080
    var connected = false
        private set

    init {
        try {
        PeerConnectionFactory.initialize(PeerConnectionFactory.InitializationOptions.builder(context)
            .setEnableInternalTracer(false).createInitializationOptions())
        audio = JavaAudioDeviceModule.builder(context).createAudioDeviceModule()
        factory = PeerConnectionFactory.builder().setAudioDeviceModule(audio)
            .setVideoDecoderFactory(DefaultVideoDecoderFactory(egl.eglBaseContext))
            .setVideoEncoderFactory(DefaultVideoEncoderFactory(egl.eglBaseContext, true, true))
            .createPeerConnectionFactory()
        renderer.init(egl.eglBaseContext, null)
        renderer.setEnableHardwareScaler(true)
        renderer.setScalingType(RendererCommon.ScalingType.SCALE_ASPECT_FIT)
        } catch (failure: Exception) {
            runCatching { renderer.release() }
            if (::factory.isInitialized) factory.dispose()
            if (::audio.isInitialized) audio.release()
            egl.release()
            scope.cancel()
            throw failure
        }
    }

    private val sink = VideoSink { frame ->
        videoWidth = frame.rotatedWidth
        videoHeight = frame.rotatedHeight
        frameCount.incrementAndGet()
        renderer.onFrame(frame)
    }

    private val observer = object : PeerConnection.Observer {
        override fun onSignalingChange(state: PeerConnection.SignalingState) {}
        override fun onIceConnectionChange(state: PeerConnection.IceConnectionState) {
            if (state == PeerConnection.IceConnectionState.FAILED) scope.launch { abort("网络连接失败") }
        }
        override fun onConnectionChange(state: PeerConnection.PeerConnectionState) {
            if (state == PeerConnection.PeerConnectionState.FAILED) scope.launch { abort("媒体连接失败") }
        }
        override fun onIceConnectionReceivingChange(receiving: Boolean) {}
        override fun onIceGatheringChange(state: PeerConnection.IceGatheringState) {}
        override fun onIceCandidate(candidate: IceCandidate) {}
        override fun onIceCandidatesRemoved(candidates: Array<out IceCandidate>) {}
        override fun onAddStream(stream: MediaStream) {}
        override fun onRemoveStream(stream: MediaStream) {}
        override fun onDataChannel(channel: DataChannel) { channel.close() }
        override fun onRenegotiationNeeded() {}
        override fun onAddTrack(receiver: RtpReceiver, streams: Array<out MediaStream>) {
            (receiver.track() as? VideoTrack)?.addSink(sink)
        }
    }

    suspend fun connect(address: HostAddress, config: StreamConfig) = mutex.withLock {
        VideoSupport.validate(config)
        check(peer == null) { "已有连接" }
        check(!disposed.get()) { "会话已关闭" }
        closing = false
        val signaling = Signaling(address, preferences)
        try {
            val info = withContext(Dispatchers.IO) { signaling.info() }
            currentCoroutineContext().ensureActive()
            check(!info.optBoolean("busy")) { "这台设备正在被控制" }
            signal = signaling
            val rtc = PeerConnection.RTCConfiguration(emptyList()).apply {
                sdpSemantics = PeerConnection.SdpSemantics.UNIFIED_PLAN
                bundlePolicy = PeerConnection.BundlePolicy.MAXBUNDLE
                continualGatheringPolicy = PeerConnection.ContinualGatheringPolicy.GATHER_ONCE
            }
            val pc = factory.createPeerConnection(rtc, observer) ?: error("无法创建 WebRTC 连接")
            peer = pc
            control = pc.createDataChannel("control", DataChannel.Init()).also { channel ->
                channel.registerObserver(object : DataChannel.Observer {
                    override fun onBufferedAmountChange(previous: Long) {}
                    override fun onStateChange() {
                        scope.launch {
                            if (closing || peer !== pc) return@launch
                            if (channel.state() == DataChannel.State.OPEN) {
                                connected = true
                                lastPong = android.os.SystemClock.elapsedRealtime()
                                focus(true)
                            } else if (channel.state() == DataChannel.State.CLOSED) abort("远程会话已结束")
                        }
                    }
                    override fun onMessage(buffer: DataChannel.Buffer) {
                        if (buffer.binary || buffer.data.remaining() > 262144) return
                        val bytes = ByteArray(buffer.data.remaining())
                        buffer.data.get(bytes)
                        scope.launch { receive(String(bytes, Charsets.UTF_8)) }
                    }
                })
            }
            motion = pc.createDataChannel("motion", DataChannel.Init().apply { ordered = false; maxRetransmits = 0 })
            val video = pc.addTransceiver(MediaStreamTrack.MediaType.MEDIA_TYPE_VIDEO,
                RtpTransceiver.RtpTransceiverInit(RtpTransceiver.RtpTransceiverDirection.RECV_ONLY))
            val h264 = factory.getRtpReceiverCapabilities(MediaStreamTrack.MediaType.MEDIA_TYPE_VIDEO).codecs
                .filter { it.name.equals("H264", ignoreCase = true) }
            check(h264.isNotEmpty()) { "设备没有可用的 H.264 解码器" }
            video.setCodecPreferences(h264)
            pc.addTransceiver(MediaStreamTrack.MediaType.MEDIA_TYPE_AUDIO,
                RtpTransceiver.RtpTransceiverInit(RtpTransceiver.RtpTransceiverDirection.RECV_ONLY))
            val offer = createOffer(pc)
            setDescription(pc, offer, true)
            withTimeout(15000) {
                while (pc.iceGatheringState() != PeerConnection.IceGatheringState.COMPLETE) delay(30)
            }
            val answer = withContext(Dispatchers.IO) {
                val response = signaling.request("POST", "/v1/session", JSONObject().put("type", "offer")
                    .put("sdp", pc.localDescription.description).put("settings", settings(config)))
                sessionId = response.getString("id")
                response
            }
            currentCoroutineContext().ensureActive()
            sessionId = answer.getString("id")
            setDescription(pc, SessionDescription(SessionDescription.Type.ANSWER, answer.getString("sdp")), false)
            changed("正在建立媒体连接…")
            val connectionStarted = android.os.SystemClock.elapsedRealtime()
            timer = scope.launch {
                var sampleTime = android.os.SystemClock.elapsedRealtime()
                while (isActive && peer === pc && !closing) {
                    val now = android.os.SystemClock.elapsedRealtime()
                    if (!connected && now - connectionStarted > 15000) {
                        scope.launch { abort("媒体连接超时，请确认两台设备的网络互通") }
                        return@launch
                    }
                    if (connected) {
                        sendRaw(JSONObject().put("type", "heartbeat").put("active", active).put("epoch", epoch))
                        sendRaw(JSONObject().put("type", "ping").put("time", now))
                        if (now - lastPong > 8000) { scope.launch { abort("连接超时") }; return@launch }
                        if (now - sampleTime >= 1000) {
                            val fps = frameCount.getAndSet(0) * 1000.0 / (now - sampleTime)
                            sampleTime = now
                            pc.getStats { report ->
                                val videoStats = report.statsMap.values.firstOrNull {
                                    it.type == "inbound-rtp" && (it.members["kind"] == "video" || it.members["mediaType"] == "video")
                                }
                                val decoded = (videoStats?.members?.get("framesDecoded") as? Number)?.toDouble() ?: 0.0
                                val total = (videoStats?.members?.get("totalDecodeTime") as? Number)?.toDouble() ?: 0.0
                                val decoder = videoStats?.members?.get("decoderImplementation") ?: "MediaCodec"
                                val decodeMs = if (decoded > 0) total * 1000 / decoded else 0.0
                                scope.launch { changed("%.0f FPS · RTT %.1f ms · 解码 %.1f ms\n%s → %s".format(fps, rtt, decodeMs, encoder, decoder)) }
                            }
                        }
                    }
                    delay(500)
                }
            }
        } catch (error: Exception) {
            cleanup()
            throw error
        }
    }

    private fun settings(config: StreamConfig) = JSONObject().put("width", config.width).put("height", config.height)
        .put("fps", config.fps).put("bitrate", config.bitrate).put("audio", true).put("render_scale", 100)

    suspend fun apply(config: StreamConfig) {
        VideoSupport.validate(config)
        val signaling = signal ?: error("尚未连接")
        val id = sessionId ?: error("尚未连接")
        withContext(Dispatchers.IO) { signaling.request("POST", "/v1/session/$id/settings", settings(config)) }
    }

    private suspend fun createOffer(pc: PeerConnection): SessionDescription = suspendCancellableCoroutine { continuation ->
        pc.createOffer(object : SimpleSdpObserver() {
            override fun onCreateSuccess(description: SessionDescription) { if (continuation.isActive) continuation.resume(description) }
            override fun onCreateFailure(error: String) { if (continuation.isActive) continuation.resumeWithException(IllegalStateException(error)) }
        }, MediaConstraints())
    }

    private suspend fun setDescription(pc: PeerConnection, description: SessionDescription, local: Boolean): Unit = suspendCancellableCoroutine { continuation ->
        val observer = object : SimpleSdpObserver() {
            override fun onSetSuccess() { if (continuation.isActive) continuation.resume(Unit) }
            override fun onSetFailure(error: String) { if (continuation.isActive) continuation.resumeWithException(IllegalStateException(error)) }
        }
        if (local) pc.setLocalDescription(observer, description) else pc.setRemoteDescription(observer, description)
    }

    private fun receive(raw: String) {
        runCatching {
            val event = JSONObject(raw)
            when (event.optString("type")) {
                "pong" -> {
                    val now = android.os.SystemClock.elapsedRealtime()
                    rtt = (now - event.getDouble("time")).coerceAtLeast(0.0)
                    lastPong = now
                    encoder = event.optString("encoder", "--")
                }
                "warning" -> failed(event.optString("message"))
                "ended" -> scope.launch { abort("远程会话已结束") }
                "clipboard" -> {
                    preferences.edit().putString("remoteClipboard", event.optString("text").take(64000)).apply()
                }
            }
        }
    }

    fun focus(value: Boolean) {
        if (value == active) return
        active = value
        epoch++
        sendRaw(JSONObject().put("type", "heartbeat").put("active", active).put("epoch", epoch))
    }

    fun send(event: JSONObject, reliable: Boolean = true) {
        if (!connected || !active) return
        event.put("epoch", epoch).put("seq", ++sequence)
        sendRaw(event, reliable)
    }

    fun sendRaw(event: JSONObject, reliable: Boolean = true) {
        val channel = if (reliable) control else motion
        if (channel?.state() != DataChannel.State.OPEN || channel.bufferedAmount() > 16384) return
        channel.send(DataChannel.Buffer(ByteBuffer.wrap(event.toString().toByteArray(Charsets.UTF_8)), false))
    }

    private suspend fun abort(message: String) {
        if (closing) return
        failed(message)
        disconnect()
        ended()
    }

    suspend fun disconnect() = mutex.withLock { cleanup() }

    private suspend fun cleanup() = withContext(NonCancellable) {
        closing = true
        focus(false)
        sendRaw(JSONObject().put("type", "disconnect"))
        connected = false
        timer?.cancel()
        timer = null
        val signaling = signal
        val id = sessionId
        sessionId = null
        signal = null
        withContext(Dispatchers.IO + NonCancellable) {
            if (signaling != null && id != null) runCatching { signaling.request("DELETE", "/v1/session/$id") }
        }
        control?.unregisterObserver()
        control?.close(); control?.dispose(); control = null
        motion?.close(); motion?.dispose(); motion = null
        peer?.close(); peer?.dispose(); peer = null
    }

    fun dispose() {
        if (!disposed.compareAndSet(false, true)) return
        scope.launch {
            disconnect()
            renderer.release()
            factory.dispose()
            audio.release()
            egl.release()
            scope.cancel()
        }
    }
}

open class SimpleSdpObserver : SdpObserver {
    override fun onCreateSuccess(description: SessionDescription) {}
    override fun onSetSuccess() {}
    override fun onCreateFailure(error: String) {}
    override fun onSetFailure(error: String) {}
}
