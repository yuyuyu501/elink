package io.github.yuyuyu501.elink

import android.app.Activity
import android.app.AlertDialog
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.graphics.Color
import android.os.Bundle
import android.view.*
import android.widget.*
import kotlinx.coroutines.*
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
    private val discovery by lazy { DeviceDiscovery(this) }
    private var presenceJob: Job? = null

    override fun onCreate(state: Bundle?) {
        super.onCreate(state)
        config = StreamConfig(preferences.getInt("width", 1920), preferences.getInt("height", 1080),
            preferences.getInt("fps", 60), preferences.getInt("bitrate", 20))
        home()
        discovery.start(scope)
        presenceJob = scope.launch {
            while (isActive) {
                delay(10000)
                if (session == null && !connecting && resumed && refreshJob?.isActive != true) refresh() else discovery.scan()
            }
        }
    }

    private fun dp(value: Int) = (value * resources.displayMetrics.density).toInt()
    private fun label(text: String, size: Float = 16f) = TextView(this).apply {
        this.text = text; textSize = size; setTextColor(Color.rgb(24, 37, 53)); setPadding(dp(12), dp(10), dp(12), dp(10))
    }
    private fun command(text: String, action: () -> Unit) = Button(this).apply { this.text = text; setOnClickListener { action() } }

    private fun home() {
        content = FrameLayout(this).apply { setBackgroundColor(Color.rgb(244, 246, 248)) }
        content.setOnApplyWindowInsetsListener { view, insets ->
            val bars = insets.getInsets(WindowInsets.Type.systemBars() or WindowInsets.Type.displayCutout())
            view.setPadding(bars.left, bars.top, bars.right, bars.bottom)
            insets
        }
        val scroll = ScrollView(this)
        val layout = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(dp(16), dp(16), dp(16), dp(16)) }
        scroll.addView(layout)
        layout.addView(label("Elink", 28f))
        layout.addView(label("0.5.4 · Android Preview", 13f))
        val row = LinearLayout(this)
        row.addView(command("添加设备") { addDevice() }, LinearLayout.LayoutParams(0, dp(52), 1f))
        row.addView(command("刷新") { refresh() }, LinearLayout.LayoutParams(0, dp(52), 1f))
        row.addView(command("设置") { settings() }, LinearLayout.LayoutParams(0, dp(52), 1f))
        layout.addView(row)
        status = label("").also { layout.addView(it) }
        devices = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        layout.addView(devices)
        content.addView(scroll)
        setContentView(content)
        window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        window.decorView.systemUiVisibility = View.SYSTEM_UI_FLAG_VISIBLE
        showSaved()
        if (resumed) refresh()
    }

    private fun saved() = preferences.getStringSet("devices", emptySet())!!.toMutableSet()
    private fun showSaved() {
        devices.removeAllViews()
        saved().sorted().forEach { address -> device(address, preferences.getString("name:$address", address) ?: address) }
    }
    private fun device(address: String, name: String, connectable: Boolean = true) {
        val button = command("$name\n$address") { connect(address) }
        button.isEnabled = connectable
        button.gravity = Gravity.START or Gravity.CENTER_VERTICAL
        button.isAllCaps = false
        button.setPadding(dp(18), dp(12), dp(18), dp(12))
        button.setOnLongClickListener {
            AlertDialog.Builder(this).setTitle(name).setMessage("删除此设备和已保存的设备身份？")
                .setPositiveButton("删除") { _, _ ->
                    val authority = runCatching { HostAddress.parse(address).authority }.getOrDefault(address)
                    preferences.edit().putStringSet("devices", saved().apply { remove(address) })
                        .remove("pin:$authority").remove("name:$address").apply()
                    showSaved()
                }.setNegativeButton("取消", null).show()
            true
        }
        devices.addView(button, LinearLayout.LayoutParams(-1, dp(84)).apply { bottomMargin = dp(8) })
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
                showSaved(); refresh(); dialog.dismiss()
            }.onFailure { field.error = it.message }
        } }
        dialog.show()
    }

    private fun refresh() {
        refreshJob?.cancel()
        refreshJob = scope.launch {
            status?.text = "正在查找设备…"
            val found = discovery.scan()
            fun canonical(address: String) = runCatching { HostAddress.parse(address).authority }.getOrDefault(address)
            val online = found.filter { it.connectable }.associateBy { canonical(it.authority) }
            val addresses = (saved().map { canonical(it) } + online.keys).toSortedSet()
            val checked = withContext(Dispatchers.IO) {
                addresses.map { address -> async {
                    address to if (address in online) null else
                        runCatching { Signaling(HostAddress.parse(address), preferences).info() }.getOrNull()
                } }.awaitAll().toMap()
            }
            ensureActive()
            devices.removeAllViews()
            val displayedIds = mutableSetOf<String>()
            for (address in addresses.sorted()) {
                val info = checked[address]
                val peer = online[address]
                val identity = peer?.id ?: info?.optString("host_id")?.takeIf { it.isNotEmpty() }
                if (identity != null && !displayedIds.add(identity)) continue
                val name = peer?.name ?: info?.optString("name") ?: preferences.getString("name:$address", address)!!
                if (info != null) preferences.edit().putString("name:$address", name).apply()
                device(address, name + if (peer?.busy == true || info?.optBoolean("busy") == true) " · 控制中"
                    else if (peer == null && info == null) " · 离线" else " · 在线")
            }
            for (peer in found.filter { !it.connectable }) {
                if (displayedIds.add(peer.id)) device(peer.address, "${peer.name} · 在线 · 仅控制端", false)
            }
            status?.text = "${devices.childCount} 台设备 · 自动刷新"
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
        input = RemoteInputView(this, stream).also { content.addView(it, FrameLayout.LayoutParams(-1, -1)) }
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
        val dialog = AlertDialog.Builder(this).setTitle("画质").setView(layout).setPositiveButton("应用", null).setNegativeButton("取消", null).create()
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
                } catch (failure: Exception) { error(failure.message ?: "设置失败") }
            }
        } }
        dialog.show()
    }

    private fun error(message: String, cause: Throwable? = null) {
        android.util.Log.e("Elink", message)
        runCatching {
            val log = java.io.File(filesDir, "elink.log")
            if (log.length() > 1_048_576) log.renameTo(java.io.File(filesDir, "elink.previous.log"))
            log.appendText("${java.time.Instant.now()} $message\n${cause?.stackTraceToString() ?: ""}\n")
        }
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
        } else super.onBackPressed()
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
