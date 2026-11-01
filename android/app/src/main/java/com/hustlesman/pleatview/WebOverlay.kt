package com.hustlesman.pleatview

import android.annotation.SuppressLint
import android.app.Activity
import android.graphics.Bitmap
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.View
import android.webkit.RenderProcessGoneDetail
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import androidx.core.graphics.createBitmap
import org.json.JSONObject

/**
 * A second, short-lived WebView on top of the dashboard for `show_url`, images and
 * MJPEG camera streams. It never gets the Home Assistant auth bridge, and the
 * dashboard underneath keeps running so closing the overlay is instant.
 */
class WebOverlay(
    private val activity: Activity,
    private val container: FrameLayout,
    private val hub: Hub,
    private val onClosed: () -> Unit,
) {
    private val main = Handler(Looper.getMainLooper())
    private var webView: WebView? = null
    private val closeRunnable = Runnable { close() }

    var current: Command.ShowWeb? = null
        private set

    val isFullscreen: Boolean get() = current?.layout?.mode == Command.Mode.FULLSCREEN

    fun show(command: Command.ShowWeb) {
        val view = webView ?: create() ?: return
        current = command
        Layouts.apply(container, command.layout)
        container.visibility = View.VISIBLE
        when (command.kind) {
            Command.WebKind.IMAGE -> view.loadDataWithBaseURL(null, imagePage(command.url), "text/html", "utf-8", null)
            Command.WebKind.PAGE -> view.loadUrl(command.url)
        }
        val fullscreen = command.layout.mode == Command.Mode.FULLSCREEN
        view.isFocusable = fullscreen
        if (fullscreen) view.requestFocus()
        main.removeCallbacks(closeRunnable)
        command.duration?.let { main.postDelayed(closeRunnable, it * 1000L) }
        hub.update {
            overlay = OverlayInfo(
                kind = if (command.kind == Command.WebKind.IMAGE) "image" else "web",
                url = command.url,
                mode = command.layout.mode.wire,
            )
        }
    }

    fun close() {
        main.removeCallbacks(closeRunnable)
        if (current == null && webView == null) return
        current = null
        container.visibility = View.GONE
        webView?.let {
            container.removeView(it)
            it.destroy()
        }
        webView = null
        hub.update { overlay = null }
        onClosed()
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun create(): WebView? {
        val view = try {
            WebView(activity)
        } catch (e: Exception) {
            Log.e("PleatView", "WebView unavailable", e)
            return null
        }
        view.setBackgroundColor(android.graphics.Color.BLACK)
        view.isVerticalScrollBarEnabled = false
        view.isHorizontalScrollBarEnabled = false
        view.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            mediaPlaybackRequiresUserGesture = false
            // Pages never need the TV's files or other apps' content.
            allowFileAccess = false
            allowContentAccess = false
            mixedContentMode = WebSettings.MIXED_CONTENT_ALWAYS_ALLOW
            useWideViewPort = true
            loadWithOverviewMode = true
        }
        view.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                val scheme = request.url.scheme?.lowercase()
                return scheme != "http" && scheme != "https"
            }

            override fun onRenderProcessGone(view: WebView, detail: RenderProcessGoneDetail): Boolean {
                if (view === webView) {
                    webView = null
                    container.removeView(view)
                    view.destroy()
                    current = null
                    container.visibility = View.GONE
                    hub.update { overlay = null }
                    onClosed()
                }
                return true
            }
        }
        view.webChromeClient = object : WebChromeClient() {
            override fun getDefaultVideoPoster(): Bitmap = createBitmap(1, 1)
        }
        container.addView(view, FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT))
        webView = view
        return view
    }

    /** `<img>` handles JPEG/PNG/GIF/WebP/SVG and MJPEG streams (camera proxies) alike. */
    private fun imagePage(url: String): String =
        "<!doctype html><html><head><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">" +
            "<style>html,body{margin:0;height:100%;background:#000;overflow:hidden}" +
            "img{width:100%;height:100%;object-fit:contain;display:block}</style></head>" +
            "<body><img id=\"i\" alt=\"\"><script>document.getElementById('i').src=${JSONObject.quote(url)};</script>" +
            "</body></html>"
}
