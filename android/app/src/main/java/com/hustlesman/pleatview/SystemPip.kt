package com.hustlesman.pleatview

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Color
import android.graphics.PixelFormat
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.util.Log
import android.view.Gravity
import android.view.TextureView
import android.view.View
import android.view.WindowManager
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import androidx.annotation.OptIn
import androidx.media3.common.MediaItem
import androidx.media3.common.MimeTypes
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.common.VideoSize
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DefaultDataSource
import androidx.media3.datasource.DefaultHttpDataSource
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import androidx.media3.ui.AspectRatioFrameLayout

/**
 * A camera, video or page in a corner over other apps while PleatView is in the background:
 * the door camera during a film. A system overlay window, like notifications over other apps
 * ("Display over other apps"), that never takes the remote's keys, touches or the sound, so
 * the other app carries on. A camera's video falls back to its picture stream (MJPEG) when
 * the TV can't decode it beside the other app's video.
 *
 * Main thread only.
 */
class SystemPip(private val app: Context, private val hub: Hub) {
    private val main = Handler(Looper.getMainLooper())
    private val windows get() = app.getSystemService(Context.WINDOW_SERVICE) as WindowManager
    private var frame: FrameLayout? = null
    private var player: ExoPlayer? = null
    private var web: WebView? = null
    private val closeRunnable = Runnable { close() }

    val isShown: Boolean get() = frame != null

    /** A picture-in-picture video, camera or page, which can go over another app. */
    fun takes(command: Command): Boolean = when (command) {
        is Command.PlayMedia -> !command.announce && command.layout.mode == Command.Mode.PIP
        is Command.ShowWeb -> command.layout.mode == Command.Mode.PIP
        else -> false
    }

    /** Shows [command] over whatever is on screen; false without the permission to. */
    fun show(command: Command): Boolean {
        if (!takes(command) || !Settings.canDrawOverlays(app)) return false
        val layout = (command as? Command.PlayMedia)?.layout ?: (command as Command.ShowWeb).layout
        val frame = frame ?: open(layout) ?: return false
        place(frame, layout)
        clear()
        when (command) {
            is Command.PlayMedia -> play(frame, command)
            is Command.ShowWeb -> showWeb(frame, command.url, command.kind)
            else -> Unit
        }
        main.removeCallbacks(closeRunnable)
        ((command as? Command.PlayMedia)?.duration ?: (command as? Command.ShowWeb)?.duration)?.let {
            main.postDelayed(closeRunnable, it * 1000L)
        }
        return true
    }

    fun close() {
        main.removeCallbacks(closeRunnable)
        val frame = frame ?: return
        clear()
        runCatching { windows.removeView(frame) }
        this.frame = null
        hub.update { overlay = null }
    }

    private fun clear() {
        player?.release()
        player = null
        web?.destroy()
        web = null
        frame?.removeAllViews()
    }

    private fun params(layout: Command.Layout): WindowManager.LayoutParams {
        val metrics = app.resources.displayMetrics
        val width = metrics.widthPixels * layout.size / 100
        val margin = (24 * metrics.density).toInt()
        @Suppress("DEPRECATION")
        val type = if (Build.VERSION.SDK_INT >= 26) WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY else WindowManager.LayoutParams.TYPE_PHONE
        return WindowManager.LayoutParams(
            width,
            width * 9 / 16,
            type,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or OverlayWindows.touchFlags(app) or
                WindowManager.LayoutParams.FLAG_HARDWARE_ACCELERATED,
            PixelFormat.TRANSLUCENT,
        ).apply {
            gravity = if (layout.position == Command.Position.CENTER) Gravity.CENTER else ToastViews.gravity(layout.position)
            x = if (layout.position == Command.Position.CENTER) 0 else margin
            y = if (layout.position == Command.Position.CENTER) 0 else margin
            title = "PleatView picture-in-picture"
        }
    }

    private fun open(layout: Command.Layout): FrameLayout? {
        val frame = FrameLayout(app).apply {
            setBackgroundResource(R.drawable.pip_frame)
            setPadding(2, 2, 2, 2)
        }
        return try {
            windows.addView(frame, params(layout))
            this.frame = frame
            frame
        } catch (e: Exception) {
            Log.w(TAG, "Could not show picture-in-picture over other apps", e)
            null
        }
    }

    private fun place(frame: FrameLayout, layout: Command.Layout) {
        runCatching { windows.updateViewLayout(frame, params(layout)) }
    }

    @OptIn(UnstableApi::class)
    private fun play(frame: FrameLayout, command: Command.PlayMedia) {
        val aspect = AspectRatioFrameLayout(app).apply { setBackgroundColor(Color.BLACK) }
        val surface = TextureView(app)
        aspect.addView(surface, FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT))
        frame.addView(aspect, FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT, Gravity.CENTER))
        val http = DefaultHttpDataSource.Factory()
            .setUserAgent("PleatView/${BuildConfig.VERSION_NAME}")
            .setAllowCrossProtocolRedirects(true)
            .setConnectTimeoutMs(10_000)
            .setReadTimeoutMs(20_000)
        // No audio focus and no sound: the other app's film carries on as it was.
        val player = ExoPlayer.Builder(app)
            .setMediaSourceFactory(DefaultMediaSourceFactory(DefaultDataSource.Factory(app, http)))
            .build()
        player.volume = 0f
        player.setVideoTextureView(surface)
        player.addListener(object : Player.Listener {
            override fun onVideoSizeChanged(videoSize: VideoSize) {
                if (videoSize.width > 0 && videoSize.height > 0) {
                    aspect.setAspectRatio(videoSize.width * videoSize.pixelWidthHeightRatio / videoSize.height)
                }
            }

            override fun onPlayerError(error: PlaybackException) {
                if (this@SystemPip.player !== player) return
                if (error.errorCode == PlaybackException.ERROR_CODE_BEHIND_LIVE_WINDOW) {
                    player.seekToDefaultPosition()
                    player.prepare()
                    return
                }
                Log.w(TAG, "Picture-in-picture over other apps: ${error.errorCodeName}")
                val fallback = command.fallbackUrl
                main.post {
                    if (this@SystemPip.player !== player) return@post
                    if (fallback == null) return@post close()
                    clear()
                    showWeb(frame, fallback, Command.WebKind.IMAGE)
                }
            }

            override fun onPlaybackStateChanged(state: Int) {
                if (state == Player.STATE_ENDED && this@SystemPip.player === player) main.post { close() }
            }
        })
        val item = MediaItem.Builder().setUri(command.url)
        val mime = command.mimeType.orEmpty()
        when {
            mime.contains("mpegurl") -> item.setMimeType(MimeTypes.APPLICATION_M3U8)
            mime.contains("dash+xml") -> item.setMimeType(MimeTypes.APPLICATION_MPD)
        }
        player.repeatMode = if (command.loop) Player.REPEAT_MODE_ONE else Player.REPEAT_MODE_OFF
        player.setMediaItem(item.build())
        player.prepare()
        player.play()
        this.player = player
        hub.update { overlay = OverlayInfo(kind = "video", url = command.url, mode = Command.Mode.PIP.wire) }
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun showWeb(frame: FrameLayout, url: String, kind: Command.WebKind) {
        val view = try {
            WebView(app)
        } catch (e: Exception) {
            Log.e(TAG, "WebView unavailable", e)
            return close()
        }
        view.setBackgroundColor(Color.BLACK)
        view.isFocusable = false
        view.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            mediaPlaybackRequiresUserGesture = false
            allowFileAccess = false
            allowContentAccess = false
            mixedContentMode = WebSettings.MIXED_CONTENT_ALWAYS_ALLOW
            useWideViewPort = true
            loadWithOverviewMode = true
        }
        view.webViewClient = WebViewClient()
        frame.addView(view, FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT))
        if (kind == Command.WebKind.IMAGE) {
            view.loadDataWithBaseURL(null, WebOverlay.imagePage(url), "text/html", "utf-8", null)
        } else {
            view.loadUrl(url)
        }
        web = view
        hub.update {
            overlay = OverlayInfo(kind = if (kind == Command.WebKind.IMAGE) "image" else "web", url = url, mode = Command.Mode.PIP.wire)
        }
    }

    private companion object {
        const val TAG = "PleatView"
    }
}
