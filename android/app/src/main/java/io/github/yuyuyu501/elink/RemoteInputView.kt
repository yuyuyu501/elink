package io.github.yuyuyu501.elink

import android.content.Context
import android.view.*
import org.json.JSONArray
import org.json.JSONObject
import kotlin.math.abs
import kotlin.math.roundToInt

class RemoteInputView(context: Context, private val session: StreamSession) : View(context) {
    private var downX = 0f
    private var downY = 0f
    private var pressed = false
    private var startX = 0f
    private var startY = 0f
    private var moved = false
    private var mouseButtons = 0
    var directTouch = true
    init { isFocusableInTouchMode = true }

    private val padRefresh = object : Runnable {
        override fun run() { sendPad(); postDelayed(this, 250) }
    }
    override fun onAttachedToWindow() { super.onAttachedToWindow(); post(padRefresh) }
    override fun onDetachedFromWindow() { removeCallbacks(padRefresh); releaseInput(); super.onDetachedFromWindow() }

    private fun position(event: MotionEvent): Boolean {
        val point = videoPoint(event.x, event.y, width, height, session.videoWidth, session.videoHeight) ?: return false
        session.send(JSONObject().put("type", "move").put("absolute", true).put("x", point.first).put("y", point.second))
        return true
    }

    private fun button(name: String, down: Boolean) = session.send(JSONObject().put("type", "button").put("button", name).put("down", down))

    override fun onTouchEvent(event: MotionEvent): Boolean {
        if (event.isFromSource(InputDevice.SOURCE_MOUSE)) return mouse(event)
        when (event.actionMasked) {
            MotionEvent.ACTION_DOWN -> {
                requestFocus()
                session.focus(true)
                downX = event.x; downY = event.y
                startX = event.x; startY = event.y; moved = false
                if (directTouch && position(event)) { button("left", true); pressed = true }
            }
            MotionEvent.ACTION_MOVE -> {
                moved = moved || abs(event.x - startX) + abs(event.y - startY) > ViewConfiguration.get(context).scaledTouchSlop
                if (directTouch) position(event) else {
                    session.send(JSONObject().put("type", "move").put("absolute", false)
                        .put("x", ((event.x - downX) * session.videoWidth / width).roundToInt().coerceIn(-32767, 32767))
                        .put("y", ((event.y - downY) * session.videoHeight / height).roundToInt().coerceIn(-32767, 32767)), false)
                    downX = event.x; downY = event.y
                }
            }
            MotionEvent.ACTION_UP -> {
                if (pressed) { position(event); button("left", false); pressed = false }
                else if (!directTouch && !moved && abs(event.x - startX) + abs(event.y - startY) < ViewConfiguration.get(context).scaledTouchSlop) {
                    button("left", true); button("left", false)
                }
                performClick()
            }
            MotionEvent.ACTION_CANCEL -> { if (pressed) button("left", false); pressed = false }
        }
        return true
    }

    override fun performClick(): Boolean { super.performClick(); return true }

    private fun mouse(event: MotionEvent): Boolean {
        session.focus(true)
        if (!position(event)) {
            if (mouseButtons != 0) {
                for ((mask, name) in listOf(MotionEvent.BUTTON_PRIMARY to "left", MotionEvent.BUTTON_SECONDARY to "right", MotionEvent.BUTTON_TERTIARY to "middle")) {
                    if (mouseButtons and mask != 0) button(name, false)
                }
                mouseButtons = 0
            }
            return false
        }
        updateMouseButtons(event)
        when (event.actionMasked) {
            MotionEvent.ACTION_SCROLL -> session.send(JSONObject().put("type", "wheel")
                .put("delta", (event.getAxisValue(MotionEvent.AXIS_VSCROLL) * 120).roundToInt().coerceIn(-12000, 12000)))
        }
        return true
    }

    override fun onGenericMotionEvent(event: MotionEvent): Boolean {
        if (event.isFromSource(InputDevice.SOURCE_JOYSTICK)) {
            val axis = { code: Int -> event.getAxisValue(code) }
            val hatX = axis(MotionEvent.AXIS_HAT_X)
            val hatY = axis(MotionEvent.AXIS_HAT_Y)
            padHat = (if (hatX < -.5) 4 else if (hatX > .5) 8 else 0) or
                (if (hatY < -.5) 1 else if (hatY > .5) 2 else 0)
            padAxes = intArrayOf((axis(MotionEvent.AXIS_LTRIGGER).coerceIn(0f, 1f) * 255).roundToInt(),
                (axis(MotionEvent.AXIS_RTRIGGER).coerceIn(0f, 1f) * 255).roundToInt(),
                (axis(MotionEvent.AXIS_X) * 32767).roundToInt(), (-axis(MotionEvent.AXIS_Y) * 32767).roundToInt(),
                (axis(MotionEvent.AXIS_Z) * 32767).roundToInt(), (-axis(MotionEvent.AXIS_RZ) * 32767).roundToInt())
            sendPad()
            return true
        }
        return if (event.isFromSource(InputDevice.SOURCE_MOUSE)) mouse(event) else super.onGenericMotionEvent(event)
    }

    override fun onCapturedPointerEvent(event: MotionEvent): Boolean {
        session.focus(true)
        session.send(JSONObject().put("type", "move").put("absolute", false)
            .put("x", event.x.roundToInt().coerceIn(-32767, 32767)).put("y", event.y.roundToInt().coerceIn(-32767, 32767)), false)
        updateMouseButtons(event)
        if (event.actionMasked == MotionEvent.ACTION_SCROLL) session.send(JSONObject().put("type", "wheel")
            .put("delta", (event.getAxisValue(MotionEvent.AXIS_VSCROLL) * 120).roundToInt().coerceIn(-12000, 12000)))
        return true
    }

    private fun updateMouseButtons(event: MotionEvent) {
        val next = if (event.actionMasked == MotionEvent.ACTION_CANCEL) 0 else event.buttonState
        for ((mask, name) in listOf(MotionEvent.BUTTON_PRIMARY to "left", MotionEvent.BUTTON_SECONDARY to "right", MotionEvent.BUTTON_TERTIARY to "middle")) {
            if ((next xor mouseButtons) and mask != 0) button(name, next and mask != 0)
        }
        mouseButtons = next
    }

    private var padButtons = 0
    private var padHat = 0
    private var padAxes = IntArray(6)
    private val gameKeys = mapOf(KeyEvent.KEYCODE_BUTTON_A to 0x1000, KeyEvent.KEYCODE_BUTTON_B to 0x2000,
        KeyEvent.KEYCODE_BUTTON_X to 0x4000, KeyEvent.KEYCODE_BUTTON_Y to 0x8000,
        KeyEvent.KEYCODE_BUTTON_L1 to 0x100, KeyEvent.KEYCODE_BUTTON_R1 to 0x200,
        KeyEvent.KEYCODE_BUTTON_START to 0x10, KeyEvent.KEYCODE_BUTTON_SELECT to 0x20,
        KeyEvent.KEYCODE_BUTTON_THUMBL to 0x40, KeyEvent.KEYCODE_BUTTON_THUMBR to 0x80,
        KeyEvent.KEYCODE_DPAD_UP to 1, KeyEvent.KEYCODE_DPAD_DOWN to 2,
        KeyEvent.KEYCODE_DPAD_LEFT to 4, KeyEvent.KEYCODE_DPAD_RIGHT to 8)

    private fun sendPad() = session.send(JSONObject().put("type", "pad").put("index", 0)
        .put("values", JSONArray(listOf(padButtons or padHat) + padAxes.toList())), false)

    fun virtualButton(mask: Int, down: Boolean) {
        padButtons = if (down) padButtons or mask else padButtons and mask.inv()
        sendPad()
    }

    fun remoteKey(vk: Int, down: Boolean, extended: Boolean = false) = session.send(JSONObject().put("type", "key")
        .put("vk", vk).put("scan", 0).put("extended", extended).put("down", down))

    fun handleKey(event: KeyEvent, down: Boolean): Boolean {
        if (event.isFromSource(InputDevice.SOURCE_GAMEPAD) || event.isFromSource(InputDevice.SOURCE_JOYSTICK)) {
            val mask = gameKeys[event.keyCode] ?: return false
            virtualButton(mask, down)
            return true
        }
        val code = event.keyCode
        val vk = when (code) {
            in KeyEvent.KEYCODE_A..KeyEvent.KEYCODE_Z -> 65 + code - KeyEvent.KEYCODE_A
            in KeyEvent.KEYCODE_0..KeyEvent.KEYCODE_9 -> 48 + code - KeyEvent.KEYCODE_0
            in KeyEvent.KEYCODE_F1..KeyEvent.KEYCODE_F12 -> 112 + code - KeyEvent.KEYCODE_F1
            in KeyEvent.KEYCODE_NUMPAD_0..KeyEvent.KEYCODE_NUMPAD_9 -> 96 + code - KeyEvent.KEYCODE_NUMPAD_0
            KeyEvent.KEYCODE_ENTER, KeyEvent.KEYCODE_NUMPAD_ENTER -> 13
            KeyEvent.KEYCODE_SPACE -> 32; KeyEvent.KEYCODE_DEL -> 8; KeyEvent.KEYCODE_FORWARD_DEL -> 46
            KeyEvent.KEYCODE_TAB -> 9; KeyEvent.KEYCODE_ESCAPE -> 27
            KeyEvent.KEYCODE_CTRL_LEFT -> 162; KeyEvent.KEYCODE_CTRL_RIGHT -> 163
            KeyEvent.KEYCODE_SHIFT_LEFT -> 160; KeyEvent.KEYCODE_SHIFT_RIGHT -> 161
            KeyEvent.KEYCODE_ALT_LEFT -> 164; KeyEvent.KEYCODE_ALT_RIGHT -> 165
            KeyEvent.KEYCODE_META_LEFT -> 91; KeyEvent.KEYCODE_META_RIGHT -> 92
            KeyEvent.KEYCODE_DPAD_LEFT -> 37; KeyEvent.KEYCODE_DPAD_UP -> 38
            KeyEvent.KEYCODE_DPAD_RIGHT -> 39; KeyEvent.KEYCODE_DPAD_DOWN -> 40
            KeyEvent.KEYCODE_MOVE_HOME -> 36; KeyEvent.KEYCODE_MOVE_END -> 35
            KeyEvent.KEYCODE_PAGE_UP -> 33; KeyEvent.KEYCODE_PAGE_DOWN -> 34
            KeyEvent.KEYCODE_INSERT -> 45; KeyEvent.KEYCODE_CAPS_LOCK -> 20; KeyEvent.KEYCODE_NUM_LOCK -> 144
            KeyEvent.KEYCODE_SEMICOLON -> 186; KeyEvent.KEYCODE_EQUALS -> 187
            KeyEvent.KEYCODE_COMMA -> 188; KeyEvent.KEYCODE_MINUS -> 189; KeyEvent.KEYCODE_PERIOD -> 190
            KeyEvent.KEYCODE_SLASH -> 191; KeyEvent.KEYCODE_GRAVE -> 192; KeyEvent.KEYCODE_LEFT_BRACKET -> 219
            KeyEvent.KEYCODE_BACKSLASH -> 220; KeyEvent.KEYCODE_RIGHT_BRACKET -> 221; KeyEvent.KEYCODE_APOSTROPHE -> 222
            else -> return false
        }
        if (!down || event.repeatCount == 0) remoteKey(vk, down, code in KeyEvent.KEYCODE_DPAD_UP..KeyEvent.KEYCODE_DPAD_RIGHT || code == KeyEvent.KEYCODE_NUMPAD_ENTER)
        return true
    }

    fun releaseInput() {
        if (hasPointerCapture()) releasePointerCapture()
        pressed = false
        mouseButtons = 0
        padButtons = 0
        padHat = 0
        padAxes = IntArray(6)
        sendPad()
        session.focus(false)
    }
}
