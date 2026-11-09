package com.hustlesman.pleatview

import android.app.Activity
import android.app.AlertDialog
import android.app.KeyguardManager
import android.content.ActivityNotFoundException
import android.content.Intent
import android.content.res.Configuration
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.text.InputType
import android.view.Gravity
import android.view.KeyEvent
import android.view.MotionEvent
import android.view.View
import android.view.WindowManager
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import android.window.OnBackInvokedDispatcher
import androidx.core.net.toUri
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat

class MainActivity : Activity(), Hub.Display, DashboardController.Callbacks, MediaController.Callbacks, DemoTour.Stage {
    private lateinit var app: PleatApp
    private lateinit var dashboard: DashboardController
    private lateinit var media: MediaController
    private lateinit var webOverlay: WebOverlay
    private lateinit var toasts: Toasts
    private lateinit var statusBanner: TextView
    private lateinit var setupScreen: View
    private lateinit var menuOverlay: View
    private lateinit var menuItems: LinearLayout
    private lateinit var curtain: View
    private lateinit var signInCard: View
    private lateinit var touchMenu: View
    private lateinit var caption: View
    private lateinit var captionText: TextView

    private val main = Handler(Looper.getMainLooper())
    private var visible = false
    private var standby = false
    private var started = false
    private var lastInput = 0L

    /** True after remote-control navigation; the idle timer then returns to the home dashboard. */
    private var userBrowsing = false

    /** The demo, while it runs instead of Home Assistant's dashboards. */
    private var demo: DemoTour? = null

    private val config: Config get() = demo?.config ?: app.settings.config
    private val menuVisible: Boolean get() = menuOverlay.visibility == View.VISIBLE
    private val signInVisible: Boolean get() = signInCard.visibility == View.VISIBLE

    private val revertRunnable = Runnable { dashboard.navigate(config.homePath) }
    private val hideTouchMenu = Runnable {
        touchMenu.animate().alpha(0f).withEndAction { touchMenu.visibility = View.GONE }
    }
    private val hideCaption = Runnable {
        caption.animate().alpha(0f).withEndAction { caption.visibility = View.GONE }
    }
    private val setupRefresh = object : Runnable {
        override fun run() {
            refreshSetup()
            main.postDelayed(this, 10_000)
        }
    }
    private val rotateRunnable = object : Runnable {
        override fun run() {
            val seconds = config.rotateSeconds
            if (seconds <= 0) return
            val idle = SystemClock.elapsedRealtime() - lastInput > ROTATE_INPUT_GRACE_MS
            if (visible && !standby && idle && !menuVisible && !signInVisible && !media.isFullscreen && !webOverlay.isFullscreen) {
                cycleDashboard(+1)
            }
            main.postDelayed(this, seconds * 1000L)
        }
    }
    private val idleRunnable = object : Runnable {
        override fun run() {
            val minutes = config.returnHomeMinutes
            val idleMs = SystemClock.elapsedRealtime() - lastInput
            if (minutes > 0 && userBrowsing && idleMs > minutes * 60_000L && !media.isActive && webOverlay.current == null) {
                userBrowsing = false
                closeMenu()
                dashboard.navigate(config.homePath)
            }
            main.postDelayed(this, 30_000)
        }
    }

    override val isVisible: Boolean get() = visible

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        app = PleatApp.from(this)
        setContentView(R.layout.activity_main)
        hideSystemBars()
        applyLockScreen()

        statusBanner = findViewById(R.id.status_banner)
        setupScreen = findViewById(R.id.setup_screen)
        menuOverlay = findViewById(R.id.menu_overlay)
        menuItems = findViewById(R.id.menu_items)
        curtain = findViewById(R.id.standby_curtain)
        signInCard = findViewById(R.id.signin_card)
        findViewById<Button>(R.id.signin_enter).setOnClickListener { showSignInDialog() }
        findViewById<Button>(R.id.signin_not_now).setOnClickListener { dashboard.dismissSignIn() }
        toasts = Toasts(findViewById(R.id.toast_layer))
        dashboard = DashboardController(this, findViewById(R.id.dashboard_container), app.hub, this)
        media = MediaController(
            this, findViewById(R.id.media_container), findViewById(R.id.player_view),
            NowPlaying(findViewById(R.id.now_playing)), app.hub, this,
        )
        webOverlay = WebOverlay(this, findViewById(R.id.web_overlay_container), app.hub) { onLayerClosed() }
        menuOverlay.setOnClickListener { closeMenu() }
        touchMenu = findViewById(R.id.touch_menu)
        touchMenu.setOnClickListener {
            hideTouchMenu.run()
            openMenu()
        }
        caption = findViewById(R.id.demo_caption)
        captionText = findViewById(R.id.demo_caption_text)
        findViewById<Button>(R.id.setup_demo).setOnClickListener { startDemo() }
        fitLayouts()

        if (Build.VERSION.SDK_INT >= 33) {
            onBackInvokedDispatcher.registerOnBackInvokedCallback(OnBackInvokedDispatcher.PRIORITY_DEFAULT) { handleBack() }
        }

        app.hub.display = this
        ControlService.start(this)
        applyConfig(null, config)
        main.postDelayed(idleRunnable, 30_000)
        handleIntent(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handleIntent(intent)
        drainPending()
    }

    override fun onStart() {
        super.onStart()
        visible = true
        ControlService.start(this)
        dashboard.onResume()
        applyKeepScreenOn()
        val overlays = app.hub.canDrawOverlays()
        app.hub.update {
            foreground = true
            overlayPermission = overlays
        }
        app.hub.uiShown()
        drainPending()
        demo?.takeIf { !it.finished }?.start() // the tour starts again after another app
    }

    override fun onStop() {
        visible = false
        demo?.stop()
        media.stop()
        webOverlay.close()
        closeMenu()
        dashboard.onPause()
        app.hub.update { foreground = false }
        app.hub.uiHidden()
        applyLockScreen()
        super.onStop()
    }

    override fun onDestroy() {
        if (app.hub.display === this) app.hub.display = null
        demo?.stop() // its steps run on their own handler
        main.removeCallbacksAndMessages(null)
        media.release()
        webOverlay.close()
        dashboard.destroy()
        super.onDestroy()
    }

    // ---- Hub.Display ------------------------------------------------------------------

    override fun handle(command: Command) {
        if (standby && command.needsDisplay) wake()
        when (command) {
            is Command.ShowDashboard -> {
                closeFullscreenLayers()
                closeMenu()
                userBrowsing = false
                dashboard.navigate(command.path)
                main.removeCallbacks(revertRunnable)
                command.duration?.let { main.postDelayed(revertRunnable, it * 1000L) }
                // Home Assistant chose this dashboard: rotation waits until a timed one has
                // gone back, then a full period (issue #32).
                restartRotation(afterMs = (command.duration ?: 0) * 1000L)
            }
            is Command.ShowWeb -> {
                if (command.layout.mode == Command.Mode.FULLSCREEN && media.isFullscreen) media.stop()
                webOverlay.show(command)
            }
            is Command.PlayMedia -> {
                if (!command.announce && command.layout.mode == Command.Mode.FULLSCREEN && webOverlay.isFullscreen) {
                    webOverlay.close()
                }
                media.play(command)
            }
            is Command.Notify -> toasts.show(command)
            is Command.Seek -> media.seek(command.position)
            is Command.SetVolume -> media.setVolume(command.level)
            is Command.Mute -> media.setMuted(command.muted)
            Command.Home -> goHome()
            Command.Reload -> dashboard.reload()
            is Command.ForgetSignIns -> app.signIns.forget(command.site) // normally handled by the Hub
            Command.Play -> media.resume()
            Command.Pause -> media.pause()
            Command.Stop -> {
                media.stop()
                webOverlay.close()
            }
            Command.TurnOn -> wake()
            Command.TurnOff -> enterStandby()
            Command.NextDashboard -> cycleDashboard(+1)
            Command.PreviousDashboard -> cycleDashboard(-1)
        }
    }

    override fun onConfigChanged(old: Config, new: Config) = applyConfig(old, new)

    override fun caption(text: String?, seconds: Int, top: Boolean) {
        main.removeCallbacks(hideCaption)
        caption.animate().cancel()
        if (text == null) {
            caption.visibility = View.GONE
            return
        }
        captionText.text = text
        caption.layoutParams = (caption.layoutParams as FrameLayout.LayoutParams).apply {
            gravity = (if (top) Gravity.TOP else Gravity.BOTTOM) or Gravity.START
        }
        caption.alpha = 0f
        caption.visibility = View.VISIBLE
        caption.animate().alpha(1f).setDuration(200)
        main.postDelayed(hideCaption, seconds * 1000L)
    }

    override fun beforeShow() = applyLockScreen()

    /**
     * Whether Home Assistant may wake the screen and show PleatView over the lock screen: on
     * TVs and on tablets whose lock is None or Swipe, yes. With a PIN, pattern or password it
     * mustn't, or the dashboard's controls would work without unlocking. Checked again before
     * each time PleatView comes to the front, in case a lock was set meanwhile.
     */
    private fun applyLockScreen() {
        if (Build.VERSION.SDK_INT < 27) return
        val open = !getSystemService(KeyguardManager::class.java).isDeviceSecure
        setShowWhenLocked(open)
        setTurnScreenOn(open)
    }

    override fun onPairingChanged() {
        if (setupScreen.visibility == View.VISIBLE) refreshSetup()
        dashboard.signInRequest?.let(::showSignInCard) // the code on the card carries the PIN
        if (menuVisible) openMenu()
    }

    // ---- Callbacks from controllers -------------------------------------------------

    override fun onStatus(message: String?) {
        statusBanner.text = message
        statusBanner.visibility = if (message == null) View.GONE else View.VISIBLE
    }

    override fun onSignIn(request: SignInRequest?) {
        if (request == null) {
            val hadFocus = signInCard.hasFocus()
            signInCard.visibility = View.GONE
            if (hadFocus) dashboard.focus()
        } else {
            showSignInCard(request)
        }
    }

    override fun applySignIn(username: String, password: String, remember: Boolean) =
        dashboard.applySignIn(username, password, remember)

    private fun showSignInCard(request: SignInRequest) {
        findViewById<TextView>(R.id.signin_title).text = getString(R.string.signin_title, request.title)
        findViewById<View>(R.id.signin_failed).visibility = if (request.failed) View.VISIBLE else View.GONE
        val qr = findViewById<ImageView>(R.id.signin_qr)
        val ip = NetUtil.localIpv4()
        if (ip != null) {
            val url = SetupCode.signInUrl(ip, app.settings.port, app.pairing.pin, request.title, request.address)
            if (qr.contentDescription != url) {
                qr.setImageBitmap(SetupCode.bitmap(url, (220 * resources.displayMetrics.density).toInt()))
                qr.contentDescription = url
            }
            qr.visibility = View.VISIBLE
        } else {
            qr.visibility = View.INVISIBLE
        }
        val wasVisible = signInVisible
        signInCard.visibility = View.VISIBLE
        if (!wasVisible && !menuVisible) findViewById<Button>(R.id.signin_enter).requestFocus()
    }

    /** Typing on the TV, with the remote's keyboard or the TV maker's phone app. */
    private fun showSignInDialog() {
        val request = dashboard.signInRequest ?: return
        val pad = (20 * resources.displayMetrics.density).toInt()
        val username = EditText(this).apply {
            hint = getString(R.string.signin_username)
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS
            setSingleLine()
        }
        val password = EditText(this).apply {
            hint = getString(R.string.signin_password)
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
            setSingleLine()
        }
        val remember = CheckBox(this).apply {
            text = getString(R.string.signin_remember)
            isChecked = true
        }
        val form = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad / 2, pad, 0)
            addView(username)
            addView(password)
            addView(remember)
        }
        AlertDialog.Builder(this, android.R.style.Theme_DeviceDefault_Dialog_Alert)
            .setTitle(getString(R.string.signin_title, request.title))
            .setView(form)
            .setPositiveButton(R.string.signin_submit) { _, _ ->
                if (password.text.isNotEmpty()) {
                    dashboard.applySignIn(username.text.toString().trim(), password.text.toString(), remember.isChecked)
                }
            }
            .setNegativeButton(android.R.string.cancel, null)
            .show()
        username.requestFocus()
    }

    override fun onMediaClosed() {
        demo?.mediaClosed()
        onLayerClosed()
    }

    override fun onMediaError(message: String) {
        toasts.show(Command.Notify(null, getString(R.string.playback_failed, message), null, 8, Command.Position.TOP_RIGHT))
    }

    private fun onLayerClosed() {
        if (!media.isFullscreen && !webOverlay.isFullscreen && !menuVisible) dashboard.focus()
    }

    // ---- Configuration ----------------------------------------------------------------

    private fun applyConfig(old: Config?, new: Config) {
        if (demo != null) {
            if (!new.isConfigured) return // the demo carries on until Home Assistant takes over
            endDemo()
        }
        showSetup(!new.isConfigured)
        if (new.isConfigured) {
            if (!started || old == null) {
                dashboard.start(new)
                started = true
            } else {
                dashboard.applyConfig(old, new)
            }
        }
        applyKeepScreenOn()
        restartRotation(seconds = new.rotateSeconds)
    }

    /** The next rotation step comes a full period from now, plus [afterMs]. */
    private fun restartRotation(afterMs: Long = 0, seconds: Int = config.rotateSeconds) {
        main.removeCallbacks(rotateRunnable)
        if (seconds > 0) main.postDelayed(rotateRunnable, afterMs + seconds * 1000L)
    }

    private fun showSetup(show: Boolean) {
        setupScreen.visibility = if (show) View.VISIBLE else View.GONE
        main.removeCallbacks(setupRefresh)
        if (show) {
            main.post(setupRefresh)
            findViewById<View>(R.id.setup_demo).requestFocus() // a TV remote's OK starts the demo
        }
        app.hub.update { view = if (show) StateModel.VIEW_SETUP else StateModel.VIEW_DASHBOARD }
    }

    private fun refreshSetup() {
        val ip = NetUtil.localIpv4()
        val port = app.settings.port
        findViewById<TextView>(R.id.setup_address).text =
            if (ip == null) getString(R.string.setup_no_network)
            // One line on wide screens; a phone's is too narrow for both.
            else "Host  $ip" + (if (isWide) "     " else "\n") + "Port  $port"
        findViewById<TextView>(R.id.setup_pin).text = formatPin(app.pairing.pin)
        val qr = findViewById<ImageView>(R.id.setup_qr)
        if (ip != null) {
            val url = SetupCode.url(ip, port, app.pairing.pin)
            if (qr.contentDescription != url) {
                qr.setImageBitmap(SetupCode.bitmap(url, (220 * resources.displayMetrics.density).toInt()))
                qr.contentDescription = url
            }
            qr.visibility = View.VISIBLE
        } else {
            qr.visibility = View.INVISIBLE
        }
        findViewById<TextView>(R.id.setup_manual).text =
            getString(R.string.setup_manual, if (ip != null) "http://$ip:$port" else "http://<address>:$port")
        findViewById<TextView>(R.id.setup_device).text =
            getString(R.string.setup_device, app.settings.deviceName, BuildConfig.VERSION_NAME)
    }

    // ---- Demo ---------------------------------------------------------------------------

    private fun startDemo() {
        if (demo != null || app.settings.config.isConfigured) return
        val tour = DemoTour(this, this)
        demo = tour
        showSetup(false)
        dashboard.start(tour.config)
        started = true
        applyKeepScreenOn()
        restartRotation()
        tour.start()
    }

    /** Ends the demo; [applyConfig] then shows the setup screen, or Home Assistant's dashboards. */
    private fun endDemo() {
        val tour = demo ?: return
        demo = null
        tour.stop()
        main.removeCallbacks(revertRunnable)
        closeMenu()
        media.stop()
        webOverlay.close()
        toasts.clear()
        dashboard.destroy()
        started = false
        userBrowsing = false
    }

    private fun applyKeepScreenOn() {
        if (config.keepScreenOn && !standby) {
            window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        } else {
            window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        }
    }

    // ---- Navigation -----------------------------------------------------------------

    private fun goHome() {
        main.removeCallbacks(revertRunnable)
        closeMenu()
        media.stop()
        webOverlay.close()
        userBrowsing = false
        dashboard.navigate(config.homePath)
    }

    private fun closeFullscreenLayers() {
        if (media.isFullscreen) media.stop()
        if (webOverlay.isFullscreen) webOverlay.close()
    }

    private fun cycleDashboard(step: Int) {
        val list = config.dashboards
        if (list.isEmpty()) return
        val index = list.indexOfFirst { it.path == dashboard.lastTarget }
        val next = if (index < 0) (if (step > 0) 0 else list.size - 1) else (index + step + list.size) % list.size
        dashboard.navigate(list[next].path)
    }

    private fun handleIntent(intent: Intent?) {
        val data = intent?.data ?: return
        if (data.scheme != "pleatview") return
        when (data.host) {
            "home" -> handle(Command.Home)
            "reload" -> handle(Command.Reload)
            "dashboard" -> {
                val path = data.getQueryParameter("path") ?: data.path
                val target = if (path.isNullOrBlank() || path == "/") "" else Config.normalizeTarget(path)
                // Any app or web page can open these links: they reach Home Assistant's pages and the
                // configured dashboards, not any site someone would like the TV to show.
                val allowed = !Config.isHttpUrl(target) || config.isHaUrl(target) ||
                    target == config.homeDashboard || config.dashboards.any { it.path == target }
                if (target.isNotEmpty() && allowed) handle(Command.ShowDashboard(target, null))
            }
        }
        setIntent(Intent(intent).setData(null))
    }

    private fun drainPending() {
        if (!visible) return
        app.hub.takePending().forEach(::handle)
    }

    override fun onConfigurationChanged(newConfig: Configuration) {
        super.onConfigurationChanged(newConfig)
        fitLayouts()
    }

    /** Room for the setup address on one line. */
    private val isWide: Boolean get() = resources.configuration.screenWidthDp >= WIDE_LAYOUT_DP

    /**
     * Text and QR code side by side on TVs, wide screens and phones turned sideways (where,
     * stacked, the QR code would be below the screen); stacked on phones and portrait tablets.
     */
    private val sideBySide: Boolean get() = resources.configuration.let {
        it.screenWidthDp >= WIDE_LAYOUT_DP ||
            (it.orientation == Configuration.ORIENTATION_LANDSCAPE && it.screenWidthDp >= SIDE_BY_SIDE_MIN_DP)
    }

    private fun fitLayouts() {
        val widthDp = resources.configuration.screenWidthDp
        val wide = sideBySide
        val density = resources.displayMetrics.density
        fun stack(row: Int, text: Int, textWidthDp: Int, code: Int) {
            findViewById<LinearLayout>(row).orientation = if (wide) LinearLayout.HORIZONTAL else LinearLayout.VERTICAL
            val textView = findViewById<View>(text)
            textView.layoutParams = (textView.layoutParams as LinearLayout.LayoutParams).apply {
                // The screens have 24 dp padding on each side; beside the text, the QR code
                // takes 56 + 240 dp.
                width = (minOf(textWidthDp, widthDp - 48 - if (wide) 296 else 0) * density).toInt()
            }
            val codeView = findViewById<View>(code)
            codeView.layoutParams = (codeView.layoutParams as LinearLayout.LayoutParams).apply {
                marginStart = if (wide) (56 * density).toInt() else 0
                topMargin = if (wide) 0 else (28 * density).toInt()
            }
        }
        stack(R.id.setup_row, R.id.setup_text, 560, R.id.setup_code)
        stack(R.id.signin_row, R.id.signin_text, 520, R.id.signin_code)
        // A phone can be narrower than the menu, and than the now-playing text beside the
        // cover art (56 + 14 dp) in its bar (24 dp margins and 12 dp padding on each side).
        fun cap(view: Int, dp: Int) {
            val target = findViewById<View>(view)
            target.layoutParams = target.layoutParams.apply { width = (dp * density).toInt() }
        }
        cap(R.id.menu_panel, minOf(MENU_WIDTH_DP, widthDp))
        cap(R.id.now_playing_text, minOf(NOW_PLAYING_TEXT_DP, widthDp - 48 - 24 - 70))
        if (setupScreen.visibility == View.VISIBLE) refreshSetup()
    }

    // ---- Touch (phones, tablets, Chromebooks) -------------------------------------------

    override fun dispatchTouchEvent(event: MotionEvent): Boolean {
        if (event.actionMasked == MotionEvent.ACTION_DOWN) {
            // A touch counts like a remote press: rotation pauses, and after a while the
            // home dashboard comes back.
            lastInput = SystemClock.elapsedRealtime()
            if (standby) {
                wake()
                return true
            }
            if (!menuVisible) {
                userBrowsing = true
                showTouchMenu()
            }
        }
        return super.dispatchTouchEvent(event)
    }

    /** The menu button fades in on a touch and out again, so it never covers the dashboard for long. */
    private fun showTouchMenu() {
        main.removeCallbacks(hideTouchMenu)
        touchMenu.animate().cancel()
        touchMenu.alpha = 1f
        touchMenu.visibility = View.VISIBLE
        main.postDelayed(hideTouchMenu, TOUCH_MENU_MS)
    }

    // ---- Remote control ---------------------------------------------------------------

    override fun dispatchKeyEvent(event: KeyEvent): Boolean {
        // API 33+ delivers Back through OnBackInvokedCallback instead of key events.
        if (event.keyCode == KeyEvent.KEYCODE_BACK && Build.VERSION.SDK_INT < 33) {
            if (event.action == KeyEvent.ACTION_UP && !event.isCanceled) handleBack()
            return true
        }
        if (event.action == KeyEvent.ACTION_DOWN) {
            lastInput = SystemClock.elapsedRealtime()
            if (standby) {
                wake()
                return true
            }
            when (event.keyCode) {
                KeyEvent.KEYCODE_MENU, KeyEvent.KEYCODE_SETTINGS -> {
                    if (menuVisible) closeMenu() else openMenu()
                    return true
                }
                KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE, KeyEvent.KEYCODE_MEDIA_PLAY, KeyEvent.KEYCODE_MEDIA_PAUSE,
                KeyEvent.KEYCODE_MEDIA_STOP, KeyEvent.KEYCODE_MEDIA_FAST_FORWARD, KeyEvent.KEYCODE_MEDIA_REWIND,
                -> if (media.isActive) {
                    when (event.keyCode) {
                        KeyEvent.KEYCODE_MEDIA_PLAY -> media.resume()
                        KeyEvent.KEYCODE_MEDIA_PAUSE -> media.pause()
                        KeyEvent.KEYCODE_MEDIA_STOP -> media.stop()
                        KeyEvent.KEYCODE_MEDIA_FAST_FORWARD -> media.seekBy(SEEK_STEP_MS)
                        KeyEvent.KEYCODE_MEDIA_REWIND -> media.seekBy(-SEEK_STEP_MS)
                        else -> media.togglePlayPause()
                    }
                    return true
                }
                KeyEvent.KEYCODE_CHANNEL_UP, KeyEvent.KEYCODE_PAGE_UP, KeyEvent.KEYCODE_MEDIA_NEXT -> {
                    userBrowsing = true
                    cycleDashboard(+1)
                    return true
                }
                KeyEvent.KEYCODE_CHANNEL_DOWN, KeyEvent.KEYCODE_PAGE_DOWN, KeyEvent.KEYCODE_MEDIA_PREVIOUS -> {
                    userBrowsing = true
                    cycleDashboard(-1)
                    return true
                }
                KeyEvent.KEYCODE_DPAD_UP, KeyEvent.KEYCODE_DPAD_DOWN, KeyEvent.KEYCODE_DPAD_LEFT,
                KeyEvent.KEYCODE_DPAD_RIGHT, KeyEvent.KEYCODE_DPAD_CENTER, KeyEvent.KEYCODE_ENTER,
                -> if (!menuVisible) userBrowsing = true
            }
        }
        return super.dispatchKeyEvent(event)
    }

    private fun handleBack() {
        lastInput = SystemClock.elapsedRealtime()
        when {
            standby -> wake()
            menuVisible -> closeMenu()
            signInVisible -> dashboard.dismissSignIn()
            media.isFullscreen -> media.stop()
            webOverlay.current != null -> webOverlay.close()
            media.isActive -> media.stop()
            !config.isConfigured -> openMenu()
            dashboard.goBack() -> Unit
            dashboard.lastTarget != config.homePath -> {
                userBrowsing = false
                dashboard.navigate(config.homePath)
            }
            else -> openMenu()
        }
    }

    // ---- Menu -------------------------------------------------------------------------

    private fun openMenu() {
        demo?.cancel() // the tour's next steps would close the menu
        menuItems.removeAllViews()
        val ip = NetUtil.localIpv4() ?: "?"
        val info = getString(
            R.string.menu_info, app.settings.deviceName, "$ip:${app.settings.port}",
            formatPin(app.pairing.pin), BuildConfig.VERSION_NAME,
        )
        menuItems.addView(menuText(info, 15f))
        if (demo != null) menuItems.addView(menuText(getString(R.string.menu_demo_info), 13f))
        if (config.isConfigured) menuItems.addView(menuText(config.haUrl, 13f))
        if (demo == null && !app.hub.canDrawOverlays()) {
            val missing = if (app.hub.canOpenFromBackground()) R.string.menu_overlay_missing_notifications else R.string.menu_overlay_missing
            menuItems.addView(menuText(getString(missing), 13f))
            addMenuButton(getString(R.string.menu_allow_overlay)) { requestOverlayPermission() }
        }

        var first: Button? = null
        if (demo != null) {
            first = addMenuButton(getString(R.string.menu_demo_end)) {
                endDemo()
                applyConfig(null, config)
            }
            config.dashboards.forEach { d ->
                addMenuButton(d.title) {
                    userBrowsing = true
                    dashboard.navigate(d.path)
                }
            }
            addMenuButton(getString(R.string.menu_demo_tour)) {
                endDemo()
                startDemo()
            }
        } else if (config.isConfigured) {
            first = addMenuButton(getString(R.string.menu_home)) { goHome() }
            config.dashboards.forEach { d ->
                addMenuButton(d.title) {
                    userBrowsing = true
                    dashboard.navigate(d.path)
                }
            }
            addMenuButton(getString(R.string.menu_reload)) { dashboard.reload() }
        }
        val saved = app.signIns.count
        if (saved > 0) {
            addMenuButton(getString(R.string.menu_forget_signins, saved)) {
                app.signIns.forget()
                closeMenu()
                toasts.show(Command.Notify(null, getString(R.string.signins_forgotten), null, 6, Command.Position.TOP_RIGHT))
            }
        }
        if (Support.offered(BuildConfig.DONATE_URL, Support.installer(this))) {
            addMenuButton(getString(R.string.menu_support)) { showSupport() }
        }
        val exit = addMenuButton(getString(R.string.menu_exit)) { finish() }
        menuOverlay.visibility = View.VISIBLE
        (first ?: exit).requestFocus()
    }

    /** The donation page as a QR code, since a TV has no browser to speak of. */
    private fun showSupport() {
        menuItems.removeAllViews()
        menuItems.addView(menuText(getString(R.string.menu_support), 18f))
        menuItems.addView(menuText(getString(R.string.support_text), 14f))
        val size = (200 * resources.displayMetrics.density).toInt()
        val qr = ImageView(this).apply {
            id = R.id.support_qr
            setImageBitmap(SetupCode.bitmap(BuildConfig.DONATE_URL, size))
            contentDescription = BuildConfig.DONATE_URL
        }
        menuItems.addView(qr, LinearLayout.LayoutParams(size, size).apply { gravity = Gravity.CENTER_HORIZONTAL })
        menuItems.addView(menuText(BuildConfig.DONATE_URL.removePrefix("https://"), 13f).apply {
            gravity = Gravity.CENTER_HORIZONTAL
            setPadding(0, (12 * resources.displayMetrics.density).toInt(), 0, 0)
        })
        val back = addMenuButton(getString(R.string.support_back)) { openMenu() }
        menuOverlay.visibility = View.VISIBLE
        back.requestFocus()
    }

    /** Opens Android's "Display over other apps" screen; many Google TV builds don't have one. */
    private fun requestOverlayPermission() {
        val intent = Intent(android.provider.Settings.ACTION_MANAGE_OVERLAY_PERMISSION, "package:$packageName".toUri())
        try {
            startActivity(intent)
        } catch (e: ActivityNotFoundException) {
            toasts.show(
                Command.Notify(null, getString(R.string.overlay_settings_missing), null, 15, Command.Position.TOP_RIGHT),
            )
        }
    }

    private fun closeMenu() {
        if (!menuVisible) return
        menuOverlay.visibility = View.GONE
        dashboard.focus()
    }

    private fun menuText(text: String, sizeSp: Float) = TextView(this).apply {
        this.text = text
        textSize = sizeSp
        setTextColor(getColor(R.color.text_secondary))
        setPadding(0, 0, 0, (12 * resources.displayMetrics.density).toInt())
    }

    private fun addMenuButton(label: String, action: () -> Unit): Button {
        val button = Button(this, null, 0, R.style.PleatView_MenuButton)
        button.text = label
        button.setOnClickListener {
            closeMenu()
            action()
        }
        val params = LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT)
        params.topMargin = (8 * resources.displayMetrics.density).toInt()
        menuItems.addView(button, params)
        return button
    }

    // ---- Standby ----------------------------------------------------------------------

    private fun enterStandby() {
        if (standby) return
        standby = true
        closeMenu()
        media.stop()
        webOverlay.close()
        curtain.visibility = View.VISIBLE
        dashboard.onPause()
        applyKeepScreenOn() // lets the TV's own screensaver/sleep timer take over
        app.hub.update { power = StateModel.POWER_STANDBY }
    }

    private fun wake() {
        if (!standby) return
        standby = false
        curtain.visibility = View.GONE
        dashboard.onResume()
        dashboard.focus()
        applyKeepScreenOn()
        app.hub.update { power = StateModel.POWER_ON }
    }

    private fun hideSystemBars() {
        WindowCompat.setDecorFitsSystemWindows(window, false)
        WindowInsetsControllerCompat(window, window.decorView).apply {
            hide(WindowInsetsCompat.Type.systemBars())
            systemBarsBehavior = WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
        }
    }

    private fun formatPin(pin: String) = pin.chunked(3).joinToString(" ")

    companion object {
        private const val ROTATE_INPUT_GRACE_MS = 60_000L
        private const val SEEK_STEP_MS = 10_000L
        private const val TOUCH_MENU_MS = 5_000L

        /** Width the setup and sign-in screens need with the text and QR code side by side. */
        private const val WIDE_LAYOUT_DP = 900

        /** Narrowest sideways phone that fits the QR code beside a 296 dp column of text. */
        private const val SIDE_BY_SIDE_MIN_DP = 640
        private const val MENU_WIDTH_DP = 380
        private const val NOW_PLAYING_TEXT_DP = 320
    }
}
