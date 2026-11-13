package com.hustlesman.pleatview

import android.app.Activity
import android.content.Context
import android.graphics.BitmapFactory
import android.graphics.Color
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.widget.FrameLayout
import android.widget.ImageView
import androidx.annotation.OptIn
import androidx.media3.common.MediaItem
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.common.util.UnstableApi
import androidx.media3.database.StandaloneDatabaseProvider
import androidx.media3.datasource.DataSourceInputStream
import androidx.media3.datasource.DataSpec
import androidx.media3.datasource.DefaultHttpDataSource
import androidx.media3.datasource.cache.CacheDataSource
import androidx.media3.datasource.cache.CacheWriter
import androidx.media3.datasource.cache.LeastRecentlyUsedCacheEvictor
import androidx.media3.datasource.cache.SimpleCache
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import androidx.media3.ui.AspectRatioFrameLayout
import androidx.media3.ui.PlayerView
import java.io.File
import java.util.concurrent.Executors

/**
 * A home page that is a playlist of media (`home_playlist`, from another controller): pictures
 * for their seconds, videos and sounds to the end, in order, again and again. Every item is
 * kept in the app's own cache as well, so the list plays on while the network or the controller
 * is gone, and after a restart.
 */
@OptIn(UnstableApi::class)
class HomePlaylist(private val activity: Activity, private val container: FrameLayout) {
    private val main = Handler(Looper.getMainLooper())
    private val loader = Executors.newSingleThreadExecutor()
    private var items: List<PlaylistItem> = emptyList()
    private var index = 0
    private var frame: FrameLayout? = null
    private var playerView: PlayerView? = null
    private var imageView: ImageView? = null
    private var player: ExoPlayer? = null
    private var paused = false
    private var failuresInARow = 0
    private val next = Runnable { advance() }

    val isShown: Boolean get() = frame != null

    /** Plays [items] full screen; the same list again keeps playing where it is. */
    fun show(items: List<PlaylistItem>) {
        if (items == this.items && frame != null) {
            frame?.bringToFront()
            return
        }
        hide()
        this.items = items
        index = 0
        failuresInARow = 0
        val frame = FrameLayout(activity).apply { setBackgroundColor(Color.BLACK) }
        val playerView = PlayerView(activity).apply {
            useController = false
            setShutterBackgroundColor(Color.BLACK)
            resizeMode = AspectRatioFrameLayout.RESIZE_MODE_FIT
            isFocusable = false
        }
        val imageView = ImageView(activity).apply { scaleType = ImageView.ScaleType.FIT_CENTER }
        val full = FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT)
        frame.addView(playerView, full)
        frame.addView(imageView, FrameLayout.LayoutParams(full))
        container.addView(frame, FrameLayout.LayoutParams(full))
        this.frame = frame
        this.playerView = playerView
        this.imageView = imageView
        precache(items)
        if (!paused) play()
    }

    fun hide() {
        main.removeCallbacks(next)
        releasePlayer()
        frame?.let { container.removeView(it) }
        frame = null
        playerView = null
        imageView = null
        items = emptyList()
    }

    fun onPause() {
        paused = true
        main.removeCallbacks(next)
        releasePlayer()
    }

    fun onResume() {
        paused = false
        if (frame != null) play()
    }

    private fun play() {
        val item = items.getOrNull(index) ?: return
        main.removeCallbacks(next)
        if (item.kind == PlaylistItem.IMAGE) showImage(item) else playMedia(item)
    }

    private fun advance() {
        if (items.isEmpty()) return
        index = (index + 1) % items.size
        play()
    }

    /** A failed item is skipped; when nothing plays at all (offline, nothing cached yet), wait. */
    private fun failed(item: PlaylistItem, why: String) {
        Log.w(TAG, "Playlist item ${item.url} didn't play: $why")
        failuresInARow++
        main.postDelayed(next, if (failuresInARow >= items.size) RETRY_ALL_MS else SKIP_MS)
    }

    private fun showImage(item: PlaylistItem) {
        releasePlayer()
        playerView?.visibility = android.view.View.INVISIBLE
        val target = imageView ?: return
        loader.execute {
            val bitmap = runCatching {
                DataSourceInputStream(dataSource(activity), DataSpec(Uri.parse(item.url))).use { input ->
                    BitmapFactory.decodeStream(input)
                }
            }.getOrNull()
            main.post {
                if (imageView !== target || items.getOrNull(index) != item) return@post
                if (bitmap == null) {
                    failed(item, "picture not readable")
                    return@post
                }
                failuresInARow = 0
                target.setImageBitmap(bitmap)
                target.visibility = android.view.View.VISIBLE
                main.postDelayed(next, item.seconds * 1000L)
            }
        }
    }

    private fun playMedia(item: PlaylistItem) {
        releasePlayer()
        imageView?.visibility = android.view.View.INVISIBLE
        imageView?.setImageDrawable(null)
        playerView?.visibility = android.view.View.VISIBLE
        val player = ExoPlayer.Builder(activity)
            .setMediaSourceFactory(DefaultMediaSourceFactory(cacheFactory(activity)))
            .build()
        player.addListener(object : Player.Listener {
            override fun onPlaybackStateChanged(state: Int) {
                if (state == Player.STATE_READY) failuresInARow = 0
                if (state == Player.STATE_ENDED) main.post(next)
            }

            override fun onPlayerError(error: PlaybackException) {
                releasePlayer()
                failed(item, error.errorCodeName)
            }
        })
        player.setMediaItem(MediaItem.fromUri(item.url))
        player.playWhenReady = true
        player.prepare()
        playerView?.player = player
        this.player = player
    }

    private fun releasePlayer() {
        playerView?.player = null
        player?.release()
        player = null
    }

    /** Fetches every item into the cache in the background, so all of them play offline. */
    private fun precache(items: List<PlaylistItem>) {
        val context = activity.applicationContext
        loader.execute {
            for (item in items) {
                runCatching {
                    CacheWriter(cacheFactory(context).createDataSource(), DataSpec(Uri.parse(item.url)), null, null).cache()
                }.onFailure { Log.d(TAG, "Not cached yet: ${item.url}: ${it.message}") }
            }
        }
    }

    companion object {
        private const val TAG = "PleatView"
        private const val SKIP_MS = 1_000L
        private const val RETRY_ALL_MS = 15_000L
        private const val CACHE_BYTES = 2L * 1024 * 1024 * 1024

        @Volatile private var cache: SimpleCache? = null

        /** One cache for the app, in its files (which the system doesn't clear like its cache). */
        private fun cache(context: Context): SimpleCache = cache ?: synchronized(this) {
            cache ?: run {
                val folder = File(context.filesDir, "playlist-cache")
                val limit = minOf(CACHE_BYTES, context.filesDir.usableSpace / 4)
                SimpleCache(folder, LeastRecentlyUsedCacheEvictor(limit), StandaloneDatabaseProvider(context))
                    .also { cache = it }
            }
        }

        private fun cacheFactory(context: Context): CacheDataSource.Factory = CacheDataSource.Factory()
            .setCache(cache(context))
            .setUpstreamDataSourceFactory(
                DefaultHttpDataSource.Factory()
                    .setUserAgent("PleatView/${BuildConfig.VERSION_NAME}")
                    .setConnectTimeoutMs(10_000)
                    .setReadTimeoutMs(20_000),
            )

        private fun dataSource(context: Context) = cacheFactory(context).createDataSource()
    }
}

/** One item of a home playlist. */
data class PlaylistItem(val url: String, val kind: String, val seconds: Int) {
    companion object {
        const val VIDEO = "video"
        const val AUDIO = "audio"
        const val IMAGE = "image"
        val KINDS = listOf(VIDEO, AUDIO, IMAGE)
    }
}
