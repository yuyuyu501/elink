package io.github.yuyuyu501.elink

import android.content.Context
import android.net.wifi.WifiManager
import android.os.Build
import android.os.SystemClock
import android.util.Base64
import kotlinx.coroutines.*
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import org.json.JSONArray
import org.json.JSONObject
import java.net.*
import java.security.MessageDigest
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap

internal data class LanDevice(
    val id: String, val name: String, val address: String, val port: Int,
    val platform: String, val roles: List<String>, val busy: Boolean = false
) {
    val connectable get() = "host" in roles
    val authority get() = if (connectable) "$address:$port" else address

    companion object {
        fun valid(id: String, name: String, address: String, port: Int,
                  platform: String, roles: List<String>, busy: Boolean = false): LanDevice? {
            if (!id.matches(Regex("[0-9a-f]{64}")) || name.length !in 1..80 || roles.isEmpty() ||
                roles.size > 2 || roles.any { it !in listOf("host", "controller") } ||
                (if ("host" in roles) port !in 1024..65535 else port != 0)) return null
            return LanDevice(id, name, address, port, platform, roles.distinct(), busy)
        }
    }
}

/** Bidirectional presence: requests introduce controller-only devices too. */
internal class DeviceDiscovery(context: Context, private val diagnostics: Diagnostics = Diagnostics(context)) {
    private val preferences = context.getSharedPreferences("elink", Context.MODE_PRIVATE)
    private val id = preferences.getString("discoveryId", null) ?: MessageDigest.getInstance("SHA-256")
        .digest(UUID.randomUUID().toString().toByteArray()).joinToString("") { "%02x".format(it) }
        .also { preferences.edit().putString("discoveryId", it).apply() }
    private val own = JSONObject().put("protocol", "elink").put("version", 2).put("host_id", id)
        .put("name", Build.MODEL.take(80).ifBlank { "Android" }).put("port", 0)
        .put("capabilities", JSONObject().put("platform", "android").put("roles", JSONArray().put("controller")))
    private data class Seen(val device: LanDevice, val time: Long)
    private val peers = ConcurrentHashMap<String, Seen>()
    private val scanLock = Mutex()
    private var listener: DatagramSocket? = null
    private var job: Job? = null
    private val wifiLock = (context.applicationContext.getSystemService(Context.WIFI_SERVICE) as? WifiManager)
        ?.createMulticastLock("elink-presence")?.apply { setReferenceCounted(false) }

    fun introduction(): String = Base64.encodeToString(own.toString().toByteArray(Charsets.UTF_8), Base64.NO_WRAP)
    fun localDevice() = LanDevice(id, own.getString("name"), localAddresses().joinToString(" · "),
        0, "android", listOf("controller"))
    fun localAddresses() = interfaces().mapNotNull { it.address.hostAddress }.distinct()
    fun report() = "listener=${listener?.isBound == true && listener?.isClosed == false}; " +
        "interfaces=${interfaces().joinToString { "${it.address.hostAddress}/${it.networkPrefixLength}" }}; " +
        "peers=${snapshot().joinToString { "${it.platform}:${it.authority}:${it.roles}" }}"

    private fun interfaces(): List<InterfaceAddress> = try {
        NetworkInterface.getNetworkInterfaces().toList()
            .filter { it.isUp && !it.isLoopback }.flatMap { it.interfaceAddresses }
            .filter { it.address is Inet4Address && it.networkPrefixLength in 1..32 }
    } catch (failure: SocketException) {
        diagnostics.record("discovery.interfaces", "Cannot enumerate network interfaces", failure)
        emptyList()
    }

    private fun sameNetwork(address: InetAddress, local: InterfaceAddress): Boolean {
        if (address !is Inet4Address) return false
        val prefix = local.networkPrefixLength.toInt()
        val shift = 32 - prefix
        fun bits(ip: InetAddress) = ip.address.fold(0L) { value, byte -> (value shl 8) or (byte.toLong() and 255) }
        return (bits(address) ushr shift) == (bits(local.address) ushr shift)
    }

    private fun allowed(address: InetAddress): Boolean = interfaces().any { sameNetwork(address, it) }

    private fun remember(info: JSONObject?, source: InetAddress) {
        if (info == null || !allowed(source) || info.optString("protocol") != "elink" || info.optInt("version") != 2) return
        val caps = info.optJSONObject("capabilities")
        if (info.has("capabilities") && caps == null) return
        val rolesJson = caps?.optJSONArray("roles")
        if (caps?.has("roles") == true && rolesJson == null) return
        val roles = rolesJson?.let { (0 until it.length()).map { index -> it.optString(index) } }
            ?: listOf("controller", "host")
        val device = LanDevice.valid(info.optString("host_id"), info.optString("name"), source.hostAddress ?: return,
            info.optInt("port", -1), caps?.optString("platform") ?: "", roles, info.optBoolean("busy")) ?: return
        if (device.id == id) return
        if (peers.size >= 512) snapshot()
        val key = "${device.id}@${device.address}"
        if (peers.containsKey(key) || peers.size < 512) {
            if (!peers.containsKey(key)) diagnostics.record("discovery.learned", "${device.platform} ${device.authority} ${device.roles}")
            peers[key] = Seen(device, SystemClock.elapsedRealtime())
        }
    }

    fun snapshot(): List<LanDevice> {
        val now = SystemClock.elapsedRealtime()
        peers.entries.removeAll { now - it.value.time > 35000 || !allowed(InetAddress.getByName(it.value.device.address)) }
        return peers.values.map { it.device }.groupBy { it.id }.values
            .map { routes -> routes.sortedBy { it.address }.first() }.sortedBy { it.name.lowercase() }
    }

    fun start(scope: CoroutineScope) {
        if (job != null) return
        runCatching { wifiLock?.acquire() }.onFailure { diagnostics.record("discovery.lock", "Wi-Fi reception lock unavailable", it) }
        job = scope.launch(Dispatchers.IO) {
          while (isActive) {
            try {
                DatagramSocket(null).use { socket ->
                    listener = socket
                    socket.broadcast = true
                    socket.bind(InetSocketAddress("0.0.0.0", 49201))
                    diagnostics.record("discovery.listen", "UDP 49201 ready; ${localAddresses()}")
                    socket.soTimeout = 500
                    var window = 0L
                    var count = 0
                    while (isActive) {
                        val packet = DatagramPacket(ByteArray(2049), 2049)
                        try {
                            socket.receive(packet)
                            if (packet.length > 2048 || !allowed(packet.address)) continue
                            val query = JSONObject(String(packet.data, 0, packet.length, Charsets.UTF_8))
                            val nonce = query.optString("nonce")
                            if (query.optString("protocol") != "elink-discovery-v2" || nonce.length != 32) continue
                            val now = SystemClock.elapsedRealtime()
                            if (now - window >= 1000) { window = now; count = 0 }
                            if (count++ >= 50) continue
                            remember(query.optJSONObject("device"), packet.address)
                            val response = JSONObject(own.toString()).put("nonce", nonce).toString().toByteArray()
                            socket.send(DatagramPacket(response, response.size, packet.address, packet.port))
                        } catch (_: SocketTimeoutException) {
                        } catch (failure: Exception) {
                            if (socket.isClosed) break
                            diagnostics.record("discovery.packet", "Presence packet failed", failure)
                        }
                    }
                }
            } catch (failure: Exception) {
                if (isActive) diagnostics.record("discovery.listen", "Cannot start presence listener; retry in 5 seconds", failure)
            } finally { listener = null }
            delay(5000)
          }
        }
    }

    suspend fun scan(): List<LanDevice> = scanLock.withLock {
        withContext(Dispatchers.IO) {
            val nonce = UUID.randomUUID().toString().replace("-", "")
            val query = JSONObject().put("protocol", "elink-discovery-v2").put("nonce", nonce)
                .put("device", own).toString().toByteArray()
            val sockets = mutableListOf<DatagramSocket>()
            try {
                val known = snapshot().map { InetAddress.getByName(it.address) }.toSet()
                for (local in interfaces()) {
                    runCatching {
                        val socket = DatagramSocket(InetSocketAddress(local.address, 0))
                        sockets.add(socket)
                        socket.broadcast = true
                        socket.soTimeout = 60
                        val targets = known.filter { sameNetwork(it, local) }.toMutableSet()
                        local.broadcast?.let { targets.add(it) }
                        for (target in targets) runCatching {
                            socket.send(DatagramPacket(query, query.size, target, 49201))
                        }.onFailure { diagnostics.record("discovery.send", "Discovery send failed on ${local.address}", it) }
                    }.onFailure { diagnostics.record("discovery.interface", "Discovery interface failed", it) }
                }
                val deadline = SystemClock.elapsedRealtime() + 1100
                while (SystemClock.elapsedRealtime() < deadline && sockets.isNotEmpty()) {
                    ensureActive()
                    for (socket in sockets) {
                        val packet = DatagramPacket(ByteArray(2049), 2049)
                        try {
                            socket.receive(packet)
                            if (packet.length > 2048) continue
                            val info = JSONObject(String(packet.data, 0, packet.length, Charsets.UTF_8))
                            if (info.optString("nonce") == nonce) remember(info, packet.address)
                        } catch (_: SocketTimeoutException) {
                        } catch (failure: Exception) { diagnostics.record("discovery.reply", "Discovery reply failed", failure) }
                    }
                }
            } finally { sockets.forEach { it.close() } }
            snapshot()
        }
    }

    fun close() {
        listener?.close()
        job?.cancel(); job = null
        if (wifiLock?.isHeld == true) wifiLock.release()
    }
}
