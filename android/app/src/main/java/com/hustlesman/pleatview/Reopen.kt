package com.hustlesman.pleatview

/** When PleatView comes back by itself: after someone left it for another app, or after a crash. */
object Reopen {
    /** Crashes further back than this don't count towards [afterCrash]'s limit. */
    const val CRASH_WINDOW_MS = 10 * 60_000L

    /** Opened again at most this many times in [CRASH_WINDOW_MS]: the next crash leaves it closed. */
    const val MOST_REOPENS = 2

    /**
     * Due once PleatView has been off screen for [minutes] and nothing has played sound for
     * as long: someone switched to another app and isn't watching anything there.
     *
     * @param hiddenSince when PleatView left the screen, 0 while it's on screen
     * @param lastSound when another app last played sound, 0 if never
     */
    fun due(now: Long, hiddenSince: Long, lastSound: Long, minutes: Int, allowed: Boolean): Boolean {
        if (!allowed || minutes <= 0 || hiddenSince == 0L) return false
        val wait = minutes * 60_000L
        return now - hiddenSince >= wait && now - lastSound >= wait
    }

    /**
     * After a crash: opened again if it was on screen (never over another app someone is using),
     * unless it was opened again [MOST_REOPENS] times already in the last ten minutes. A screen
     * that keeps crashing stays closed rather than flashing on and off.
     *
     * @param earlier the times of earlier crashes, from [crashTimes]
     */
    fun afterCrash(now: Long, onScreen: Boolean, earlier: List<Long>): Boolean =
        onScreen && earlier.count { now - it in 0 until CRASH_WINDOW_MS } < MOST_REOPENS

    /** Earlier crashes' times as kept ("t1,t2"), the last ten minutes' only. */
    fun crashTimes(saved: String?, now: Long): List<Long> =
        saved.orEmpty().split(',').mapNotNull { it.trim().toLongOrNull() }.filter { now - it in 0 until CRASH_WINDOW_MS }
}
