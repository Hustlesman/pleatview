package com.hustlesman.pleatview

import android.content.Context
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.SurfaceView
import android.view.View
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.ProgressBar
import android.widget.TextView
import androidx.annotation.OptIn
import androidx.media3.common.AudioAttributes
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.common.MediaMetadata
import androidx.media3.common.MimeTypes
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DefaultDataSource
import androidx.media3.datasource.DefaultHttpDataSource
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import androidx.media3.ui.PlayerView

/**
 * Native playback with ExoPlayer: full-screen video, picture-in-picture (camera
 * pop-ups), background audio with a now-playing card, and announcements that duck
 * whatever is playing.
 */
@OptIn(UnstableApi::class)
class MediaController(
    private val context: Context,
    private val container: FrameLayout,
    private val playerView: PlayerView,
    private val nowPlaying: NowPlaying,
    private val hub: Hub,
    private val callbacks: Callbacks,
) {
    interface Callbacks {
        fun onMediaClosed()
        fun onMediaError(message: String)

        /** A camera's video didn't play: show its picture stream in the same place instead. */
        fun onMediaFallback(command: Command.ShowWeb)
    }

    private val main = Handler(Looper.getMainLooper())
    private var player: ExoPlayer? = null
    private var announcer: ExoPlayer? = null
    private var request: Command.PlayMedia? = null
    private var appVolume = 1f
    private var appMuted = false
    private var streamTitle: String? = null
    private var streamArtist: String? = null
    private val stopRunnable = Runnable { stop() }

    val isActive: Boolean get() = request != null
    val isFullscreen: Boolean get() = request?.layout?.mode == Command.Mode.FULLSCREEN

    init {
        // Keep the video surface above WebView video layers when shown as picture-in-picture.
        (playerView.videoSurfaceView as? SurfaceView)?.setZOrderMediaOverlay(true)
        nowPlaying.progress = {
            player?.let { p -> p.currentPosition to p.duration.takeIf { it != C.TIME_UNSET && !p.isCurrentMediaItemLive } }
        }
    }

    fun play(command: Command.PlayMedia) {
        if (command.announce) {
            announce(command)
            return
        }
        val p = player ?: newPlayer().also {
            it.addListener(listener)
            player = it
        }
        request = command
        streamTitle = null
        streamArtist = null
        command.volume?.let { appVolume = it }
        applyVolume()
        p.repeatMode = if (command.loop) Player.REPEAT_MODE_ONE else Player.REPEAT_MODE_OFF
        p.setMediaItem(mediaItem(command), ((command.startPosition ?: 0.0) * 1000).toLong())
        p.prepare()
        p.playWhenReady = true
        showLayout(command)
        main.removeCallbacks(stopRunnable)
        command.duration?.let { main.postDelayed(stopRunnable, it * 1000L) }
        publish()
    }

    fun resume() {
        val p = player ?: return
        if (p.playbackState == Player.STATE_ENDED) p.seekToDefaultPosition()
        p.playWhenReady = true
    }

    fun pause() {
        player?.playWhenReady = false
    }

    fun togglePlayPause() {
        val p = player ?: return
        if (p.playWhenReady) pause() else resume()
    }

    fun seek(seconds: Double) {
        player?.seekTo((seconds * 1000).toLong())
    }

    fun seekBy(deltaMs: Long) {
        val p = player ?: return
        if (p.isCurrentMediaItemSeekable) p.seekTo((p.currentPosition + deltaMs).coerceAtLeast(0))
    }

    fun setVolume(level: Float) {
        appVolume = level.coerceIn(0f, 1f)
        applyVolume()
    }

    fun setMuted(muted: Boolean) {
        appMuted = muted
        applyVolume()
    }

    fun stop() {
        main.removeCallbacks(stopRunnable)
        val wasActive = request != null
        request = null
        player?.let {
            it.removeListener(listener)
            it.release() // frees the hardware decoder, which TVs have few of
        }
        player = null
        playerView.player = null
        container.visibility = View.GONE
        nowPlaying.hide()
        hub.update { media = null }
        if (wasActive) callbacks.onMediaClosed()
    }

    fun release() {
        stop()
        finishAnnouncement()
    }

    private fun showLayout(command: Command.PlayMedia) {
        if (command.layout.mode == Command.Mode.BACKGROUND) {
            playerView.player = null
            container.visibility = View.GONE
            nowPlaying.show(displayTitle(command), command.artist, command.imageUrl)
            return
        }
        nowPlaying.hide()
        val fullscreen = command.layout.mode == Command.Mode.FULLSCREEN
        playerView.player = player
        playerView.useController = fullscreen
        playerView.isFocusable = fullscreen
        Layouts.apply(container, command.layout)
        container.visibility = View.VISIBLE
        if (fullscreen) playerView.requestFocus()
    }

    private fun announce(command: Command.PlayMedia) {
        finishAnnouncement()
        val a = newPlayer()
        announcer = a
        applyVolume() // ducks the main player
        a.volume = if (appMuted) 0f else command.volume ?: appVolume
        a.addListener(object : Player.Listener {
            override fun onPlaybackStateChanged(state: Int) {
                if (state == Player.STATE_ENDED) main.post { if (announcer === a) finishAnnouncement() }
            }

            override fun onPlayerError(error: PlaybackException) {
                Log.w(TAG, "Announcement failed: ${error.errorCodeName}")
                main.post { if (announcer === a) finishAnnouncement() }
            }
        })
        a.setMediaItem(mediaItem(command))
        a.prepare()
        a.play()
    }

    private fun finishAnnouncement() {
        announcer?.release()
        announcer = null
        applyVolume()
    }

    private fun applyVolume() {
        val duck = if (announcer != null) DUCK_FACTOR else 1f
        player?.volume = if (appMuted) 0f else appVolume * duck
        val volume = appVolume.toDouble()
        val muted = appMuted
        hub.update {
            this.volume = volume
            this.muted = muted
        }
    }

    private fun newPlayer(): ExoPlayer {
        val http = DefaultHttpDataSource.Factory()
            .setUserAgent("PleatView/${BuildConfig.VERSION_NAME}")
            .setAllowCrossProtocolRedirects(true)
            .setConnectTimeoutMs(10_000)
            .setReadTimeoutMs(20_000)
        return ExoPlayer.Builder(context)
            .setMediaSourceFactory(DefaultMediaSourceFactory(DefaultDataSource.Factory(context, http)))
            .setAudioAttributes(
                AudioAttributes.Builder().setUsage(C.USAGE_MEDIA).setContentType(C.AUDIO_CONTENT_TYPE_MOVIE).build(),
                false,
            )
            .build()
    }

    private fun mediaItem(command: Command.PlayMedia): MediaItem {
        // The demo's video comes from the app itself.
        val builder = MediaItem.Builder().setUri(Demo.asset(command.url)?.let { "asset:///$it" } ?: command.url)
        val mime = command.mimeType.orEmpty()
        when {
            mime.contains("mpegurl") -> builder.setMimeType(MimeTypes.APPLICATION_M3U8)
            mime.contains("dash+xml") -> builder.setMimeType(MimeTypes.APPLICATION_MPD)
        }
        builder.setMediaMetadata(MediaMetadata.Builder().setTitle(command.title).setArtist(command.artist).build())
        return builder.build()
    }

    private fun displayTitle(command: Command.PlayMedia): String =
        command.title ?: streamTitle ?: command.url.substringBefore('?').substringAfterLast('/').ifEmpty { command.url }

    private fun publish() {
        val p = player
        val r = request
        if (p == null || r == null) {
            hub.update { media = null }
            return
        }
        val state = when {
            !p.playWhenReady -> "paused"
            p.isPlaying -> "playing"
            p.playbackState == Player.STATE_BUFFERING || p.playbackState == Player.STATE_IDLE -> "buffering"
            p.playbackState == Player.STATE_ENDED -> "idle"
            else -> "paused" // ready but suppressed, e.g. by audio focus
        }
        val duration = p.duration.takeIf { it != C.TIME_UNSET && !p.isCurrentMediaItemLive }?.let { it / 1000.0 }
        val info = MediaInfo(
            state = state,
            url = r.url,
            mimeType = r.mimeType,
            title = r.title ?: streamTitle,
            artist = r.artist ?: streamArtist,
            imageUrl = r.imageUrl,
            mode = r.layout.mode.wire,
            position = p.currentPosition.coerceAtLeast(0) / 1000.0,
            duration = duration,
            updatedAt = System.currentTimeMillis() / 1000.0,
        )
        hub.update { media = info }
    }

    private val listener = object : Player.Listener {
        override fun onEvents(player: Player, events: Player.Events) {
            if (events.containsAny(
                    Player.EVENT_PLAYBACK_STATE_CHANGED,
                    Player.EVENT_PLAY_WHEN_READY_CHANGED,
                    Player.EVENT_IS_PLAYING_CHANGED,
                    Player.EVENT_POSITION_DISCONTINUITY,
                    Player.EVENT_MEDIA_METADATA_CHANGED,
                    Player.EVENT_TIMELINE_CHANGED,
                )
            ) {
                publish()
            }
        }

        override fun onPlaybackStateChanged(state: Int) {
            if (state == Player.STATE_ENDED && request?.loop != true) main.post { stop() }
        }

        override fun onMediaMetadataChanged(metadata: MediaMetadata) {
            // Internet radio (ICY) and HLS streams report their own titles.
            metadata.title?.let { streamTitle = it.toString() }
            metadata.artist?.let { streamArtist = it.toString() }
            request?.takeIf { it.layout.mode == Command.Mode.BACKGROUND }?.let {
                nowPlaying.update(displayTitle(it), it.artist ?: streamArtist)
            }
        }

        override fun onPlayerError(error: PlaybackException) {
            val p = player ?: return
            if (error.errorCode == PlaybackException.ERROR_CODE_BEHIND_LIVE_WINDOW) {
                p.seekToDefaultPosition()
                p.prepare()
                return
            }
            Log.w(TAG, "Playback failed: ${error.errorCodeName}", error)
            val failed = request
            main.post { stop() }
            val fallback = failed?.fallbackUrl
            if (fallback != null) {
                callbacks.onMediaFallback(Command.ShowWeb(fallback, Command.WebKind.IMAGE, failed.layout, failed.duration))
            } else {
                callbacks.onMediaError(error.errorCodeName)
            }
        }
    }

    companion object {
        private const val TAG = "PleatView"
        private const val DUCK_FACTOR = 0.2f
    }
}

/** The small card shown while audio plays over the dashboard. */
class NowPlaying(private val root: View) {
    private val title: TextView = root.findViewById(R.id.now_playing_title)
    private val subtitle: TextView = root.findViewById(R.id.now_playing_subtitle)
    private val art: ImageView = root.findViewById(R.id.now_playing_art)
    private val bar: ProgressBar = root.findViewById(R.id.now_playing_progress)
    private val main = Handler(Looper.getMainLooper())

    /** Returns (position, duration or null) in milliseconds. */
    var progress: (() -> Pair<Long, Long?>?)? = null

    private val ticker = object : Runnable {
        override fun run() {
            val (position, duration) = progress?.invoke() ?: (0L to null)
            if (duration != null && duration > 0) {
                bar.visibility = View.VISIBLE
                bar.progress = (position * 1000 / duration).toInt().coerceIn(0, 1000)
            } else {
                bar.visibility = View.INVISIBLE
            }
            main.postDelayed(this, 1000)
        }
    }

    fun show(title: String, subtitle: String?, imageUrl: String?) {
        update(title, subtitle)
        art.setImageDrawable(null)
        art.visibility = View.GONE
        imageUrl?.let { ImageLoader.load(it, art, maxSize = 256) }
        root.visibility = View.VISIBLE
        main.removeCallbacks(ticker)
        main.post(ticker)
    }

    fun update(title: String, subtitle: String?) {
        this.title.text = title
        this.subtitle.text = subtitle.orEmpty()
        this.subtitle.visibility = if (subtitle.isNullOrEmpty()) View.GONE else View.VISIBLE
    }

    fun hide() {
        root.visibility = View.GONE
        main.removeCallbacks(ticker)
    }
}
