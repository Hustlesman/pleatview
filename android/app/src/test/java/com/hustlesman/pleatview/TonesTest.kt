package com.hustlesman.pleatview

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.abs

class TonesTest {
    private fun seconds(name: String) = Tones.samples(name)!!.size.toDouble() / Tones.RATE

    private fun peak(name: String) = Tones.samples(name)!!.maxOf { abs(it.toInt()) }.toDouble() / Short.MAX_VALUE

    @Test
    fun `warnings are short and gentle, alerts longer and louder`() {
        assertEquals(setOf("chime", "ding", "beep", "alarm", "siren", "bell"), Tones.NAMES)
        for (warning in listOf("chime", "ding", "beep")) {
            assertTrue("$warning: ${seconds(warning)} s", seconds(warning) < 1.0)
            assertTrue("$warning: peak ${peak(warning)}", peak(warning) <= 0.45)
        }
        for (alert in listOf("alarm", "siren", "bell")) {
            assertTrue("$alert: ${seconds(alert)} s", seconds(alert) in 1.5..3.0)
            assertTrue("$alert: peak ${peak(alert)}", peak(alert) in 0.6..0.8)
        }
    }

    @Test
    fun `none and unknown names make no sound`() {
        assertNull(Tones.samples("none"))
        assertNull(Tones.samples("klaxon"))
    }

    @Test
    fun `notes start and end quietly, so they don't click`() {
        for (name in Tones.NAMES) {
            val samples = Tones.samples(name)!!
            assertTrue("$name starts at ${samples[0]}", abs(samples[0].toInt()) < 200)
            assertTrue("$name ends at ${samples.last()}", abs(samples.last().toInt()) < 200)
        }
    }
}
