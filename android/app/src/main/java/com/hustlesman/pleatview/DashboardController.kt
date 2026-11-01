package com.hustlesman.pleatview

import android.annotation.SuppressLint
import android.app.Activity
import android.content.Context
import android.graphics.Bitmap
import android.net.ConnectivityManager
import android.net.Network
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.util.Log
import android.webkit.ConsoleMessage
import android.webkit.HttpAuthHandler
import android.webkit.JavascriptInterface
import android.webkit.PermissionRequest
import android.webkit.RenderProcessGoneDetail
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import androidx.core.content.ContextCompat
import androidx.core.graphics.createBitmap
import androidx.webkit.ScriptHandler
import androidx.webkit.WebViewCompat
import androidx.webkit.WebViewFeature
import org.json.JSONObject
import kotlin.math.roundToInt

/**
 * Owns the dashboard WebView and keeps it alive: signs the Home Assistant frontend in
 * with the provisioned token, retries when HA is unreachable, recreates the WebView
 * when its renderer dies or freezes, and reloads pages that stop responding.
 */
class DashboardController(
    private val activity: Activity,
    private val container: FrameLayout,
    private val hub: Hub,
    private val callbacks: Callbacks,
) {
    interface Callbacks {
        /** A problem worth showing on screen, or null when it's resolved. */
        fun onStatus(message: String?)

        /** A page asks for a username and password, or null once it no longer does. */
        fun onSignIn(request: SignInRequest?)
    }

    private val main = Handler(Looper.getMainLooper())
    private var config = Config()
    private var webView: WebView? = null
    private var scriptHandler: ScriptHandler? = null
    private var scriptTemplate: String? = null
    private var zoomHandler: ScriptHandler? = null
    private var zoomTemplate: String? = null
    private var listenerMode = false

    private var currentUrl: String? = null
    private var reportedPath: String? = null
    private var loadStartedAt = 0L
    private var lastHeartbeat = 0L
    private var disconnectedSince = 0L
    private var probeSentAt = 0L
    private var ready = false
    private var failed = false
    private var paused = false
    private var clearHistoryOnLoad = false
    private var retryAttempt = 0
    private val forcedAuth = ArrayDeque<Long>()
    private var networkCallback: ConnectivityManager.NetworkCallback? = null
    private val signInFlow = SignInFlow(PleatApp.from(activity).signIns, main) { request ->
        hub.update { this.signIn = request }
        callbacks.onSignIn(request)
    }

    /** The last dashboard PleatView navigated to on its own (not by remote-control browsing). */
    var lastTarget: String = "/"
        private set

    private val retryRunnable = Runnable { currentUrl?.let(::load) }
    private var recreateTarget: String? = null
    private val recreateRunnable = Runnable {
        create()
        load(recreateTarget ?: config.resolve(config.homePath))
    }
    private val reloadRunnable = Runnable {
        Log.i(TAG, "Periodic reload")
        reload()
        scheduleReload()
    }
    private val watchdogRunnable = object : Runnable {
        override fun run() {
            watchdog()
            main.postDelayed(this, WATCHDOG_INTERVAL_MS)
        }
    }

    init {
        if (BuildConfig.DEBUG) WebView.setWebContentsDebuggingEnabled(true)
    }

    fun start(config: Config) {
        this.config = config
        signInFlow.config = config
        create()
        navigate(config.homePath)
        main.removeCallbacks(watchdogRunnable)
        main.postDelayed(watchdogRunnable, WATCHDOG_INTERVAL_MS)
        scheduleReload()
        registerNetworkCallback()
    }

    fun applyConfig(old: Config, new: Config) {
        config = new
        signInFlow.config = new
        if (old.haUrl != new.haUrl || old.haToken != new.haToken || old.zoom != new.zoom || old.kiosk != new.kiosk) {
            hub.update { authError = false }
            create()
            navigate(new.homePath)
        } else if (old.homePath != new.homePath && lastTarget == old.homePath) {
            navigate(new.homePath)
        }
        if (old.reloadMinutes != new.reloadMinutes) scheduleReload()
    }

    /** A username and password the user entered for the page that asked. */
    fun applySignIn(username: String, password: String, remember: Boolean) = signInFlow.apply(username, password, remember)

    /** "Not now" for the current sign-in request. */
    fun dismissSignIn() = signInFlow.dismiss()

    val signInRequest: SignInRequest? get() = signInFlow.pending

    /** Shows a dashboard path (or URL), in place when the HA frontend is already running. */
    fun navigate(target: String) {
        lastTarget = target
        signInFlow.target = target
        val url = config.resolve(target)
        val view = webView ?: return
        if (ready && !failed && config.isHaUrl(url) && config.isHaUrl(view.url)) {
            val path = Config.pathOf(url)
            view.evaluateJavascript("window.__pleatview&&window.__pleatview.navigate(${JSONObject.quote(path)})", null)
            currentUrl = url
            view.clearHistory()
            publishLocation(path)
        } else {
            load(url)
        }
    }

    fun reload() {
        val view = webView ?: return
        val url = currentUrl ?: config.resolve(config.homePath)
        if (failed || view.url.isNullOrEmpty() || view.url == BLANK) {
            load(url)
        } else {
            ready = false
            lastHeartbeat = 0
            loadStartedAt = SystemClock.elapsedRealtime()
            view.reload()
        }
    }

    /**
     * Back inside the page (closes HA dialogs, undoes remote-control browsing). It stays on
     * the site on screen: going back into another dashboard's site would reopen it, password
     * prompt and all (issue #31).
     */
    fun goBack(): Boolean {
        val view = webView ?: return false
        if (!view.canGoBack()) return false
        val history = view.copyBackForwardList()
        val current = history.currentItem?.url ?: return false
        val previous = history.getItemAtIndex(history.currentIndex - 1)?.url ?: return false
        if (Config.originOf(current) == null || Config.originOf(current) != Config.originOf(previous)) return false
        view.goBack()
        return true
    }

    fun focus() {
        webView?.requestFocus()
    }

    fun onResume() {
        paused = false
        // Timers may have been throttled while hidden; give the page a fresh grace period.
        val now = SystemClock.elapsedRealtime()
        if (lastHeartbeat != 0L) lastHeartbeat = now
        loadStartedAt = now
        disconnectedSince = 0
        probeSentAt = 0
        webView?.onResume()
    }

    fun onPause() {
        paused = true
        webView?.onPause()
    }

    fun destroy() {
        main.removeCallbacks(retryRunnable)
        main.removeCallbacks(recreateRunnable)
        main.removeCallbacks(reloadRunnable)
        main.removeCallbacks(watchdogRunnable)
        networkCallback?.let { cb ->
            runCatching { connectivity().unregisterNetworkCallback(cb) }
        }
        networkCallback = null
        destroyWebView()
    }

    // ---- WebView lifecycle --------------------------------------------------------------

    private fun create() {
        destroyWebView()
        ready = false
        failed = false
        lastHeartbeat = 0
        disconnectedSince = 0
        probeSentAt = 0
        forcedAuth.clear()
        val view = try {
            WebView(activity)
        } catch (e: Exception) {
            // WebView missing or mid-update.
            Log.e(TAG, "WebView unavailable", e)
            callbacks.onStatus(activity.getString(R.string.status_no_webview))
            main.postDelayed({ create(); navigate(lastTarget) }, 30_000)
            return
        }
        configure(view)
        installBridge(view)
        installZoom(view)
        container.addView(view, FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT))
        webView = view
        signInFlow.attach(view)
        view.requestFocus()
    }

    private fun destroyWebView() {
        signInFlow.detach()
        main.removeCallbacks(retryRunnable)
        scriptHandler?.let { runCatching { it.remove() } }
        scriptHandler = null
        zoomHandler?.let { runCatching { it.remove() } }
        zoomHandler = null
        webView?.let {
            container.removeView(it)
            it.stopLoading()
            it.destroy()
        }
        webView = null
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun configure(view: WebView) {
        view.setBackgroundColor(ContextCompat.getColor(activity, R.color.background))
        view.isVerticalScrollBarEnabled = false
        view.isHorizontalScrollBarEnabled = false
        view.isFocusable = true
        view.isFocusableInTouchMode = true
        view.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            mediaPlaybackRequiresUserGesture = false // camera cards autoplay
            mixedContentMode = WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE
            useWideViewPort = true
            loadWithOverviewMode = true
            textZoom = 100
            userAgentString = "$userAgentString PleatView/${BuildConfig.VERSION_NAME}"
        }
        view.webViewClient = Client()
        view.webChromeClient = Chrome()
    }

    /**
     * The bridge is only exposed to the Home Assistant origin. On current WebViews it is
     * a WebMessageListener plus a document-start script scoped to that origin; older
     * WebViews fall back to JavaScript interfaces that check the page origin per call.
     */
    @SuppressLint("RequiresFeature") // the reply proxy only exists when WEB_MESSAGE_LISTENER does
    private fun installBridge(view: WebView) {
        val origin = config.haOrigin ?: return
        listenerMode = false
        if (WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER) &&
            WebViewFeature.isFeatureSupported(WebViewFeature.DOCUMENT_START_SCRIPT)
        ) {
            listenerMode = true
            WebViewCompat.addWebMessageListener(view, "pleatviewBridge", setOf(origin)) { _, message, _, isMainFrame, reply ->
                onBridgeMessage(message.data ?: return@addWebMessageListener, isMainFrame) { reply.postMessage(it) }
            }
            scriptHandler = WebViewCompat.addDocumentStartJavaScript(view, script(), setOf(origin))
        } else {
            if (config.haToken.isNotEmpty()) view.addJavascriptInterface(LegacyAuthBridge(), "externalApp")
            view.addJavascriptInterface(LegacyBridge(), "pleatviewNative")
        }
    }

    private fun script(): String {
        val template = scriptTemplate
            ?: activity.assets.open("pleatview.js").bufferedReader().use { it.readText() }.also { scriptTemplate = it }
        val pageConfig = JSONObject()
            .put("externalAuth", config.haToken.isNotEmpty())
            .put("kiosk", config.kiosk)
            .put("viewportWidth", viewportWidth())
        return template.replace("__PLEATVIEW_CONFIG__", pageConfig.toString())
    }

    /** Web pages shown as dashboards (not Home Assistant) get the same zoom. */
    private fun installZoom(view: WebView) {
        if (viewportWidth() == 0 || !WebViewFeature.isFeatureSupported(WebViewFeature.DOCUMENT_START_SCRIPT)) return
        zoomHandler = WebViewCompat.addDocumentStartJavaScript(view, zoomScript(), setOf("*"))
    }

    private fun zoomScript(): String {
        val template = zoomTemplate
            ?: activity.assets.open("zoom.js").bufferedReader().use { it.readText() }.also { zoomTemplate = it }
        return template
            .replace("__PLEATVIEW_WIDTH__", viewportWidth().toString())
            .replace("__PLEATVIEW_HA_ORIGIN__", JSONObject.quote(config.haOrigin ?: ""))
    }

    /** CSS width that makes the dashboard render at [Config.zoom] percent. 0 keeps the page default. */
    private fun viewportWidth(): Int {
        if (config.zoom == 100) return 0
        val metrics = activity.resources.displayMetrics
        val widthPx = container.width.takeIf { it > 0 } ?: metrics.widthPixels
        return (widthPx / metrics.density * 100f / config.zoom).roundToInt()
    }

    private fun load(url: String) {
        val view = webView ?: return
        main.removeCallbacks(retryRunnable)
        signInFlow.onNavigate()
        currentUrl = url
        reportedPath = null
        failed = false
        ready = false
        lastHeartbeat = 0
        loadStartedAt = SystemClock.elapsedRealtime()
        clearHistoryOnLoad = true
        view.loadUrl(url)
        publishLocation(null)
    }

    // ---- Bridge -----------------------------------------------------------------------

    private fun onBridgeMessage(raw: String, isMainFrame: Boolean, reply: (String) -> Unit) {
        val message = runCatching { JSONObject(raw) }.getOrNull() ?: return
        when (message.optString("type")) {
            "getExternalAuth" -> {
                val payload = message.optJSONObject("payload") ?: JSONObject()
                val callback = payload.optString("callback")
                if (CALLBACK_NAME.matches(callback)) reply(authReply(callback, payload.optBoolean("force")).toString())
            }
            "revokeExternalAuth" -> {
                val callback = message.optJSONObject("payload")?.optString("callback").orEmpty()
                if (CALLBACK_NAME.matches(callback)) {
                    reply(JSONObject().put("type", "auth").put("callback", callback).put("success", true).toString())
                }
            }
            "status" -> if (isMainFrame) onHeartbeat(message)
        }
    }

    /** Answer to the frontend's token request. A forced refresh means HA rejected the token. */
    private fun authReply(callback: String, force: Boolean): JSONObject {
        val reply = JSONObject().put("type", "auth").put("callback", callback)
        val now = SystemClock.elapsedRealtime()
        if (force) {
            forcedAuth.addLast(now)
            while (forcedAuth.isNotEmpty() && now - forcedAuth.first() > 60_000) forcedAuth.removeFirst()
        }
        if (config.haToken.isEmpty() || forcedAuth.size > 1) {
            Log.w(TAG, "Home Assistant rejected the access token")
            hub.update { authError = true }
            callbacks.onStatus(activity.getString(R.string.status_auth_failed))
            return reply.put("success", false).put("data", JSONObject().put("message", "auth_invalid"))
        }
        return reply.put("success", true).put(
            "data", JSONObject().put("access_token", config.haToken).put("expires_in", TOKEN_LIFETIME_SECONDS),
        )
    }

    private fun onHeartbeat(message: JSONObject) {
        // A last heartbeat from a Home Assistant page we've already left must not report its path.
        if (!config.isHaUrl(currentUrl)) return
        val now = SystemClock.elapsedRealtime()
        lastHeartbeat = now
        if (message.optBoolean("ready")) ready = true
        val connected = if (message.isNull("connected")) null else message.optBoolean("connected")
        disconnectedSince = when {
            connected != false -> 0L
            disconnectedSince == 0L -> now
            else -> disconnectedSince
        }
        reportedPath = message.optString("path").ifEmpty { null }
        publishLocation(reportedPath)
        hub.update {
            haConnected = connected
            if (connected == true) {
                authError = false
                lastError = null
            }
        }
        if (connected == true) callbacks.onStatus(null)
    }

    private inner class LegacyAuthBridge {
        @JavascriptInterface
        fun getExternalAuth(payload: String?) {
            main.post { legacyAuth(payload, revoke = false) }
        }

        @JavascriptInterface
        fun revokeExternalAuth(payload: String?) {
            main.post { legacyAuth(payload, revoke = true) }
        }
    }

    private inner class LegacyBridge {
        @JavascriptInterface
        fun postMessage(message: String?) {
            main.post {
                if (message != null && config.isHaUrl(webView?.url)) {
                    onBridgeMessage(message, isMainFrame = true) { reply ->
                        webView?.evaluateJavascript("window.__pleatview&&window.__pleatview.receive($reply)", null)
                    }
                }
            }
        }
    }

    private fun legacyAuth(raw: String?, revoke: Boolean) {
        if (!config.isHaUrl(webView?.url)) return
        val payload = runCatching { JSONObject(raw ?: "{}") }.getOrDefault(JSONObject())
        val callback = payload.optString("callback")
        if (!CALLBACK_NAME.matches(callback)) return
        val reply = if (revoke) {
            JSONObject().put("success", true).put("data", JSONObject.NULL)
        } else {
            authReply(callback, payload.optBoolean("force"))
        }
        val js = "(function(){var f=window[${JSONObject.quote(callback)}];" +
            "if(typeof f==='function')f(${reply.getBoolean("success")},${reply.opt("data")});})()"
        webView?.evaluateJavascript(js, null)
    }

    // ---- Health ---------------------------------------------------------------------

    private fun watchdog() {
        val view = webView ?: return
        if (paused) return
        val now = SystemClock.elapsedRealtime()
        // A page can freeze the renderer: after a Wi-Fi drop one did on a Fire TV, and every
        // later load went into the same frozen renderer. Only a new one gets out of that.
        if (probeSentAt != 0L && now - probeSentAt > UNRESPONSIVE_MS) {
            Log.w(TAG, "Dashboard renderer stopped responding; starting a new one")
            hub.update { lastError = "The page stopped responding, so PleatView started it again" }
            if (Build.VERSION.SDK_INT >= 29) view.webViewRenderProcess?.terminate()
            recreate()
            return
        }
        if (probeSentAt == 0L) {
            probeSentAt = now
            view.evaluateJavascript("0") { if (view === webView) probeSentAt = 0 }
        }
        if (failed || !config.isHaUrl(view.url)) return
        val silentSince = if (lastHeartbeat != 0L) lastHeartbeat else loadStartedAt
        when {
            silentSince != 0L && now - silentSince > STALL_MS -> {
                Log.w(TAG, "Dashboard stopped responding; reloading")
                reload()
            }
            disconnectedSince != 0L && now - disconnectedSince > DISCONNECTED_RELOAD_MS -> {
                Log.w(TAG, "Lost the Home Assistant connection for too long; reloading")
                disconnectedSince = 0
                reload()
            }
        }
    }

    /** A new WebView on the same page, once the old renderer has died or been given up on. */
    private fun recreate() {
        recreateTarget = currentUrl
        destroyWebView()
        main.removeCallbacks(recreateRunnable)
        main.postDelayed(recreateRunnable, 1_000)
    }

    private fun onLoadFailed(reason: CharSequence?) {
        if (failed) return
        failed = true
        ready = false
        val delay = RETRY_DELAYS_SECONDS[minOf(retryAttempt, RETRY_DELAYS_SECONDS.size - 1)]
        retryAttempt++
        Log.w(TAG, "Dashboard failed to load: $reason; retrying in ${delay}s")
        callbacks.onStatus(activity.getString(R.string.status_unreachable, Config.originOf(config.haUrl) ?: config.haUrl, delay))
        hub.update {
            lastError = "Can't load ${currentUrl.orEmpty()}: $reason"
            haConnected = false
        }
        webView?.loadUrl(BLANK) // hide the browser's error page
        main.removeCallbacks(retryRunnable)
        main.postDelayed(retryRunnable, delay * 1000L)
    }

    private fun scheduleReload() {
        main.removeCallbacks(reloadRunnable)
        if (config.reloadMinutes > 0) main.postDelayed(reloadRunnable, config.reloadMinutes * 60_000L)
    }

    private fun registerNetworkCallback() {
        if (networkCallback != null) return
        val callback = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                main.post {
                    if (failed) {
                        retryAttempt = 0
                        main.removeCallbacks(retryRunnable)
                        main.postDelayed(retryRunnable, 1_000)
                    }
                }
            }
        }
        try {
            connectivity().registerDefaultNetworkCallback(callback)
            networkCallback = callback
        } catch (e: Exception) {
            Log.w(TAG, "Could not watch network changes", e)
        }
    }

    private fun connectivity() = activity.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager

    private fun publishLocation(path: String?) {
        val url = currentUrl
        hub.update {
            view = StateModel.VIEW_DASHBOARD
            this.url = url
            dashboard = when {
                path != null -> path
                url != null && config.isHaUrl(url) -> Config.pathOf(url)
                // A web page shown as a dashboard stays that dashboard, wherever the site navigates.
                Config.isHttpUrl(lastTarget) && !config.isHaUrl(lastTarget) -> lastTarget
                else -> null
            }
        }
    }

    private inner class Client : WebViewClient() {
        override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
            val scheme = request.url.scheme?.lowercase()
            return scheme != "http" && scheme != "https" // no intent://, tel:, market: … on a TV
        }

        override fun onPageFinished(view: WebView, url: String?) {
            if (view !== webView || url == BLANK) return
            signInFlow.onPageLoaded()
            signInFlow.onPageShown(url, finished = true)
            if (!listenerMode && config.isHaUrl(url)) {
                view.evaluateJavascript(script(), null)
            } else if (zoomHandler == null && viewportWidth() != 0 && !config.isHaUrl(url)) {
                view.evaluateJavascript(zoomScript(), null) // older WebViews: no document-start scripts
            }
            if (clearHistoryOnLoad) {
                view.clearHistory()
                clearHistoryOnLoad = false
            }
            if (!failed) {
                retryAttempt = 0
                callbacks.onStatus(null)
            }
        }

        override fun doUpdateVisitedHistory(view: WebView, url: String?, isReload: Boolean) {
            if (view !== webView || url == BLANK) return
            // A new dashboard starts a new history as soon as it's committed, so Back can't
            // return to the previous one even before this one finishes loading.
            if (clearHistoryOnLoad) view.clearHistory()
            // Single-page apps change routes without loading a page, for example to their login screen.
            signInFlow.onPageShown(url, finished = false)
        }

        override fun onPageStarted(view: WebView, url: String?, favicon: Bitmap?) {
            if (view === webView && url != BLANK) signInFlow.onPageStarted()
        }

        override fun onReceivedHttpAuthRequest(view: WebView, handler: HttpAuthHandler, host: String, realm: String) {
            if (view === webView) signInFlow.onPrompt(handler, host, realm) else handler.cancel()
        }

        override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
            if (view === webView && request.isForMainFrame) onLoadFailed(error.description)
        }

        override fun onReceivedHttpError(view: WebView, request: WebResourceRequest, response: WebResourceResponse) {
            if (view === webView && request.isForMainFrame && response.statusCode >= 500) {
                onLoadFailed("HTTP ${response.statusCode}")
            }
        }

        override fun onRenderProcessGone(view: WebView, detail: RenderProcessGoneDetail): Boolean {
            if (view === webView) {
                Log.w(TAG, "Dashboard renderer gone; recreating")
                recreate()
            }
            return true
        }
    }

    private class Chrome : WebChromeClient() {
        // Without this, Android WebView shows a grey "play" poster before videos start.
        override fun getDefaultVideoPoster(): Bitmap = createBitmap(1, 1)

        override fun onPermissionRequest(request: PermissionRequest) = request.deny()

        override fun onConsoleMessage(message: ConsoleMessage): Boolean {
            if (BuildConfig.DEBUG) Log.d(TAG, "[web] ${message.message()} (${message.sourceId()}:${message.lineNumber()})")
            return true
        }
    }

    companion object {
        private const val TAG = "PleatView"
        private const val BLANK = "about:blank"
        private const val TOKEN_LIFETIME_SECONDS = 1800
        private const val WATCHDOG_INTERVAL_MS = 30_000L
        private const val STALL_MS = 120_000L
        private const val UNRESPONSIVE_MS = 45_000L
        private const val DISCONNECTED_RELOAD_MS = 5 * 60_000L
        private val RETRY_DELAYS_SECONDS = intArrayOf(3, 5, 10, 20, 30, 60)
        private val CALLBACK_NAME = Regex("^[A-Za-z_$][A-Za-z0-9_$]{0,63}$")
    }
}
