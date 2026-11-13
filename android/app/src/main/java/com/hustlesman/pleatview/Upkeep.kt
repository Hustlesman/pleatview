package com.hustlesman.pleatview

import android.app.Activity
import android.app.ActivityManager
import android.content.Context
import android.content.Intent
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.Process
import android.os.StatFs
import android.os.SystemClock
import android.util.Log
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.net.Inet4Address
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.NetworkInterface
import java.net.Socket
import java.net.URI
import java.util.concurrent.Callable
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/**
 * Upkeep (docs/protocol.md, "Upkeep"): what a controller needs to look after a screen it can't
 * walk up to. Android lets an app start itself again, not restart the device or install an
 * update without a person, so this app lists `restart_app` and `diagnostics` only.
 */
object Upkeep {
    private const val TAG = "PleatView"
    private const val CHECK_MS = 4000L
    private const val LOG_LINES = 100

    // Addresses keep their host and path; their queries (where tokens travel) are left out.
    private val QUERY = Regex("""(\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s?#"']*)\?[^\s"']*""")
    private val BEARER = Regex("""(Bearer\s+)\S+""", RegexOption.IGNORE_CASE)

    fun redact(text: String): String = BEARER.replace(QUERY.replace(text, "$1?…"), "$1…")

    /**
     * Starts the app again in a new process: a small activity in a process of its own ends this
     * one and opens the app. If Android won't let it open (in the background without the
     * overlay permission), nothing is ended.
     */
    fun restartApp(context: Context) {
        Log.i(TAG, "Starting again, as the controller asked")
        val intent = Intent(context, RestartActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_NO_ANIMATION)
            .putExtra(RestartActivity.EXTRA_PID, Process.myPid())
        context.startActivity(intent)
    }

    /** What the screen knows of itself, and a few fixed checks. Blocks for a few seconds at most. */
    fun diagnostics(context: Context, home: String?): JSONObject {
        val activity = context.getSystemService(ActivityManager::class.java)
        val memory = ActivityManager.MemoryInfo().also { activity?.getMemoryInfo(it) }
        val storage = runCatching { StatFs(context.filesDir.path) }.getOrNull()
        val gateway = gateway(context)
        return JSONObject()
            .put("uptime", SystemClock.elapsedRealtime() / 1000)
            .put("app_uptime", (SystemClock.elapsedRealtime() - Process.getStartElapsedRealtime()) / 1000)
            .put("os", "Android ${Build.VERSION.RELEASE}")
            .put("kernel", System.getProperty("os.version") ?: JSONObject.NULL)
            .put("load", JSONObject.NULL)
            .put("processors", Runtime.getRuntime().availableProcessors())
            .put("memory", JSONObject().put("total", memory.totalMem).put("available", memory.availMem))
            .put("storage", storage?.let { JSONObject().put("total", it.totalBytes).put("free", it.availableBytes) } ?: JSONObject.NULL)
            .put("temperature", JSONObject.NULL)
            .put("power_warning", JSONObject.NULL)
            .put("network", network(context))
            .put("gateway", gateway ?: JSONObject.NULL)
            .put("checks", checks(gateway, home))
            .put("log", recentLog())
    }

    private fun gateway(context: Context): String? = runCatching {
        val connectivity = context.getSystemService(ConnectivityManager::class.java)
        val links = connectivity.getLinkProperties(connectivity.activeNetwork) ?: return null
        links.routes.firstOrNull { it.isDefaultRoute && it.gateway is Inet4Address }?.gateway?.hostAddress
    }.getOrNull()

    private fun network(context: Context): JSONArray {
        val connectivity = context.getSystemService(ConnectivityManager::class.java)
        val capabilities = runCatching { connectivity.getNetworkCapabilities(connectivity.activeNetwork) }.getOrNull()
        val wifiSignal = if (Build.VERSION.SDK_INT >= 29 && capabilities?.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) == true) {
            capabilities.signalStrength.takeIf { it != Int.MIN_VALUE && it < 0 }
        } else null
        val result = JSONArray()
        val interfaces = runCatching { NetworkInterface.getNetworkInterfaces()?.toList() }.getOrNull().orEmpty()
        for (nic in interfaces.sortedBy { it.name }) {
            if (runCatching { !nic.isUp || nic.isLoopback }.getOrDefault(true)) continue
            val addresses = nic.inetAddresses.toList().mapNotNull { it.hostAddress?.substringBefore('%') }
            if (addresses.isEmpty()) continue
            val kind = when {
                nic.name.startsWith("wlan") -> "wifi"
                nic.name.startsWith("eth") -> "ethernet"
                else -> "other"
            }
            result.put(
                JSONObject().put("name", nic.name).put("kind", kind).put("addresses", JSONArray(addresses.take(6)))
                    .put("signal", if (kind == "wifi") wifiSignal ?: JSONObject.NULL else JSONObject.NULL),
            )
        }
        return result
    }

    /** The gateway answers, the home page's host resolves, and a connection to it opens. */
    fun checks(gateway: String?, home: String?): JSONArray {
        val pool = Executors.newFixedThreadPool(3)
        try {
            val jobs = mutableListOf<Pair<Triple<String, String, Long>, java.util.concurrent.Future<Boolean>>>()
            fun add(check: String, target: String, action: () -> Boolean) {
                jobs += Triple(check, target, SystemClock.elapsedRealtime()) to pool.submit(Callable { action() })
            }
            if (gateway != null) add("gateway", gateway) { ping(gateway) }
            val uri = runCatching { URI(home ?: "") }.getOrNull()
            val host = uri?.host?.trim('[', ']')
            if (uri != null && (uri.scheme == "http" || uri.scheme == "https") && !host.isNullOrEmpty()) {
                val port = if (uri.port > 0) uri.port else if (uri.scheme == "https") 443 else 80
                add("dns", host) { InetAddress.getAllByName(host).isNotEmpty() }
                add("home", "$host:$port") {
                    Socket().use { it.connect(InetSocketAddress(host, port), CHECK_MS.toInt()) }
                    true
                }
            }
            val result = JSONArray()
            for ((about, future) in jobs) {
                val (check, target, started) = about
                val ok = runCatching { future.get(CHECK_MS + 500, TimeUnit.MILLISECONDS) }.getOrDefault(false)
                result.put(
                    JSONObject().put("check", check).put("target", target).put("ok", ok)
                        .put("ms", SystemClock.elapsedRealtime() - started),
                )
            }
            return result
        } finally {
            pool.shutdownNow()
        }
    }

    private fun ping(address: String): Boolean {
        val ping = listOf("/system/bin/ping", "/system/xbin/ping").firstOrNull { File(it).canExecute() }
            ?: return InetAddress.getByName(address).isReachable(2000)
        val process = ProcessBuilder(ping, "-c", "1", "-W", "2", "-n", address).redirectErrorStream(true).start()
        return process.waitFor() == 0 // ping gives up after 2 seconds; the check after 4
    }

    /** This app's own last log lines, which apps may read since Android 4.1, without queries. */
    private fun recentLog(): JSONArray {
        val lines = runCatching {
            val process = ProcessBuilder("logcat", "-d", "-t", LOG_LINES.toString(), "--pid=${Process.myPid()}", "-v", "time")
                .redirectErrorStream(true).start()
            val text = process.inputStream.bufferedReader().use { it.readText() }
            process.waitFor() // -d: logcat writes what it has and ends
            text.lines().filter { it.isNotBlank() && !it.startsWith("-----") }.takeLast(LOG_LINES)
        }.getOrDefault(emptyList())
        return JSONArray(lines.map { redact(it).take(400) })
    }
}

/** Ends the app's main process and opens the app again, from a process of its own. */
class RestartActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val pid = intent.getIntExtra(EXTRA_PID, -1)
        if (pid > 0 && pid != Process.myPid()) Process.killProcess(pid)
        Handler(Looper.getMainLooper()).postDelayed({
            startActivity(
                Intent(this, MainActivity::class.java)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK or Intent.FLAG_ACTIVITY_NO_ANIMATION),
            )
            finish()
            Process.killProcess(Process.myPid())
        }, 600)
    }

    companion object {
        const val EXTRA_PID = "pid"
        const val PROCESS_SUFFIX = ":restart"
    }
}
