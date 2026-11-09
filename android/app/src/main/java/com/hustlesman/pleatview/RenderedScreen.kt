package com.hustlesman.pleatview

import android.app.Activity
import android.graphics.Color
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.widget.FrameLayout
import androidx.annotation.OptIn
import androidx.media3.common.MediaItem
import androidx.media3.common.MimeTypes
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DefaultHttpDataSource
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.hls.HlsMediaSource
import androidx.media3.ui.AspectRatioFrameLayout
import androidx.media3.ui.PlayerView

/**
 * The TV's dashboards as Home Assistant renders them (render.py in the integration): one
 * live H.264 stream, played full screen. Home Assistant decides what it shows; the TV only
 * decodes it, which every TV does in hardware.
 */
class RenderedScreen(private val activity: Activity, private val container: FrameLayout, private val onTrouble: (String?) -> Unit) {
    private val main = Handler(Looper.getMainLooper())
    private var view: PlayerView? = null
    private var player: ExoPlayer? = null
    private var url: String? = null
    private var paused = false
    private val retry = Runnable { url?.let(::start) }

    val isShown: Boolean get() = view != null

    /** Plays [url] full screen; the same stream again keeps playing. */
    fun show(url: String) {
        if (url == this.url && view != null) {
            view?.bringToFront()
            return
        }
        hide()
        this.url = url
        @OptIn(UnstableApi::class)
        val view = PlayerView(activity).apply {
            useController = false
            setShutterBackgroundColor(Color.BLACK)
            setBackgroundColor(Color.BLACK)
            resizeMode = AspectRatioFrameLayout.RESIZE_MODE_FIT
            isFocusable = false
        }
        container.addView(view, FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT))
        this.view = view
        if (!paused) start(url)
    }

    fun hide() {
        main.removeCallbacks(retry)
        release()
        view?.let { container.removeView(it) }
        view = null
        url = null
    }

    /** Plays from the live edge again, as a reload does for a page. */
    fun restart() {
        val url = url ?: return
        release()
        if (!paused) start(url)
    }

    fun onPause() {
        paused = true
        main.removeCallbacks(retry)
        release()
    }

    fun onResume() {
        paused = false
        url?.let { if (player == null) start(it) }
    }

    @OptIn(UnstableApi::class)
    private fun start(url: String) {
        release()
        val http = DefaultHttpDataSource.Factory()
            .setUserAgent("PleatView/${BuildConfig.VERSION_NAME}")
            .setConnectTimeoutMs(10_000)
            .setReadTimeoutMs(15_000)
        val player = ExoPlayer.Builder(activity)
            .setMediaSourceFactory(HlsMediaSource.Factory(http))
            .build()
        player.volume = 0f
        player.addListener(object : Player.Listener {
            override fun onPlayerError(error: PlaybackException) {
                // Home Assistant or its renderer restarting, or Wi-Fi gone for a moment.
                Log.w(TAG, "Rendered dashboard: ${error.errorCodeName}; trying again")
                onTrouble("The dashboard Home Assistant renders didn't play (${error.errorCodeName}); trying again")
                release()
                main.removeCallbacks(retry)
                main.postDelayed(retry, RETRY_MS)
            }

            override fun onPlaybackStateChanged(state: Int) {
                if (state == Player.STATE_READY) onTrouble(null)
            }
        })
        player.setMediaItem(MediaItem.Builder().setUri(url).setMimeType(MimeTypes.APPLICATION_M3U8).build())
        player.playWhenReady = true
        player.prepare()
        view?.player = player
        this.player = player
    }

    private fun release() {
        view?.player = null
        player?.release()
        player = null
    }

    companion object {
        private const val TAG = "PleatView"
        private const val RETRY_MS = 2_000L
    }
}
