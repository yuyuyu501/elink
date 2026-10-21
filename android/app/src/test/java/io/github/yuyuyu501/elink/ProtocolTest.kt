package io.github.yuyuyu501.elink

import org.junit.Assert.*
import org.junit.Test

class ProtocolTest {
    @Test fun addressValidation() {
        assertEquals(49200, HostAddress.parse("100.98.115.91").port)
        assertEquals("[::1]:49201", HostAddress.parse("[::1]:49201").authority)
        for (bad in listOf("https://example.com", "a@b", "host/path", "host:22", "host#x")) {
            assertThrows(Exception::class.java) { HostAddress.parse(bad) }
        }
    }
    @Test fun letterboxingDoesNotClickDesktop() {
        assertNull(videoPoint(10f, 10f, 1000, 1000, 1920, 1080))
        val center = videoPoint(500f, 500f, 1000, 1000, 1920, 1080)!!
        assertEquals(32768, center.first)
        assertEquals(32768, center.second)
    }
}
