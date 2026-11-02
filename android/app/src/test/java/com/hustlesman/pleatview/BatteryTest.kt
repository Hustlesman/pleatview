package com.hustlesman.pleatview

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class BatteryTest {
    @Test
    fun `a tablet reports its level and whether it's plugged in`() {
        assertEquals(BatteryInfo(87, charging = true), BatteryInfo.of(true, 87, 100, plugged = 1, tv = false))
        assertEquals(BatteryInfo(40, charging = false), BatteryInfo.of(true, 20, 50, plugged = 0, tv = false))
    }

    @Test
    fun `no battery, a TV or a nonsense reading reports none`() {
        // Chromecast with Google TV and Fire TV Stick: present false, level 50 or 0.
        assertNull(BatteryInfo.of(false, 50, 100, plugged = 1, tv = false))
        assertNull(BatteryInfo.of(true, 100, 100, plugged = 1, tv = true))
        assertNull(BatteryInfo.of(true, -1, 100, plugged = 0, tv = false))
        assertNull(BatteryInfo.of(true, 50, 0, plugged = 0, tv = false))
    }
}
