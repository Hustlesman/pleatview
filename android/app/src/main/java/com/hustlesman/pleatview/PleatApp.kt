package com.hustlesman.pleatview

import android.app.Application
import android.content.Context
import android.util.Log

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
                info = hub::info,
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
        settings = Settings(this)
        pairing = Pairing(settings)
        signIns = SignIns(settings, KeystoreVault())
        hub = Hub(this, settings, pairing, signIns)
        hub.enrolOut = { url, token, fingerprint ->
            (connectOut ?: throw IllegalArgumentException("this device's key store isn't available")).enrol(url, token, fingerprint)
        }
        Battery.watch(this) { info -> hub.update { battery = info } }
    }

    companion object {
        fun from(context: Context): PleatApp = context.applicationContext as PleatApp
    }
}
