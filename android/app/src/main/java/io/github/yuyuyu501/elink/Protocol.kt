package io.github.yuyuyu501.elink

import java.net.URI
import kotlin.math.roundToInt

data class StreamConfig(val width: Int = 1920, val height: Int = 1080, val fps: Int = 60, val bitrate: Int = 20)

data class HostAddress(val host: String, val port: Int = 49200) {
    val authority: String get() = (if (host.contains(':')) "[$host]" else host) + ":$port"
    companion object {
        fun parse(value: String): HostAddress {
            require(value.isNotBlank() && value.length <= 300 && value.none { it.isWhitespace() || it in "/\\?#%@" }) { "地址格式无效" }
            val uri = URI("https://$value")
            val host = uri.host?.removeSurrounding("[", "]") ?: error("请输入 IP 或主机名")
            val port = if (uri.port == -1) 49200 else uri.port
            require(port in 1024..65535 && uri.rawUserInfo == null) { "端口格式无效" }
            return HostAddress(host, port)
        }
    }
}

fun videoPoint(x: Float, y: Float, viewWidth: Int, viewHeight: Int, videoWidth: Int, videoHeight: Int): Pair<Int, Int>? {
    if (viewWidth <= 0 || viewHeight <= 0 || videoWidth <= 0 || videoHeight <= 0) return null
    val scale = minOf(viewWidth.toFloat() / videoWidth, viewHeight.toFloat() / videoHeight)
    val width = videoWidth * scale
    val height = videoHeight * scale
    val left = (viewWidth - width) / 2
    val top = (viewHeight - height) / 2
    if (x < left || y < top || x > left + width || y > top + height) return null
    return ((x - left) / width * 65535).roundToInt().coerceIn(0, 65535) to
        ((y - top) / height * 65535).roundToInt().coerceIn(0, 65535)
}
