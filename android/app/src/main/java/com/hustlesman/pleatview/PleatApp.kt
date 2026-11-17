package com.hustlesman.pleatview

import android.app.Application
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.Process
import android.util.Log
import java.io.File

class PleatApp : Application() {
    lateinit var settings: Settings
        private set
    lateinit var pairing: Pairing
        private set
    lateinit var hub: Hub
        private set
    lateinit var signIns: SignIns
        private set

    /** The phone setup page served at `/`. */
    val setupPage: ByteArray by lazy { assets.open("setup.html").use { it.readBytes() } }

    /** The local control API (protocol v1), which protocol v2's calls go through too. */
    val api: Api by lazy { Api(hub, pairing) { setupPage } }

    /** Protocol v2: one connection out to a controller anywhere; none if the key store fails. */
    val connectOut: ConnectOut? by lazy {
        try {
            ConnectOut(
                prefs = getSharedPreferences("connect_out", MODE_PRIVATE),
                signer = ConnectOut.KeystoreSigner(),
                // What it says when it connects (docs/protocol-v2.md): with its features, which tell
                // a controller it asks the person at the screen first (enrol_confirm).
                info = { hub.info().put("features", org.json.JSONArray(StateModel.FEATURES)) },
                call = api::call,
                stateJson = hub::stateJson,
                addStateListener = hub::addStateListener,
                removeStateListener = hub::removeStateListener,
                codeChanged = hub::codeChanged,
            )
        } catch (e: Exception) {
            Log.w("PleatView", "No connection out: the key store failed", e)
            null
        }
    }

    override fun onCreate() {
        super.onCreate()
        // The process that starts the app again (Upkeep) needs none of it.
        if (processName().endsWith(RestartActivity.PROCESS_SUFFIX)) return
        reopenAfterCrash()
        settings = Settings(this)
        pairing = Pairing(settings)
        pairing.onLockout = { Log.w("PleatView", "Pairing locked for a minute after five wrong PINs; the PIN changed") }
        signIns = SignIns(settings, KeystoreVault())
        hub = Hub(this, settings, pairing, signIns)
        hub.enrolOut = { url, token, fingerprint ->
            (connectOut ?: throw IllegalArgumentException("this device's key store isn't available")).enrol(url, token, fingerprint)
        }
        Battery.watch(this) { info -> hub.update { battery = info } }
    }

    /**
     * If PleatView stops while it's on screen, it opens again (Reopen.afterCrash): a screen on a
     * wall has nobody to open it. Android still notes the crash first (the handler before this
     * one), so it reaches the crash reports; RestartActivity waits for that.
     */
    private fun reopenAfterCrash() {
        val earlierHandler = Thread.getDefaultUncaughtExceptionHandler()
        Thread.setDefaultUncaughtExceptionHandler { thread, error ->
            try {
                val now = System.currentTimeMillis()
                val crashes = getSharedPreferences(CRASHES, MODE_PRIVATE)
                val earlier = Reopen.crashTimes(crashes.getString(CRASH_TIMES, null), now)
                // commit, not apply: the process is about to end.
                crashes.edit().putString(CRASH_TIMES, (earlier + now).joinToString(",")).commit()
                val onScreen = ::hub.isInitialized && hub.display?.isVisible == true
                if (Reopen.afterCrash(now, onScreen, earlier)) {
                    Log.w("PleatView", "Stopped on screen; opening again")
                    startActivity(
                        Intent(this, RestartActivity::class.java)
                            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_NO_ANIMATION)
                            .putExtra(RestartActivity.EXTRA_PID, Process.myPid())
                            .putExtra(RestartActivity.EXTRA_CRASHED, true),
                    )
                } else if (onScreen) {
                    Log.w("PleatView", "Stopped on screen ${earlier.size + 1} times in ten minutes; staying closed")
                }
            } catch (_: Throwable) {
                // Whatever happens here, Android still hears of the crash.
            }
            earlierHandler?.uncaughtException(thread, error)
        }
    }

    private fun processName(): String =
        if (Build.VERSION.SDK_INT >= 28) getProcessName()
        else runCatching { File("/proc/self/cmdline").readText().trimEnd('\u0000') }.getOrDefault("")

    companion object {
        private const val CRASHES = "crashes"
        private const val CRASH_TIMES = "times"

        fun from(context: Context): PleatApp = context.applicationContext as PleatApp
    }
}
