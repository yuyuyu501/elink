package io.github.yuyuyu501.elink

import android.app.Activity
import android.app.AlertDialog
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.res.ColorStateList
import android.graphics.Color
import android.graphics.drawable.GradientDrawable
import android.graphics.drawable.RippleDrawable
import android.net.Uri
import android.os.Bundle
import android.view.*
import android.widget.*
import kotlinx.coroutines.*
import kotlinx.coroutines.sync.Semaphore
import kotlinx.coroutines.sync.withPermit
import org.json.JSONObject
import org.webrtc.SurfaceViewRenderer

class MainActivity : Activity() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val preferences by lazy { getSharedPreferences("elink", MODE_PRIVATE) }
    private var session: StreamSession? = null
    private var input: RemoteInputView? = null
    private var renderer: SurfaceViewRenderer? = null
    private var connecting = false
    private var connectJob: Job? = null
    private var refreshJob: Job? = null
    private var stopping = false
    private var resumed = false
    private var popup: PopupWindow? = null
    private var config = StreamConfig()
    private lateinit var content: FrameLayout
    private lateinit var devices: LinearLayout
    private var status: TextView? = null
    private var virtualPad: LinearLayout? = null
    private var errorDialog: AlertDialog? = null
    private val diagnostics by lazy { Diagnostics(this) }
    private val discovery by lazy { DeviceDiscovery(this, diagnostics) }
    private var presenceJob: Job? = null
    private lateinit var pageBody: LinearLayout
    private var page = "设备"
    private data class DeviceRow(val address: String, val name: String, val detail: String,
                                 val online: Boolean, val connectable: Boolean, val saved: Boolean,
                                 val busy: Boolean = false)
    private var deviceRows = emptyList<DeviceRow>()
    private var discoveryStatus = "正在查找设备…"
    private var pendingReport: String? = null
    private val accent = Color.rgb(40, 85, 235)
    private val muted = Color.rgb(103, 119, 141)

    override fun onCreate(state: Bundle?) {
        super.onCreate(state)
        config = StreamConfig(preferences.getInt("width", 1920), preferences.getInt("height", 1080),
            preferences.getInt("fps", 60), preferences.getInt("bitrate", 20))
        home()
        discovery.start(scope)
        presenceJob = scope.launch {
            while (isActive) {
                delay(10000)
                if (resumed && refreshJob?.isActive != true) refresh()
            }
        }
    }

    private fun dp(value: Int) = (value * resources.displayMetrics.density).toInt()
    private fun label(text: String, size: Float = 16f) = TextView(this).apply {
        this.text = text; textSize = size; setTextColor(Color.rgb(23, 43, 70)); setPadding(0, dp(8), 0, dp(8))
    }
    private fun surface(fill: Int = Color.WHITE) = GradientDrawable().apply {
        setColor(fill); cornerRadius = dp(8).toFloat(); setStroke(dp(1), Color.rgb(222, 229, 240))
    }
    private fun buttonSurface(fill: Int = Color.WHITE) = RippleDrawable(
        ColorStateList.valueOf(Color.argb(35, 40, 85, 235)), surface(fill), surface())
    private fun command(text: String, action: () -> Unit) = Button(this).apply {
        this.text = text; isAllCaps = false; textSize = 14f; minHeight = dp(48)
        setTextColor(accent); background = buttonSurface(); setPadding(dp(12), dp(4), dp(12), dp(4))
        setOnClickListener { action() }
    }

    private fun home() {
        content = FrameLayout(this).apply { setBackgroundColor(Color.rgb(244, 246, 251)) }
        content.setOnApplyWindowInsetsListener { view, insets ->
            val bars = insets.getInsets(WindowInsets.Type.systemBars() or WindowInsets.Type.displayCutout())
            view.setPadding(bars.left, bars.top, bars.right, bars.bottom)
            insets
        }
        val layout = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        layout.addView(label("Elink", 26f).apply {
            setTypeface(typeface, android.graphics.Typeface.BOLD); setPadding(dp(20), dp(12), dp(20), dp(12))
        })
        val scroll = ScrollView(this).apply { isFillViewport = true }
        pageBody = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL; setPadding(dp(20), dp(4), dp(20), dp(20))
        }
        scroll.addView(pageBody)
        layout.addView(scroll, LinearLayout.LayoutParams(-1, 0, 1f))
        val navigation = LinearLayout(this).apply {
            setPadding(dp(12), dp(8), dp(12), dp(8)); setBackgroundColor(Color.WHITE)
        }
        val tabs = listOf("设备" to android.R.drawable.ic_menu_myplaces,
            "设置" to android.R.drawable.ic_menu_preferences, "关于" to android.R.drawable.ic_menu_info_details)
        for ((name, icon) in tabs) {
            val button = command(name) { page = name; home() }.apply {
                val drawable = getDrawable(icon)?.mutate()?.apply {
                    setBounds(0, 0, dp(24), dp(24)); setTint(if (page == name) accent else muted)
                }
                setCompoundDrawables(null, drawable, null, null)
                compoundDrawablePadding = dp(4)
                setTextColor(if (page == name) accent else muted)
                isSelected = page == name
                background = buttonSurface(if (isSelected) Color.rgb(233, 239, 255) else Color.WHITE)
                minHeight = dp(68)
                contentDescription = name
            }
            navigation.addView(button, LinearLayout.LayoutParams(0, -2, 1f))
        }
        layout.addView(navigation)
        content.addView(layout)
        setContentView(content)
        window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        window.decorView.systemUiVisibility = View.SYSTEM_UI_FLAG_VISIBLE
        status = null
        when (page) {
            "设置" -> settingsPage()
            "关于" -> aboutPage()
            else -> {
                val actions = LinearLayout(this)
                actions.addView(label("在线设备", 21f), LinearLayout.LayoutParams(0, -2, 1f))
                actions.addView(command("添加设备") { addDevice() })
                actions.addView(ImageButton(this).apply {
                    setImageResource(android.R.drawable.ic_popup_sync); contentDescription = "刷新设备"
                    background = null; setOnClickListener { refresh() }
                }, LinearLayout.LayoutParams(dp(48), dp(48)))
                pageBody.addView(actions)
                status = label(discoveryStatus, 13f).apply { setTextColor(muted) }.also { pageBody.addView(it) }
                devices = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
                pageBody.addView(devices)
                renderDevices()
            }
        }
        if (resumed && refreshJob?.isActive != true) refresh()
    }

    private fun saved() = preferences.getStringSet("devices", emptySet())!!.toMutableSet()
    private fun renderDevices() {
        if (session != null || page != "设备" || !::devices.isInitialized) return
        devices.removeAllViews()
        val local = discovery.localDevice()
        device(DeviceRow(local.address.ifBlank { "暂无 IPv4 网络" }, local.name,
            "Android · 本机 · 仅控制端", true, false, false))
        deviceRows.filter { it.online }.forEach { device(it) }
        if (deviceRows.none { it.online }) devices.addView(label("暂未发现其它在线设备", 14f).apply { setTextColor(muted) })
        val offline = deviceRows.filter { !it.online }
        if (offline.isNotEmpty()) {
            devices.addView(label("已保存 · 离线", 15f))
            offline.forEach { device(it) }
        }
    }
    private fun device(row: DeviceRow) {
        val card = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL; background = surface()
            setPadding(dp(16), dp(10), dp(16), dp(14))
        }
        val heading = LinearLayout(this).apply { gravity = Gravity.CENTER_VERTICAL }
        heading.addView(label(row.name, 18f).apply { setTypeface(typeface, android.graphics.Typeface.BOLD) },
            LinearLayout.LayoutParams(0, -2, 1f))
        heading.addView(label(if (row.online) "● 在线" else "离线", 12f).apply {
            setTextColor(if (row.online) Color.rgb(25, 135, 91) else muted)
        })
        card.addView(heading)
        card.addView(label(row.detail, 13f).apply { setTextColor(muted) })
        card.addView(label(row.address, 13f).apply { setTextColor(muted); setTextIsSelectable(true) })
        card.addView(command(if (!row.online) "设备离线" else if (!row.connectable) "仅控制端 · 不支持被控"
                            else if (row.busy) "控制中 · 暂不可连接" else "连接") {
            connect(row.address)
        }.apply {
            isEnabled = row.online && row.connectable && !row.busy
            if (isEnabled) { setTextColor(Color.WHITE); background = buttonSurface(accent) }
            else { setTextColor(muted); background = surface(Color.rgb(242, 245, 250)) }
        }, LinearLayout.LayoutParams(-1, dp(48)))
        if (row.saved) card.setOnLongClickListener {
            AlertDialog.Builder(this).setTitle(row.name).setMessage("删除此设备和已保存的设备身份？")
                .setPositiveButton("删除") { _, _ ->
                    val authority = HostAddress.parse(row.address).authority
                    preferences.edit().putStringSet("devices", saved().apply {
                        removeAll { runCatching { HostAddress.parse(it).authority }.getOrNull() == authority }
                    }).remove("pin:$authority").remove("name:${row.address}").apply()
                    deviceRows = deviceRows.filter { it.address != row.address }; renderDevices(); refresh()
                }.setNegativeButton("取消", null).show()
            true
        }
        devices.addView(card, LinearLayout.LayoutParams(-1, -2).apply { bottomMargin = dp(12) })
    }

    private fun settingsPage() {
        pageBody.addView(label("设置", 23f))
        pageBody.addView(label("画质", 17f))
        pageBody.addView(command("${config.width} × ${config.height}   ·   ${config.fps} FPS   ·   ${config.bitrate} Mbps") { settings() },
            LinearLayout.LayoutParams(-1, -2))
        pageBody.addView(label("输入", 17f))
        pageBody.addView(Switch(this).apply {
            text = "直接触控"; minHeight = dp(56); isChecked = preferences.getBoolean("directTouch", true)
            setOnCheckedChangeListener { _, enabled -> preferences.edit().putBoolean("directTouch", enabled).apply() }
        })
        pageBody.addView(label("关闭后使用触控板模式。连接后的控制中心提供游戏鼠标、键盘、剪贴板和虚拟手柄。", 13f).apply { setTextColor(muted) })
        pageBody.addView(label("网络", 17f))
        pageBody.addView(label("自动发现同一局域网设备", 15f))
        pageBody.addView(label("本机地址\n${discovery.localAddresses().joinToString("\n").ifBlank { "暂无 IPv4 地址" }}", 13f).apply {
            setTextColor(muted); setTextIsSelectable(true)
        })
    }

    private fun aboutPage() {
        pageBody.addView(label("关于 Elink", 23f))
        pageBody.addView(label("${BuildConfig.VERSION_NAME} · Android Preview", 15f))
        pageBody.addView(label("Android 当前支持控制 Windows 和 Linux；本机作为仅控制端显示。", 14f).apply { setTextColor(muted) })
        pageBody.addView(command("查看更新") { openLink("https://github.com/yuyuyu501/elink/releases") },
            LinearLayout.LayoutParams(-1, dp(52)))
        pageBody.addView(command("项目主页") { openLink("https://github.com/yuyuyu501/elink") },
            LinearLayout.LayoutParams(-1, dp(52)).apply { topMargin = dp(8) })
        pageBody.addView(label("诊断", 17f))
        pageBody.addView(command("导出诊断日志") {
            pendingReport = diagnostics.report(discovery.report())
            startActivityForResult(Intent(Intent.ACTION_CREATE_DOCUMENT).apply {
                addCategory(Intent.CATEGORY_OPENABLE); type = "text/plain"
                putExtra(Intent.EXTRA_TITLE, "Elink-Android-diagnostics.txt")
            }, 1001)
        }, LinearLayout.LayoutParams(-1, dp(52)))
        pageBody.addView(label("日志包含设备地址、发现结果和错误详情，可用于排查设备未显示或连接失败。", 13f).apply { setTextColor(muted) })
    }

    private fun openLink(url: String) {
        runCatching { startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url))) }
            .onFailure { error("无法打开浏览器", it) }
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == 1001) {
            val report = pendingReport
            pendingReport = null
            if (resultCode == RESULT_OK && report != null) runCatching {
                val uri = data?.data ?: throw IllegalStateException("未选择文件")
                contentResolver.openOutputStream(uri, "wt")?.bufferedWriter()?.use { it.write(report) }
                    ?: throw IllegalStateException("无法写入文件")
                Toast.makeText(this, "诊断日志已保存", Toast.LENGTH_SHORT).show()
            }.onFailure { error("日志导出失败", it) }
        }
    }

    private fun addDevice() {
        val field = EditText(this).apply { hint = "IP / MagicDNS / IP:端口"; isSingleLine = true }
        val dialog = AlertDialog.Builder(this).setTitle("添加设备").setView(field)
            .setPositiveButton("添加", null).setNegativeButton("取消", null).create()
        dialog.setOnShowListener { dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener {
            runCatching {
                val address = field.text.toString().trim()
                HostAddress.parse(address)
                preferences.edit().putStringSet("devices", saved().apply { add(address) }).apply()
                refresh(); dialog.dismiss()
            }.onFailure { field.error = it.message }
        } }
        dialog.show()
    }

    private fun refresh() {
        refreshJob?.cancel()
        refreshJob = scope.launch {
          try {
            if (session == null && page == "设备") status?.text = "正在查找设备…"
            val found = discovery.scan()
            fun canonical(address: String) = runCatching { HostAddress.parse(address).authority }.getOrDefault(address)
            val online = found.filter { it.connectable }.associateBy { canonical(it.authority) }
            // Renew live peers before probing saved endpoints that may be offline.
            val addresses = (online.keys.sorted() + saved().map { canonical(it) }.sorted()).distinct().take(128)
            val introduction = discovery.introduction()
            val permits = Semaphore(4)
            val checked = withContext(Dispatchers.IO) {
                val results = java.util.concurrent.ConcurrentHashMap<String, JSONObject>()
                // Offline saved addresses must not postpone the next live-presence renewal.
                withTimeoutOrNull(8000) {
                    addresses.map { address -> async {
                        permits.withPermit {
                            ensureActive()
                            runCatching {
                                Signaling(HostAddress.parse(address), preferences, probe = true).info(introduction)
                            }.onSuccess {
                                results[address] = it
                                diagnostics.record("discovery.https", "$address presence_received=${it.optBoolean("presence_received")}; features=${it.optJSONArray("features")}")
                            }.onFailure { diagnostics.record("discovery.https", "$address introduction failed", it) }
                        }
                    } }.awaitAll()
                }
                results.toMap()
            }
            ensureActive()
            val rows = mutableListOf<DeviceRow>()
            val displayedIds = mutableSetOf<String>()
            for (address in addresses.sorted()) {
                val info = checked[address]
                val peer = online[address]
                val identity = peer?.id ?: info?.optString("host_id")?.takeIf { it.isNotEmpty() }
                if (identity != null && !displayedIds.add(identity)) continue
                val name = peer?.name ?: info?.optString("name") ?: preferences.getString("name:$address", address)!!
                if (info != null) preferences.edit().putString("name:$address", name).apply()
                val busy = peer?.busy == true || info?.optBoolean("busy") == true
                val platform = peer?.platform ?: info?.optJSONObject("capabilities")?.optString("platform").orEmpty()
                val roles = info?.optJSONObject("capabilities")?.optJSONArray("roles")
                val canHost = peer?.connectable ?: (roles == null || (0 until roles.length()).any { roles.optString(it) == "host" })
                rows.add(DeviceRow(address, name, "${platform.ifBlank { "桌面设备" }} · ${if (busy) "控制中" else if (canHost) "可连接" else "仅控制端"}",
                    peer != null || info != null, canHost, saved().any { canonical(it) == address }, busy))
            }
            for (peer in found.filter { !it.connectable }) {
                if (displayedIds.add(peer.id)) rows.add(DeviceRow(peer.address, peer.name, "${peer.platform} · 仅控制端", true, false, false))
            }
            deviceRows = rows
            discoveryStatus = "${rows.count { it.online } + 1} 台在线 · 每 10 秒自动刷新"
            if (session == null && page == "设备") { renderDevices(); status?.text = discoveryStatus }
          } catch (failure: Exception) {
              if (failure is CancellationException) throw failure
              diagnostics.record("discovery.refresh", "刷新失败", failure)
              discoveryStatus = "查找失败，可在关于中导出诊断日志"
              if (session == null && page == "设备") status?.text = discoveryStatus
          }
        }
    }

    private fun connect(address: String) {
        if (connecting || session != null) return
        connecting = true
        status?.text = "正在连接 $address…"
        refreshJob?.cancel()
        connectJob = scope.launch {
            var stream: StreamSession? = null
            try {
                VideoSupport.validate(config)
                val view = SurfaceViewRenderer(this@MainActivity)
                stream = StreamSession(this@MainActivity, preferences, view,
                    { text -> status?.text = text }, { message -> error(message) }, { disconnect() })
                session = stream
                renderer = view
                val endpoint = HostAddress.parse(address)
                streaming(view, stream)
                stream.connect(endpoint, config)
            } catch (failure: Exception) {
                stream?.dispose()
                if (session === stream && !stopping) {
                    session = null; renderer = null; input = null
                    if (!isFinishing && !isDestroyed) home()
                }
                if (failure !is CancellationException) error(failure.message ?: "连接失败", failure)
            } finally { connecting = false }
        }
    }

    private fun streaming(view: SurfaceViewRenderer, stream: StreamSession) {
        content = FrameLayout(this).apply { setBackgroundColor(Color.BLACK) }
        content.setOnApplyWindowInsetsListener { view, insets ->
            val cutout = insets.getInsets(WindowInsets.Type.displayCutout())
            view.setPadding(cutout.left, cutout.top, cutout.right, cutout.bottom)
            insets
        }
        content.addView(view, FrameLayout.LayoutParams(-1, -1))
        input = RemoteInputView(this, stream).also {
            it.directTouch = preferences.getBoolean("directTouch", true)
            content.addView(it, FrameLayout.LayoutParams(-1, -1))
        }
        status = label("正在连接…", 12f).apply {
            setTextColor(Color.WHITE); setBackgroundColor(0x66000000)
            content.addView(this, FrameLayout.LayoutParams(-2, -2, Gravity.TOP or Gravity.START))
        }
        val menu = ImageButton(this).apply {
            setImageResource(android.R.drawable.ic_menu_manage); contentDescription = "控制中心"
            setOnClickListener { if (popup?.isShowing == true) popup?.dismiss() else controls(this) }
        }
        content.addView(menu, FrameLayout.LayoutParams(dp(48), dp(48), Gravity.TOP or Gravity.END))
        setContentView(content)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        window.decorView.systemUiVisibility = View.SYSTEM_UI_FLAG_FULLSCREEN or View.SYSTEM_UI_FLAG_HIDE_NAVIGATION or
            View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY or View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN or View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
        input?.requestFocus()
    }

    private fun controls(anchor: View) {
        input?.releaseInput()
        val menu = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setBackgroundColor(Color.WHITE); setPadding(dp(8), dp(8), dp(8), dp(8)) }
        val popupWindow = PopupWindow(menu, dp(240), -2, true).apply {
            setBackgroundDrawable(android.graphics.drawable.ColorDrawable(Color.WHITE))
            isOutsideTouchable = true; elevation = dp(6).toFloat()
            setOnDismissListener { session?.focus(true); input?.requestFocus(); popup = null }
        }
        popup = popupWindow
        menu.addView(command("画质") { settings() })
        menu.addView(command("触控：直接点按 / 触控板") { input?.let { it.directTouch = !it.directTouch }; popupWindow.dismiss() })
        menu.addView(command("游戏鼠标") { popupWindow.dismiss(); input?.post { input?.requestPointerCapture() } })
        menu.addView(command("键盘 / 剪贴板") { keyboard() })
        menu.addView(command("虚拟手柄") { togglePad(); popupWindow.dismiss() })
        menu.addView(command("断开连接") { popupWindow.dismiss(); disconnect() })
        popupWindow.showAsDropDown(anchor)
    }

    private fun togglePad() {
        virtualPad?.let { content.removeView(it); virtualPad = null; return }
        val pad = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; setBackgroundColor(0x55000000) }
        for ((name, mask) in listOf("↑" to 1, "←" to 4, "↓" to 2, "→" to 8, "A" to 0x1000, "B" to 0x2000, "X" to 0x4000, "Y" to 0x8000, "☰" to 0x10)) {
            val button = Button(this).apply {
                text = name
                setOnTouchListener { _, event ->
                    when (event.actionMasked) {
                        MotionEvent.ACTION_DOWN -> input?.virtualButton(mask, true)
                        MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> input?.virtualButton(mask, false)
                    }
                    true
                }
            }
            pad.addView(button, LinearLayout.LayoutParams(0, dp(56), 1f))
        }
        content.addView(pad, FrameLayout.LayoutParams(-1, dp(56), Gravity.BOTTOM))
        virtualPad = pad
    }

    private fun keyboard() {
        val layout = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        val field = EditText(this).apply { hint = "文本" }
        layout.addView(field)
        val row = LinearLayout(this)
        listOf("Esc" to 27, "Tab" to 9, "Win" to 91).forEach { (name, vk) ->
            row.addView(command(name) { session?.focus(true); input?.remoteKey(vk, true); input?.remoteKey(vk, false) }, LinearLayout.LayoutParams(0, dp(48), 1f))
        }
        layout.addView(row)
        val clipboard = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
        layout.addView(command("粘贴本机剪贴板") { field.setText(clipboard.primaryClip?.getItemAt(0)?.coerceToText(this) ?: "") })
        layout.addView(command("复制远端剪贴板") {
            clipboard.setPrimaryClip(ClipData.newPlainText("Elink", preferences.getString("remoteClipboard", "")))
        })
        AlertDialog.Builder(this).setTitle("键盘 / 剪贴板").setView(layout)
            .setPositiveButton("发送") { _, _ ->
                session?.focus(true)
                session?.sendRaw(JSONObject().put("type", "clipboard").put("text", field.text.toString().take(64000)))
                input?.remoteKey(162, true); input?.remoteKey(86, true)
                input?.remoteKey(86, false); input?.remoteKey(162, false)
            }.setNegativeButton("关闭", null).show()
    }

    private fun settings() {
        val layout = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(dp(20), dp(4), dp(20), dp(4)) }
        val dimensions = VideoSupport.dimensions.filter { VideoSupport.rates(it).isNotEmpty() }
        if (dimensions.isEmpty()) { error("当前设备没有可用的 H.264 硬件解码器"); return }
        fun spinner(label: String, entries: List<String>, selected: Int): Spinner {
            layout.addView(this.label(label))
            return Spinner(this).apply {
                adapter = ArrayAdapter(this@MainActivity, android.R.layout.simple_spinner_dropdown_item, entries)
                setSelection(selected.coerceAtLeast(0)); layout.addView(this)
            }
        }
        val resolution = spinner("分辨率", dimensions.map { "${it.first} × ${it.second}" }, dimensions.indexOf(config.width to config.height))
        var rates = VideoSupport.rates(dimensions[resolution.selectedItemPosition])
        val fps = spinner("帧率", rates.map { "$it FPS" }, rates.indexOf(config.fps))
        resolution.onItemSelectedListener = object : AdapterView.OnItemSelectedListener {
            override fun onNothingSelected(parent: AdapterView<*>?) {}
            override fun onItemSelected(parent: AdapterView<*>?, view: View?, position: Int, id: Long) {
                val previous = rates.getOrNull(fps.selectedItemPosition) ?: config.fps
                rates = VideoSupport.rates(dimensions[position])
                fps.adapter = ArrayAdapter(this@MainActivity, android.R.layout.simple_spinner_dropdown_item, rates.map { "$it FPS" })
                fps.setSelection(rates.indexOf(previous).coerceAtLeast(0))
            }
        }
        layout.addView(label("码率(Mbps)"))
        val rate = EditText(this).apply { inputType = android.text.InputType.TYPE_CLASS_NUMBER; setText(config.bitrate.toString()) }
        layout.addView(rate)
        val scroll = ScrollView(this).apply { addView(layout) }
        val dialog = AlertDialog.Builder(this).setTitle("画质").setView(scroll).setPositiveButton("应用", null).setNegativeButton("取消", null).create()
        dialog.setOnShowListener { dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener {
            val bitrate = rate.text.toString().toIntOrNull()
            if (bitrate == null || bitrate !in 1..80) { rate.error = "1–80 Mbps"; return@setOnClickListener }
            val (width, height) = dimensions[resolution.selectedItemPosition]
            val next = StreamConfig(width, height, rates[fps.selectedItemPosition], bitrate)
            scope.launch {
                try {
                    VideoSupport.validate(next)
                    session?.apply(next)
                    config = next
                    preferences.edit().putInt("width", width).putInt("height", height).putInt("fps", next.fps).putInt("bitrate", bitrate).apply()
                    dialog.dismiss()
                    if (session == null && page == "设置") home()
                } catch (failure: Exception) {
                    if (failure is CancellationException) throw failure
                    error(failure.message ?: "设置失败", failure)
                }
            }
        } }
        dialog.show()
    }

    private fun error(message: String, cause: Throwable? = null) {
        diagnostics.record("error", message, cause)
        if (isFinishing || isDestroyed) return
        input?.releaseInput()
        errorDialog?.dismiss()
        errorDialog = AlertDialog.Builder(this).setTitle("Elink").setMessage(message).setPositiveButton("关闭", null).create()
        errorDialog?.setOnDismissListener {
            if (popup?.isShowing != true && session?.connected == true && resumed) {
                session?.focus(true)
                input?.requestFocus()
            }
        }
        errorDialog?.show()
    }

    private fun disconnect() {
        if (stopping) return
        stopping = true
        connectJob?.cancel()
        input?.releaseInput()
        val stream = session
        scope.launch {
            stream?.disconnect()
            stream?.dispose()
            session = null; renderer = null; input = null; virtualPad = null
            stopping = false
            if (!isFinishing && !isDestroyed) home()
        }
    }

    override fun dispatchKeyEvent(event: KeyEvent): Boolean {
        if (session?.connected == true && popup?.isShowing != true && currentFocus is RemoteInputView && event.keyCode != KeyEvent.KEYCODE_BACK) {
            if (input?.handleKey(event, event.action == KeyEvent.ACTION_DOWN) == true) return true
        }
        return super.dispatchKeyEvent(event)
    }
    override fun onBackPressed() {
        if (session != null) {
            AlertDialog.Builder(this).setMessage("断开远程连接？").setPositiveButton("断开") { _, _ -> disconnect() }
                .setNegativeButton("取消", null).show()
        } else if (page != "设备") { page = "设备"; home() } else super.onBackPressed()
    }
    override fun onPause() { input?.releaseInput(); super.onPause() }
    override fun onResume() {
        super.onResume(); resumed = true
        if (session?.connected == true) session?.focus(true)
        else if (session == null && !connecting && ::devices.isInitialized) refresh()
    }
    override fun onStop() { resumed = false; if (session != null && !isChangingConfigurations) disconnect(); super.onStop() }
    override fun onDestroy() { discovery.close(); presenceJob?.cancel(); refreshJob?.cancel(); connectJob?.cancel(); input?.releaseInput(); session?.dispose(); scope.cancel(); super.onDestroy() }
}
