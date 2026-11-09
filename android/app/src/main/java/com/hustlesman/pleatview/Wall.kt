package com.hustlesman.pleatview

import android.annotation.SuppressLint
import android.app.Activity
import android.graphics.Bitmap
import android.graphics.Color
import android.os.Handler
import android.os.Looper
import android.webkit.PermissionRequest
import android.webkit.RenderProcessGoneDetail
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import android.widget.LinearLayout
import androidx.core.graphics.createBitmap
import androidx.webkit.ProfileStore
import androidx.webkit.ScriptHandler
import androidx.webkit.WebViewCompat
import androidx.webkit.WebViewFeature
import java.net.URI
import java.net.URLDecoder
import kotlin.math.ceil
import kotlin.math.roundToInt
import kotlin.math.sqrt

/**
 * A wall of YouTube streams, https://pleatview.hustlesman.com/wall?v=ID,ID,…, which the app shows
 * itself rather than loading that page. A renderer runs the scripts of every player it shows on one
 * thread, and on a TV box that thread keeps up with only a few YouTube players: nine stall (issue #77).
 * Each row of the wall gets its own WebView profile, which Chromium gives its own renderer process,
 * so the rows play side by side on the processor's cores.
 */
class Wall(private val activity: Activity, private val container: FrameLayout, private val onTrouble: (String) -> Unit) {
    private val main = Handler(Looper.getMainLooper())
    private var layout: LinearLayout? = null
    private var rows: List<List<String>> = emptyList()
    private var columns = 1
    private var viewportWidth = 0

    val isShown: Boolean get() = layout != null

    /**
     * Shows [ids] as a wall, at the zoom of a page [viewportWidth] CSS pixels wide (0: the screen's
     * own). The same wall again stays as it is: when the low-memory killer takes the dashboard's
     * idle renderer and PleatView recreates that WebView, the streams play on.
     */
    fun show(ids: List<String>, viewportWidth: Int) {
        if (isShown && ids == rows.flatten() && viewportWidth == this.viewportWidth) return
        hide()
        this.viewportWidth = viewportWidth
        columns = columns(ids.size)
        rows = ids.chunked(columns)
        val layout = LinearLayout(activity).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(Color.BLACK)
        }
        rows.indices.forEach { layout.addView(row(it), rowParams(it)) }
        container.addView(layout, FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT))
        this.layout = layout
    }

    /** Starts the wall again, as a reload does for a page. */
    fun reload() {
        if (!isShown) return
        val ids = rows.flatten()
        hide()
        show(ids, viewportWidth)
    }

    fun hide() {
        main.removeCallbacksAndMessages(null)
        val layout = layout ?: return
        container.removeView(layout)
        for (i in 0 until layout.childCount) (layout.getChildAt(i) as WebView).destroy()
        this.layout = null
    }

    fun onPause() = forEachRow { it.onPause() }

    fun onResume() = forEachRow { it.onResume() }

    private fun forEachRow(action: (WebView) -> Unit) {
        val layout = layout ?: return
        for (i in 0 until layout.childCount) action(layout.getChildAt(i) as WebView)
    }

    private fun rowParams(index: Int) = LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f).apply {
        // The same gap as between the players in a row: GAP CSS pixels at the page's zoom.
        val cssPixel = if (viewportWidth > 0 && container.width > 0) container.width.toFloat() / viewportWidth else activity.resources.displayMetrics.density
        if (index > 0) topMargin = (GAP * cssPixel).roundToInt()
    }

    @SuppressLint("SetJavaScriptEnabled", "RequiresFeature") // setProfile only runs when MULTI_PROFILE does
    private fun row(index: Int): WebView {
        val view = WebView(activity)
        if (WebViewFeature.isFeatureSupported(WebViewFeature.MULTI_PROFILE)) {
            val name = "wall-$index"
            ProfileStore.getInstance().getOrCreateProfile(name)
            WebViewCompat.setProfile(view, name)
        }
        forgetQualityCap(view)
        view.isFocusable = false
        view.setBackgroundColor(Color.BLACK)
        view.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            mediaPlaybackRequiresUserGesture = false
            allowFileAccess = false
            allowContentAccess = false
            // The row's viewport width is PleatView's zoom.
            useWideViewPort = true
            loadWithOverviewMode = true
            userAgentString = "$userAgentString PleatView/${BuildConfig.VERSION_NAME}"
        }
        view.webViewClient = object : WebViewClient() {
            // The wall stays a wall: a link out of a player doesn't take over its row.
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest) = request.isForMainFrame

            override fun onRenderProcessGone(view: WebView, detail: RenderProcessGoneDetail): Boolean {
                val layout = layout ?: return true
                val at = layout.indexOfChild(view)
                if (at < 0) return true
                onTrouble("A row of the wall stopped, so PleatView started it again")
                layout.removeViewAt(at)
                view.destroy()
                main.postDelayed({ if (this@Wall.layout === layout) layout.addView(row(at), at, rowParams(at)) }, 1_000)
                return true
            }
        }
        view.webChromeClient = object : WebChromeClient() {
            // Without this, Android WebView shows a grey "play" poster before videos start.
            override fun getDefaultVideoPoster(): Bitmap = createBitmap(1, 1)

            override fun onPermissionRequest(request: PermissionRequest) = request.deny()
        }
        // The page's address is the wall's own, which YouTube's players are told as the referrer.
        view.loadDataWithBaseURL(PAGE, html(rows[index], columns, viewportWidth), "text/html", "utf-8", null)
        return view
    }

    companion object {
        const val PAGE = "https://pleatview.hustlesman.com/wall"
        const val MAX_STREAMS = 16
        private const val HOST = "pleatview.hustlesman.com"
        private const val GAP = 4
        private val ID = Regex("^[A-Za-z0-9_-]{11}$")
        private val YOUTUBE_ORIGINS = setOf("https://www.youtube.com", "https://www.youtube-nocookie.com")
        private const val FORGET_QUALITY_CAP = "try{localStorage.removeItem('yt-player-performance-cap');" +
            "localStorage.removeItem('yt-player-performance-cap-active-set')}catch(e){}"

        /** The YouTube video IDs of a wall address, or null if [url] isn't one. */
        fun ids(url: String): List<String>? {
            val uri = runCatching { URI(url) }.getOrNull() ?: return null
            if (uri.scheme?.lowercase() !in listOf("https", "http") || uri.host?.lowercase() != HOST) return null
            if (uri.rawPath.orEmpty().removeSuffix("/").removeSuffix(".html") != "/wall") return null
            val value = uri.rawQuery.orEmpty().split('&')
                .firstNotNullOfOrNull { param -> param.split('=', limit = 2).takeIf { it[0] == "v" }?.getOrNull(1) }
                ?: return null
            val ids = URLDecoder.decode(value, "UTF-8").split(',').map { it.trim() }.filter { ID.matches(it) }
            return ids.take(MAX_STREAMS).ifEmpty { null }
        }

        /** As many columns as rows, or one more: 4 streams are 2 by 2, 9 are 3 by 3, 5 are 3 and 2. */
        fun columns(count: Int): Int = ceil(sqrt(count.toDouble())).toInt().coerceAtLeast(1)

        /** One row of the wall: muted players side by side, each a column of a [columns]-wide grid. */
        fun html(ids: List<String>, columns: Int, viewportWidth: Int): String {
            val viewport = if (viewportWidth > 0) "width=$viewportWidth" else "width=device-width, initial-scale=1"
            val players = ids.joinToString("") { id ->
                "<iframe src=\"https://www.youtube-nocookie.com/embed/$id?autoplay=1&amp;mute=1&amp;controls=0&amp;playsinline=1" +
                    "&amp;rel=0&amp;loop=1&amp;playlist=$id\" allow=\"autoplay; encrypted-media\"" +
                    " referrerpolicy=\"strict-origin-when-cross-origin\"></iframe>"
            }
            return "<!doctype html><html><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"$viewport\">" +
                "<style>html,body{margin:0;height:100%;background:#000;overflow:hidden}" +
                "body{display:grid;gap:${GAP}px;grid-template-columns:repeat($columns,1fr)}" +
                "iframe{width:100%;height:100%;border:0}</style></head><body>$players</body></html>"
        }

        /**
         * YouTube remembers a device that dropped frames and keeps its players at low quality for
         * 30 days, even once they'd play well (issue #77). In [view], every player starts afresh
         * and finds its quality again.
         */
        fun forgetQualityCap(view: WebView): ScriptHandler? {
            if (!WebViewFeature.isFeatureSupported(WebViewFeature.DOCUMENT_START_SCRIPT)) return null
            return WebViewCompat.addDocumentStartJavaScript(view, FORGET_QUALITY_CAP, YOUTUBE_ORIGINS)
        }
    }
}
