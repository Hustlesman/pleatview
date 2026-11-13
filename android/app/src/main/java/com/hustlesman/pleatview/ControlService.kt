package com.hustlesman.pleatview

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.os.Build
import android.os.IBinder
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import java.io.IOException

/**
 * Keeps the control API reachable (and advertised over mDNS) while PleatView is in
 * the background, so Home Assistant can still see the TV and bring it to the front.
 */
class ControlService : Service() {
    private var server: HttpServer? = null
    private var advertiser: NsdAdvertiser? = null
    private var advertisedName: String? = null
    private val nameListener: (Config, Config) -> Unit = { _, _ ->
        val app = PleatApp.from(this)
        if (app.settings.deviceName != advertisedName) advertise(app)
    }

    override fun onCreate() {
        super.onCreate()
        val app = PleatApp.from(this)
        startInForeground(app.settings.port)

        server = HttpServer(app.settings.port, app.api::handle).also {
            try {
                it.start()
            } catch (e: IOException) {
                Log.e(TAG, "Could not listen on port ${app.settings.port}", e)
            }
        }
        advertise(app)
        app.settings.addListener(nameListener)
        // Protocol v2: the connection out lives with the process, from here on.
        app.connectOut?.start()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int = START_STICKY

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        PleatApp.from(this).settings.removeListener(nameListener)
        advertiser?.unregister()
        server?.stop()
        super.onDestroy()
    }

    private fun advertise(app: PleatApp) {
        advertiser?.unregister()
        advertisedName = app.settings.deviceName
        advertiser = NsdAdvertiser(this).also {
            it.register(
                name = app.settings.deviceName,
                port = app.settings.port,
                attributes = mapOf(
                    "id" to app.settings.deviceId,
                    "name" to app.settings.deviceName,
                    "api" to Api.API_VERSION.toString(),
                    "version" to BuildConfig.VERSION_NAME,
                ),
            )
        }
    }

    private fun startInForeground(port: Int) {
        val manager = getSystemService(NotificationManager::class.java)
        if (Build.VERSION.SDK_INT >= 26) {
            manager.createNotificationChannel(
                NotificationChannel(CHANNEL, getString(R.string.notification_channel), NotificationManager.IMPORTANCE_MIN),
            )
        }
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE,
        )
        val notification: Notification = NotificationCompat.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_launcher_foreground)
            .setContentTitle(getString(R.string.app_name))
            .setContentText(getString(R.string.notification_text, port))
            .setContentIntent(open)
            .setOngoing(true)
            .setPriority(NotificationCompat.PRIORITY_MIN)
            .build()
        val type = if (Build.VERSION.SDK_INT >= 29) ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE else 0
        ServiceCompat.startForeground(this, NOTIFICATION_ID, notification, type)
    }

    companion object {
        private const val TAG = "PleatView"
        private const val CHANNEL = "control"
        private const val NOTIFICATION_ID = 1

        fun start(context: Context) {
            try {
                ContextCompat.startForegroundService(context, Intent(context, ControlService::class.java))
            } catch (e: Exception) {
                // Android 12+ refuses background starts in some states; the activity retries on resume.
                Log.w(TAG, "Could not start control service", e)
            }
        }
    }
}

/** Advertises `_pleatview._tcp` so Home Assistant discovers the TV through zeroconf. */
class NsdAdvertiser(context: Context) {
    private val nsd = context.getSystemService(Context.NSD_SERVICE) as NsdManager
    private var listener: NsdManager.RegistrationListener? = null

    fun register(name: String, port: Int, attributes: Map<String, String>) {
        val info = NsdServiceInfo().apply {
            serviceName = name
            serviceType = SERVICE_TYPE
            this.port = port
            attributes.forEach { (k, v) -> setAttribute(k, v) }
        }
        val l = object : NsdManager.RegistrationListener {
            override fun onServiceRegistered(info: NsdServiceInfo) {
                Log.i(TAG, "Advertising ${info.serviceName}")
            }

            override fun onRegistrationFailed(info: NsdServiceInfo, error: Int) {
                Log.w(TAG, "mDNS registration failed: $error")
            }

            override fun onServiceUnregistered(info: NsdServiceInfo) = Unit
            override fun onUnregistrationFailed(info: NsdServiceInfo, error: Int) = Unit
        }
        listener = l
        try {
            nsd.registerService(info, NsdManager.PROTOCOL_DNS_SD, l)
        } catch (e: Exception) {
            Log.w(TAG, "mDNS registration failed", e)
            listener = null
        }
    }

    fun unregister() {
        listener?.let { runCatching { nsd.unregisterService(it) } }
        listener = null
    }

    companion object {
        private const val TAG = "PleatView"
        const val SERVICE_TYPE = "_pleatview._tcp"
    }
}
