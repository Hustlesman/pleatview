package com.hustlesman.pleatview

import android.content.Context
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.webkit.WebResourceResponse
import androidx.webkit.WebViewAssetLoader

/**
 * The built-in demo, for people without Home Assistant yet and for app-store reviewers: a
 * sample home (assets/demo) and a tour of what Home Assistant can show on top of it.
 */
object Demo {
    /** Where the demo's pages live. The address never reaches the network: the app answers it. */
    const val BASE = "https://${WebViewAssetLoader.DEFAULT_DOMAIN}/demo/"
    const val HOME = BASE + "index.html"
    const val ENERGY = BASE + "energy.html"
    const val CAMERAS = BASE + "cameras.html"
    const val DOOR_CAMERA = BASE + "camera.html?id=front-door"
    const val VIDEO = BASE + "clip.mp4"
    const val VIDEO_TITLE = "Caminandes: Gran Dillama"
    const val VIDEO_CREDIT = "© 2013 Blender Foundation, caminandes.com, CC BY 3.0"

    fun config(context: Context) = Config(
        dashboards = listOf(
            Dashboard(context.getString(R.string.demo_overview), HOME),
            Dashboard(context.getString(R.string.demo_energy), ENERGY),
            Dashboard(context.getString(R.string.demo_cameras), CAMERAS),
        ),
        homeDashboard = HOME,
    )

    /** The file in the app's assets behind a demo address, or null for any other address. */
    fun asset(url: String): String? {
        if (!url.startsWith(BASE)) return null
        val path = url.removePrefix(BASE).substringBefore('#').substringBefore('?')
        if (path.split('/').any { it.isEmpty() || it == "." || it == ".." }) return null
        return "demo/$path"
    }

    /** Answers a WebView's requests for the demo's pages; null for every other address. */
    class Pages(context: Context) {
        private val assets = WebViewAssetLoader.AssetsPathHandler(context.applicationContext)
        private val loader = WebViewAssetLoader.Builder()
            .addPathHandler("/demo/") { path -> assets.handle("demo/$path") }
            .build()

        fun serve(url: Uri): WebResourceResponse? = loader.shouldInterceptRequest(url)
    }
}

/**
 * The demo's tour: a few things Home Assistant could send, each with a caption that says
 * what it is. The steps go through the same code as Home Assistant's own commands.
 */
class DemoTour(private val context: Context, private val stage: Stage) {
    interface Stage {
        fun handle(command: Command)

        /** Shows [text] for [seconds], at the bottom of the screen or the top; null hides it. */
        fun caption(text: String?, seconds: Int = 0, top: Boolean = false)
    }

    val config = Demo.config(context)
    private val main = Handler(Looper.getMainLooper())

    /** The last step has run. */
    var finished = false
        private set

    /** The tour's video is on screen; the last caption waits for it. */
    private var playing = false
    private val end = Runnable {
        finished = true
        stage.caption(context.getString(R.string.demo_end), 20)
    }

    fun start() {
        stop()
        finished = false
        playing = false
        val start = SystemClock.uptimeMillis()
        for ((second, step) in steps()) main.postAtTime(step, this, start + second * 1000L)
        // In case the video never gets going.
        main.postAtTime(end, this, start + LAST_STEP_MS)
    }

    /** A video stopped, at its end or for Back: if it was the tour's, the last caption follows. */
    fun mediaClosed() {
        if (!playing) return
        playing = false
        main.removeCallbacks(end, this)
        main.postAtTime(end, this, SystemClock.uptimeMillis() + 1_500)
    }

    fun stop() {
        main.removeCallbacksAndMessages(this)
        playing = false
        stage.caption(null)
    }

    /** Someone took over (opened the menu): the tour stays stopped, even after another app. */
    fun cancel() {
        stop()
        finished = true
    }

    private fun steps(): List<Pair<Int, Runnable>> {
        // Below a tablet's width, the bottom of the screen is the caption's: the camera goes up top.
        val wide = context.resources.configuration.screenWidthDp >= 600
        val corner = if (wide) Command.Layout(Command.Mode.PIP) else Command.Layout(Command.Mode.PIP, Command.Position.TOP_RIGHT, 60)
        return listOf(
            2 to Runnable { stage.caption(context.getString(R.string.demo_intro), 8) },
            11 to Runnable {
                stage.caption(context.getString(R.string.demo_notify_caption), 10)
                stage.handle(
                    Command.Notify(
                        context.getString(R.string.demo_notify_title), context.getString(R.string.demo_notify_message),
                        null, 10, Command.Position.TOP_RIGHT,
                    ),
                )
            },
            22 to Runnable {
                stage.caption(context.getString(R.string.demo_camera_caption), 16)
                stage.handle(Command.ShowWeb(Demo.DOOR_CAMERA, Command.WebKind.PAGE, corner, 18))
            },
            42 to Runnable {
                stage.caption(context.getString(R.string.demo_dashboard_caption), 15)
                stage.handle(Command.ShowDashboard(Demo.ENERGY, 15))
            },
            59 to Runnable {
                stage.caption(context.getString(R.string.demo_video_caption, "${Demo.VIDEO_TITLE}, ${Demo.VIDEO_CREDIT}"), 14, top = true)
            },
            61 to Runnable {
                playing = true
                stage.handle(
                    Command.PlayMedia(
                        url = Demo.VIDEO, mimeType = "video/mp4", title = Demo.VIDEO_TITLE, artist = Demo.VIDEO_CREDIT,
                        imageUrl = null, layout = Command.Layout(Command.Mode.FULLSCREEN), duration = null, loop = false,
                        announce = false, volume = null, startPosition = null,
                    ),
                )
            },
        )
    }

    companion object {
        private const val LAST_STEP_MS = 100_000L
    }
}
