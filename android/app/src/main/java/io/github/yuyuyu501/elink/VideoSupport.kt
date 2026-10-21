package io.github.yuyuyu501.elink

import android.media.MediaCodecInfo
import android.media.MediaCodecList

object VideoSupport {
    val dimensions = listOf(1280 to 720, 1920 to 1080, 2560 to 1440)
    private val decoders: List<MediaCodecInfo.VideoCapabilities> by lazy {
        MediaCodecList(MediaCodecList.REGULAR_CODECS).codecInfos.filter { !it.isEncoder && it.isHardwareAccelerated }
            .mapNotNull { codec -> runCatching { codec.getCapabilitiesForType("video/avc").videoCapabilities }.getOrNull() }
    }

    fun supports(width: Int, height: Int, fps: Int) = decoders.any {
        runCatching { it.areSizeAndRateSupported(width, height, fps.toDouble()) }.getOrDefault(false)
    }

    fun rates(size: Pair<Int, Int>) = listOf(30, 60, 90, 120).filter { supports(size.first, size.second, it) }

    fun validate(config: StreamConfig) {
        check(supports(config.width, config.height, config.fps)) { "当前设备未声明支持此 H.264 分辨率和帧率组合" }
    }
}
