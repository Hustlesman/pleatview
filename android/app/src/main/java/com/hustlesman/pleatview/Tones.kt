package com.hustlesman.pleatview

import kotlin.math.PI
import kotlin.math.exp
import kotlin.math.min
import kotlin.math.sin

/**
 * The tones of warnings and alerts, made here rather than shipped as sound files: each is
 * a few notes. Warnings get short, gentle ones (chime, ding, beep); alerts louder, longer
 * ones (alarm, siren, bell). The Linux app makes the same tones (linux/src/ui/tones.js).
 */
object Tones {
    const val RATE = 44_100

    /** One note: a pitch (gliding from [from] to [to] Hz), its length, its loudness and how it fades. */
    private data class Note(val from: Double, val to: Double, val ms: Int, val level: Double, val bell: Boolean, val gapMs: Int = 0)

    private fun bell(hz: Double, ms: Int, level: Double, gapMs: Int = 0) = Note(hz, hz, ms, level, bell = true, gapMs = gapMs)
    private fun flat(hz: Double, ms: Int, level: Double, gapMs: Int = 0) = Note(hz, hz, ms, level, bell = false, gapMs = gapMs)
    private fun glide(from: Double, to: Double, ms: Int, level: Double) = Note(from, to, ms, level, bell = false)

    private val TONES: Map<String, List<Note>> = mapOf(
        // Warnings: under a second, about half as loud as an alert.
        "chime" to listOf(bell(1318.5, 220, 0.4, gapMs = 30), bell(1046.5, 520, 0.4)),
        "ding" to listOf(bell(1568.0, 800, 0.4)),
        "beep" to listOf(flat(880.0, 110, 0.3, gapMs = 80), flat(880.0, 110, 0.3)),
        // Alerts: two to three seconds, loud.
        "alarm" to List(3) { listOf(flat(880.0, 180, 0.7, gapMs = 30), flat(1174.7, 180, 0.7, gapMs = 120)) }.flatten(),
        "siren" to List(3) { listOf(glide(650.0, 1300.0, 420, 0.7), glide(1300.0, 650.0, 420, 0.7)) }.flatten(),
        "bell" to List(3) { bell(1046.5, 650, 0.85, gapMs = 60) },
    )

    val NAMES: Set<String> = TONES.keys

    /** The tone's samples: 16-bit mono at [RATE] Hz, or null for "none" and names it doesn't know. */
    fun samples(name: String): ShortArray? {
        val notes = TONES[name] ?: return null
        val out = ShortArray(notes.sumOf { (it.ms + it.gapMs) * RATE / 1000 })
        var at = 0
        for (note in notes) {
            val count = note.ms * RATE / 1000
            val edge = min(count / 2, RATE * 6 / 1000) // 6 ms in and out, so notes don't click
            var phase = 0.0
            for (i in 0 until count) {
                val t = i.toDouble() / RATE
                val hz = note.from + (note.to - note.from) * i / count
                phase += 2 * PI * hz / RATE
                val release = min(1.0, (count - i).toDouble() / edge)
                val fade = when {
                    note.bell -> exp(-t / (note.ms / 1000.0 * 0.3)) * min(1.0, i / (RATE * 0.003)) * release
                    i < edge -> i.toDouble() / edge
                    else -> release
                }
                // A bell has overtones; the rest are plain.
                val wave = if (note.bell) (sin(phase) + 0.35 * sin(2 * phase) + 0.15 * sin(3 * phase)) / 1.5 else sin(phase)
                out[at + i] = (wave * fade * note.level * Short.MAX_VALUE).toInt().toShort()
            }
            at += count + note.gapMs * RATE / 1000
        }
        return out
    }
}
