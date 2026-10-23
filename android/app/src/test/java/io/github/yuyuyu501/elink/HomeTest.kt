package io.github.yuyuyu501.elink

import android.content.Intent
import android.view.View
import android.view.ViewGroup
import android.widget.TextView
import android.util.Base64
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import java.io.IOException

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class HomeTest {
    private fun texts(view: View): List<TextView> = buildList {
        if (view is TextView) add(view)
        if (view is ViewGroup) for (index in 0 until view.childCount) addAll(texts(view.getChildAt(index)))
    }

    @Test fun pagesExposeSettingsAboutAndNonConnectableLocalPhone() {
        val controller = Robolectric.buildActivity(MainActivity::class.java).create()
        val activity = controller.get()
        fun labels() = texts(activity.window.decorView)
        try {
            assertFalse(labels().any { it is android.widget.Button && it.text == "连接" })
            assertTrue(labels().any { it.text == "Android · 在线 · 仅控制端" })
            assertFalse(labels().any { it.text == "暂未发现其它在线设备" })
            labels().single { it.text == "设置" }.performClick()
            assertTrue(labels().any { it.text == "直接触控" })
            assertTrue(labels().any { it.text == "自动发现同一局域网设备" })
            labels().single { it.text == "关于" }.performClick()
            assertTrue(labels().any { it.text.toString().contains(BuildConfig.VERSION_NAME) })
            labels().single { it.text == "导出诊断日志" }.performClick()
            val export = shadowOf(activity).nextStartedActivityForResult
            assertEquals(Intent.ACTION_CREATE_DOCUMENT, export.intent.action)
            assertEquals("text/plain", export.intent.type)
            activity.onBackPressed()
            assertTrue(labels().any { it.text == "设备列表" })
        } finally {
            controller.destroy()
        }
    }

    @Test fun actualFailurePersistsStackAndAppearsInExportedReport() {
        val controller = Robolectric.buildActivity(MainActivity::class.java).create()
        val activity = controller.get()
        try {
            val diagnostics = Diagnostics(activity)
            val failure = runCatching { throw IOException("presence socket unavailable") }.exceptionOrNull()!!
            diagnostics.record("discovery.send", "send failed", failure)
            val file = java.io.File(activity.filesDir, "elink.log")
            assertTrue(file.readText().contains("java.io.IOException: presence socket unavailable"))
            val report = diagnostics.report("listener=false")
            assertTrue(report.contains("[discovery.send] send failed"))
            assertTrue(report.contains("HomeTest.actualFailurePersistsStackAndAppearsInExportedReport"))
            assertTrue(report.contains("listener=false"))
            assertTrue(report.contains(BuildConfig.VERSION_NAME))
        } finally {
            controller.destroy()
        }
    }

    private fun verifyCompactLayout(width: Int, height: Int) {
        val controller = Robolectric.buildActivity(MainActivity::class.java).create()
        val activity = controller.get()
        val root = activity.findViewById<ViewGroup>(android.R.id.content)
        fun measure() {
            root.measure(View.MeasureSpec.makeMeasureSpec(width, View.MeasureSpec.EXACTLY),
                View.MeasureSpec.makeMeasureSpec(height, View.MeasureSpec.EXACTLY))
            root.layout(0, 0, width, height)
        }
        try {
            for (page in listOf("设备", "设置", "关于")) {
                texts(root).single { it.text == page && it is android.widget.Button }.performClick()
                measure()
                for (tab in texts(root).filter { it.text in listOf("设备", "设置", "关于") && it is android.widget.Button }) {
                    val bounds = android.graphics.Rect()
                    tab.getDrawingRect(bounds)
                    root.offsetDescendantRectToMyCoords(tab, bounds)
                    assertTrue("$page ${tab.text} $bounds", bounds.left >= 0 && bounds.right <= width &&
                        bounds.top >= 0 && bounds.bottom <= height && bounds.height() >= 48)
                    assertEquals("$page ${tab.text}", 1, tab.lineCount)
                }
            }
            val discovery = DeviceDiscovery(activity)
            val own = JSONObject(String(Base64.decode(discovery.introduction(), Base64.NO_WRAP), Charsets.UTF_8))
            assertEquals("android", own.getJSONObject("capabilities").getString("platform"))
            assertEquals(listOf("controller"), (0 until own.getJSONObject("capabilities").getJSONArray("roles").length())
                .map { own.getJSONObject("capabilities").getJSONArray("roles").getString(it) })
            assertEquals(0, own.getInt("port"))
            assertEquals(discovery.localDevice().id, own.getString("host_id"))
        } finally {
            controller.destroy()
        }
    }

    @Test @Config(qualifiers = "w320dp-h640dp-mdpi")
    fun smallPhoneKeepsAllNavigationReachable() = verifyCompactLayout(320, 640)

    @Test @Config(qualifiers = "w640dp-h360dp-land-mdpi")
    fun landscapeKeepsAllNavigationReachable() = verifyCompactLayout(640, 360)
}
