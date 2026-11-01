package com.hustlesman.pleatview

import android.net.Uri
import android.os.Handler
import android.os.SystemClock
import android.webkit.HttpAuthHandler
import android.webkit.WebView

/**
 * Signs dashboard pages in. Password prompts (HTTP basic auth) and login forms get the
 * sign-in saved on this TV for that site; without one, or when it stops working, the TV
 * asks through [onRequest]. A sign-in the user enters is saved only once the site accepts it.
 *
 * Main thread only.
 */
class SignInFlow(
    private val signIns: SignIns,
    private val main: Handler,
    private val onRequest: (SignInRequest?) -> Unit,
) {
    var config = Config()

    /** The dashboard (path or URL) the TV was asked to show. */
    var target: String? = null
    private var view: WebView? = null

    private var request: SignInRequest? = null
    private var promptHandler: HttpAuthHandler? = null

    /** Password prompts answered with a saved sign-in on this page: a second prompt means it was rejected. */
    private val promptsTried = mutableSetOf<Pair<String, String>>()

    /** A sign-in just sent to a site, waiting to see whether the site accepts it. */
    private var trying: Attempt? = null

    private data class Attempt(
        val signIn: SignIn,
        val save: Boolean,
        /** It was the saved sign-in, rather than one just entered. */
        val saved: Boolean,
        val at: Long,
        /** A new page or route has shown since it was sent. */
        val navigated: Boolean = false,
    )

    /** The main frame is loading a page: until it finishes, a missing form proves nothing. */
    private var loading = false

    /** When a saved sign-in was last rejected, per origin: until a while later, ask instead. */
    private val rejected = mutableMapOf<String, Long>()

    /** "Not now" on a login form: don't ask again until the next page. */
    private var dismissed: String? = null

    /** "Not now" on password prompts (also asked for a page's images): don't ask again on this page. */
    private val dismissedPrompts = mutableSetOf<Pair<String, String>>()

    /** Each form search belongs to one page; a newer page stops older searches. */
    private var search = 0

    fun attach(view: WebView) {
        this.view = view
    }

    fun detach() {
        promptHandler?.cancel()
        promptHandler = null
        trying = null
        search++
        show(null)
        view = null
    }

    /** A new page starts loading in the dashboard. */
    fun onNavigate() {
        search++
        promptsTried.clear()
        dismissedPrompts.clear()
        dismissed = null
        promptHandler?.cancel()
        promptHandler = null
        if (request != null) show(null)
    }

    // ---- Password prompts ---------------------------------------------------------

    fun onPrompt(handler: HttpAuthHandler, host: String, realm: String) {
        val key = host.lowercase() to realm
        if (key in dismissedPrompts) return handler.cancel()
        val dashboard = dashboardOrigin()
        val saved = signIns.find(SignIn.Kind.PROMPT, host, realm, dashboard)
        if (saved != null && key !in promptsTried && handler.useHttpAuthUsernamePassword()) {
            promptsTried += key
            // A sign-in from before 1.0.5 belongs to this dashboard once it works here.
            if (saved.dashboard.isEmpty() && dashboard.isNotEmpty()) {
                trying = Attempt(saved.copy(dashboard = dashboard), save = true, saved = true, SystemClock.elapsedRealtime())
            }
            handler.proceed(saved.username, saved.password)
            return
        }
        // A second prompt after a sign-in means the site rejected it.
        val attempt = trying
        val rejected = key in promptsTried ||
            (attempt != null && attempt.signIn.kind == SignIn.Kind.PROMPT && attempt.signIn.matches(SignIn.Kind.PROMPT, host, realm, dashboard))
        trying = null
        promptHandler?.cancel()
        promptHandler = handler
        show(SignInRequest(SignIn.Kind.PROMPT, host, realm, titleFor(host), failed = rejected))
    }

    /** The origin of the dashboard on screen, which a password-prompt sign-in is kept for. */
    private fun dashboardOrigin(): String {
        val target = target ?: return ""
        return (if (Config.isHttpUrl(target)) Config.originOf(target) else config.haOrigin).orEmpty()
    }

    /** The main frame finished loading: a pending password sign-in worked. */
    fun onPageLoaded() {
        val attempt = trying ?: return
        if (attempt.signIn.kind != SignIn.Kind.PROMPT) return
        trying = null
        if (attempt.save) signIns.save(attempt.signIn)
    }

    // ---- Login forms --------------------------------------------------------------

    fun onPageStarted() {
        loading = true
    }

    /** A page (finished loading) or a single-page app's new route is showing: look for a login form. */
    fun onPageShown(url: String?, finished: Boolean) {
        if (finished) loading = false
        val origin = url?.let(Config::originOf) ?: return
        trying?.let { if (it.signIn.kind == SignIn.Kind.FORM && !it.navigated) trying = it.copy(navigated = true) }
        val token = ++search
        if (!Config.isHttpUrl(url) || config.isHaUrl(url)) {
            // Home Assistant: no login form here.
            formSent()?.let(::accepted)
            if (request?.kind == SignIn.Kind.FORM) show(null)
            return
        }
        lookForForm(origin, token, attempt = 0)
    }

    private fun lookForForm(origin: String, token: Int, attempt: Int) {
        val view = view ?: return
        view.evaluateJavascript(SignIns.DETECT_SCRIPT) { result ->
            if (token != search) return@evaluateJavascript
            val form = result == "\"form\""
            val sent = formSent()
            val now = SystemClock.elapsedRealtime()
            when {
                // The form is gone from a loaded page: the site accepted the sign-in.
                sent != null && !form && !loading -> accepted(sent)
                // The form is back on a new page, or still there well after sending: rejected.
                sent != null && form && (sent.navigated || now - sent.at > SAME_PAGE_VERDICT_MS) -> rejected(sent, origin)
                sent == null && form -> onForm(origin)
                attempt < FORM_SEARCHES -> main.postDelayed({ lookForForm(origin, token, attempt + 1) }, FORM_SEARCH_INTERVAL_MS)
                // No form appeared: a card asking for one isn't needed any more.
                request?.kind == SignIn.Kind.FORM -> show(null)
            }
        }
    }

    /** The form sign-in waiting for the site's answer, unless it was sent too long ago to tell. */
    private fun formSent(): Attempt? {
        val sent = trying?.takeIf { it.signIn.kind == SignIn.Kind.FORM } ?: return null
        if (SystemClock.elapsedRealtime() - sent.at < VERDICT_MS) return sent
        trying = null
        return null
    }

    private fun accepted(sent: Attempt) {
        trying = null
        if (sent.save) {
            signIns.save(sent.signIn)
            rejected.remove(sent.signIn.site)
        }
        if (request?.kind == SignIn.Kind.FORM) show(null)
    }

    private fun rejected(sent: Attempt, origin: String) {
        trying = null
        if (sent.saved) rejected[origin] = SystemClock.elapsedRealtime()
        show(SignInRequest(SignIn.Kind.FORM, origin, "", titleFor(origin, view?.title), failed = true))
    }

    /** A login form with no sign-in on its way: use the saved one, or ask. */
    private fun onForm(origin: String) {
        val saved = signIns.find(SignIn.Kind.FORM, origin)
        val rejectedAt = rejected[origin]
        if (saved != null && (rejectedAt == null || SystemClock.elapsedRealtime() - rejectedAt > REJECTED_PAUSE_MS)) {
            fill(saved, save = false, saved = true)
        } else if (dismissed != origin) {
            show(SignInRequest(SignIn.Kind.FORM, origin, "", titleFor(origin, view?.title), failed = saved != null))
        }
    }

    private fun fill(signIn: SignIn, save: Boolean, saved: Boolean) {
        val view = view ?: return
        trying = Attempt(signIn, save, saved, SystemClock.elapsedRealtime())
        view.evaluateJavascript(SignIns.fillScript(signIn.site, signIn.username, signIn.password), null)
        // A single-page app may sign in without loading a page: watch this one too.
        val token = ++search
        main.postDelayed({ lookForForm(signIn.site, token, attempt = 0) }, FORM_SEARCH_INTERVAL_MS)
    }

    // ---- From the TV or a phone ----------------------------------------------------

    /** The user entered a username and password for the current request. */
    fun apply(username: String, password: String, remember: Boolean) {
        val current = request ?: return
        val dashboard = if (current.kind == SignIn.Kind.PROMPT) dashboardOrigin() else ""
        val signIn = SignIn(current.kind, current.site, current.realm, username, password, dashboard)
        show(null)
        when (current.kind) {
            SignIn.Kind.PROMPT -> {
                val handler = promptHandler ?: return
                promptHandler = null
                trying = Attempt(signIn, remember, saved = false, SystemClock.elapsedRealtime())
                handler.proceed(username, password)
            }
            SignIn.Kind.FORM -> fill(signIn, save = remember, saved = false)
        }
    }

    /** "Not now": a password prompt is cancelled; a login form stays for the remote. */
    fun dismiss() {
        val current = request ?: return
        if (current.kind == SignIn.Kind.PROMPT) {
            dismissedPrompts += current.site.lowercase() to current.realm
            promptHandler?.cancel()
            promptHandler = null
        } else {
            dismissed = current.site
        }
        show(null)
    }

    val pending: SignInRequest? get() = request

    private fun show(next: SignInRequest?) {
        if (next == request) return
        request = next
        onRequest(next)
    }

    /**
     * What to call a site on the TV and the phone: the name of a dashboard on it, else
     * "Home Assistant", else the web-page dashboard that led there (a sign-in page on another
     * site, such as single sign-on), else the site's own name from [pageTitle], else its host.
     */
    private fun titleFor(site: String, pageTitle: String? = null): String {
        val host = (if ("://" in site) Uri.parse(site).host else site.substringBefore(':')) ?: site
        fun onHost(url: String) = Uri.parse(url).host.equals(host, ignoreCase = true)
        // The dashboard being opened, then another one on that site: a password prompt only
        // names its host, and add-ons often share Home Assistant's host on another port.
        config.dashboards.firstOrNull { Config.isHttpUrl(it.path) && it.path == target && onHost(it.path) }
            ?.let { return it.title }
        config.dashboards.firstOrNull { Config.isHttpUrl(it.path) && onHost(it.path) }?.let { return it.title }
        if (config.haOrigin?.let { onHost(it) } == true) return "Home Assistant"
        return SignIns.siteName(pageTitle) ?: host
    }

    private companion object {
        const val FORM_SEARCHES = 8
        const val FORM_SEARCH_INTERVAL_MS = 1_500L
        const val VERDICT_MS = 30_000L
        const val SAME_PAGE_VERDICT_MS = 8_000L
        const val REJECTED_PAUSE_MS = 10 * 60_000L
    }
}
