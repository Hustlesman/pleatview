package com.hustlesman.pleatview

import android.app.Application
import android.content.Context

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

    override fun onCreate() {
        super.onCreate()
        settings = Settings(this)
        pairing = Pairing(settings)
        signIns = SignIns(settings, KeystoreVault())
        hub = Hub(this, settings, pairing, signIns)
        Battery.watch(this) { info -> hub.update { battery = info } }
    }

    companion object {
        fun from(context: Context): PleatApp = context.applicationContext as PleatApp
    }
}
