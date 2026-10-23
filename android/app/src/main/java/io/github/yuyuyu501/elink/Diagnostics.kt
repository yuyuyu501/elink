package io.github.yuyuyu501.elink

import android.content.Context
import android.util.Log
import java.io.File
import java.time.Instant

/** One bounded, exportable log shared by UI, discovery and session errors. */
internal class Diagnostics(context: Context) {
    private val directory = context.filesDir

    @Synchronized fun record(stage: String, message: String, cause: Throwable? = null) {
        Log.i("Elink", "$stage: $message", cause)
        runCatching {
            val file = File(directory, "elink.log")
            if (file.length() > 1_048_576) {
                val previous = File(directory, "elink.previous.log")
                if (previous.exists()) previous.delete()
                file.renameTo(previous)
            }
            file.appendText("${Instant.now()} [$stage] $message\n${cause?.stackTraceToString() ?: ""}\n")
        }
    }

    @Synchronized fun report(state: String): String = buildString {
        appendLine("Elink ${BuildConfig.VERSION_NAME} Android Preview")
        appendLine(Instant.now())
        appendLine(state)
        for (name in listOf("elink.previous.log", "elink.log")) {
            appendLine("\n--- $name ---")
            appendLine(runCatching { File(directory, name).takeIf { it.exists() }?.readText() ?: "" }.getOrDefault(""))
        }
    }
}
