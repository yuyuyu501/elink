package io.github.yuyuyu501.elink

import android.content.SharedPreferences
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.security.MessageDigest
import java.security.cert.X509Certificate
import java.util.concurrent.TimeUnit
import javax.net.ssl.SSLContext
import javax.net.ssl.X509TrustManager

class Signaling(val address: HostAddress, private val preferences: SharedPreferences, probe: Boolean = false) {
    private var fingerprint: String? = preferences.getString("pin:${address.authority}", null)
    private var observed: String? = null
    private val trust = object : X509TrustManager {
        override fun getAcceptedIssuers(): Array<X509Certificate> = emptyArray()
        override fun checkClientTrusted(chain: Array<X509Certificate>, authType: String) = error("Client certificates unsupported")
        override fun checkServerTrusted(chain: Array<X509Certificate>, authType: String) {
            require(chain.isNotEmpty())
            val digest = MessageDigest.getInstance("SHA-256").digest(chain[0].encoded).joinToString("") { "%02x".format(it) }
            if (fingerprint != null && fingerprint != digest) throw java.security.cert.CertificateException("设备身份已更改；确认设备后删除并重新添加")
            observed = digest
        }
    }
    private val context = SSLContext.getInstance("TLS").apply { init(null, arrayOf(trust), null) }
    private val client = OkHttpClient.Builder().sslSocketFactory(context.socketFactory, trust)
        .hostnameVerifier { _, session ->
            val cert = session.peerCertificates.firstOrNull() as? X509Certificate
            val digest = cert?.let { MessageDigest.getInstance("SHA-256").digest(it.encoded).joinToString("") { b -> "%02x".format(b) } }
            digest != null && digest == (fingerprint ?: observed)
        }.followRedirects(false).followSslRedirects(false)
        .connectTimeout(if (probe) 2 else 8, TimeUnit.SECONDS)
        .readTimeout(if (probe) 3 else 40, TimeUnit.SECONDS)
        .apply { if (probe) callTimeout(4, TimeUnit.SECONDS) }.build()

    fun request(method: String, path: String, value: JSONObject? = null, presence: String? = null): JSONObject {
        require(path.startsWith("/v1/"))
        val body = value?.toString()?.toRequestBody("application/json".toMediaType())
        val request = Request.Builder().url("https://${address.authority}$path").method(method, body)
            .apply { if (presence != null) header("X-Elink-Presence", presence) }.build()
        client.newCall(request).execute().use { response ->
            val source = response.body ?: error("主机响应为空")
            source.byteStream().use { stream ->
                val output = java.io.ByteArrayOutputStream()
                val chunk = ByteArray(8192)
                while (output.size() <= 1_048_576) {
                    val count = stream.read(chunk)
                    if (count < 0) break
                    output.write(chunk, 0, count)
                }
                val bytes = output.toByteArray()
                require(bytes.size <= 1_048_576) { "主机响应过大" }
                val result = JSONObject(String(bytes, Charsets.UTF_8))
                check(response.isSuccessful) { result.optString("error", "连接失败 HTTP ${response.code}") }
                return result
            }
        }
    }

    fun info(presence: String? = null): JSONObject {
        val result = request("GET", "/v1/info", presence = presence)
        require(result.optString("protocol") == "elink" && result.optInt("version") == 2) { "不兼容的 Elink 主机" }
        if (fingerprint == null) {
            fingerprint = observed ?: error("未取得主机证书")
            preferences.edit().putString("pin:${address.authority}", fingerprint).apply()
        }
        return result
    }
}
