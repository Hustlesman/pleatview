package com.hustlesman.pleatview

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioFocusRequest
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioTrack
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.util.Log
import android.view.KeyEvent

/**
 * What a notification's level does besides its card, whatever app is in front:
 *
 * - a notice: nothing;
 * - a warning: a short tone, mixed over whatever plays, which carries on (no audio focus);
 * - an alert: the app in front (a film in Plex) is paused, the way a call pauses it on a
 *   phone, through audio focus and the pause media key, and a stronger tone plays. It
 *   doesn't resume by itself. An app that ignores both is muted for the alert's duration,
 *   where muting the TV's music can't silence the tone too (not on fixed-volume HDMI).
 *
 * Main thread only.
 */
class Alerting(private val app: Context) {
    private val main = Handler(Looper.getMainLooper())
    private val audio = app.getSystemService(Context.AUDIO_SERVICE) as AudioManager
    private val prefs = app.getSharedPreferences("alerting", Context.MODE_PRIVATE)
    private var track: AudioTrack? = null
    private var focus: AudioFocusRequest? = null
    private val unmute = Runnable { unmuteMusic() }

    init {
        // Muted for an alert and then stopped before it ended: the music comes back now.
        if (prefs.getBoolean(MUTED, false)) unmuteMusic()
    }

    fun on(notify: Command.Notify, pauseOwn: () -> Unit) {
        when (notify.level) {
            Command.Level.NOTICE -> Unit
            Command.Level.WARNING -> notify.tone?.let { play(it, alarm = false) }
            Command.Level.ALERT -> alert(notify, pauseOwn)
        }
    }

    private fun alert(notify: Command.Notify, pauseOwn: () -> Unit) {
        pauseOwn()
        takeFocus()
        // The app in front's media session (Plex, Netflix…) gets a pause key too.
        val now = SystemClock.uptimeMillis()
        audio.dispatchMediaKeyEvent(KeyEvent(now, now, KeyEvent.ACTION_DOWN, KeyEvent.KEYCODE_MEDIA_PAUSE, 0))
        audio.dispatchMediaKeyEvent(KeyEvent(now, now, KeyEvent.ACTION_UP, KeyEvent.KEYCODE_MEDIA_PAUSE, 0))
        main.postDelayed({
            val mute = othersPlaying() && !audio.isVolumeFixed
            if (mute) muteMusic(notify.duration)
            val tone = notify.tone
            if (tone != null) play(tone, alarm = mute) else releaseFocus()
        }, SETTLE_MS)
    }

    /** Plays [name]; on the alarm stream when the music stream is muted, otherwise as media. */
    private fun play(name: String, alarm: Boolean) {
        val samples = Tones.samples(name) ?: return
        stop()
        val attributes = AudioAttributes.Builder()
            .setUsage(if (alarm) AudioAttributes.USAGE_ALARM else AudioAttributes.USAGE_MEDIA)
            .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
            .build()
        val format = AudioFormat.Builder()
            .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
            .setSampleRate(Tones.RATE)
            .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
            .build()
        val track = try {
            AudioTrack(attributes, format, samples.size * 2, AudioTrack.MODE_STATIC, AudioManager.AUDIO_SESSION_ID_GENERATE)
        } catch (e: Exception) {
            Log.w(TAG, "No tone: ${e.message}")
            return
        }
        // A TV without a usable output for this kind of sound gives an uninitialized track.
        try {
            check(track.state == AudioTrack.STATE_INITIALIZED) { "no output" }
            track.write(samples, 0, samples.size)
            track.play()
        } catch (e: Exception) {
            Log.w(TAG, "No tone: ${e.message}")
            track.release()
            releaseFocus()
            return
        }
        this.track = track
        main.postDelayed({
            if (this.track === track) stop()
            releaseFocus()
        }, samples.size * 1000L / Tones.RATE + 200)
    }

    private fun stop() {
        track?.let {
            runCatching { it.stop() }
            it.release()
        }
        track = null
    }

    /** Audio focus for good: apps pause and stay paused, rather than ducking or resuming. */
    @Suppress("DEPRECATION")
    private fun takeFocus() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val request = AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN)
                .setAudioAttributes(
                    AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_MEDIA)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build(),
                )
                .setOnAudioFocusChangeListener { }
                .build()
            audio.requestAudioFocus(request)
            focus = request
        } else {
            audio.requestAudioFocus(null, AudioManager.STREAM_MUSIC, AudioManager.AUDIOFOCUS_GAIN)
        }
    }

    @Suppress("DEPRECATION")
    private fun releaseFocus() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            focus?.let { audio.abandonAudioFocusRequest(it) }
            focus = null
        } else {
            audio.abandonAudioFocus(null)
        }
    }

    /** Whether something still plays music: an app that ignored the pause (paused players don't count). */
    private fun othersPlaying(): Boolean = audio.isMusicActive

    private fun muteMusic(seconds: Int) {
        Log.i(TAG, "Alert: something still plays; muting the music for $seconds s")
        audio.adjustStreamVolume(AudioManager.STREAM_MUSIC, AudioManager.ADJUST_MUTE, 0)
        prefs.edit().putBoolean(MUTED, true).apply()
        main.removeCallbacks(unmute)
        main.postDelayed(unmute, seconds * 1000L)
    }

    private fun unmuteMusic() {
        audio.adjustStreamVolume(AudioManager.STREAM_MUSIC, AudioManager.ADJUST_UNMUTE, 0)
        prefs.edit().putBoolean(MUTED, false).apply()
    }

    private companion object {
        const val TAG = "PleatView"
        const val MUTED = "music_muted"

        /** How long an app gets to pause before the tone, and before checking it did. */
        const val SETTLE_MS = 700L
    }
}
