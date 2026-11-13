package com.hustlesman.pleatview

import org.json.JSONArray
import org.json.JSONObject
import java.net.URI

data class Dashboard(val title: String, val path: String)

/**
 * User-facing configuration. Written by Home Assistant (through the integration),
 * the phone setup page, or both, via `POST /api/config`.
 */
data class Config(
    val deviceName: String = "",
    val haUrl: String = "",
    val haToken: String = "",
    val dashboards: List<Dashboard> = emptyList(),
    val homeDashboard: String = "",
    val kiosk: String = KIOSK_FULL,
    val zoom: Int = 100,
    val keepScreenOn: Boolean = true,
    val launchOnBoot: Boolean = true,
    val reloadMinutes: Int = 0,
    val rotateSeconds: Int = 0,
    val returnHomeMinutes: Int = 0,
    val reopenMinutes: Int = 10,
    /** A live stream of the TV's dashboards, rendered by Home Assistant; empty: the TV renders them. */
    val renderStream: String = "",
    /** A home page that is a playlist of media the app plays and caches itself (from another controller). */
    val homePlaylist: List<PlaylistItem> = emptyList(),
    val rev: String = "",
) {
    /** Set up: Home Assistant's address, or a home page of its own from another controller,
     *  which leaves the setup screen without Home Assistant. */
    val isConfigured: Boolean get() = haUrl.isNotEmpty() || isHttpUrl(homeDashboard) || homePlaylist.isNotEmpty()

    /** `scheme://host[:port]` of Home Assistant, used to scope the auth bridge. */
    val haOrigin: String? get() = originOf(haUrl)

    /** Path (or URL) of the dashboard PleatView returns to. "/" lets HA pick the user's default. */
    val homePath: String get() = homeDashboard.ifEmpty { dashboards.firstOrNull()?.path ?: "/" }

    /** Turns a dashboard path such as `/lovelace/cameras` into an absolute URL. */
    fun resolve(pathOrUrl: String): String {
        val value = pathOrUrl.trim()
        if (isHttpUrl(value)) return value
        return haUrl + normalizePath(value)
    }

    fun isHaUrl(url: String?): Boolean {
        val origin = haOrigin ?: return false
        return url != null && originOf(url) == origin
    }

    fun toJson(includeSecrets: Boolean = false): JSONObject = JSONObject().apply {
        put("device_name", deviceName)
        put("ha_url", haUrl)
        if (includeSecrets) put("ha_token", haToken) else put("ha_token_set", haToken.isNotEmpty())
        put("dashboards", JSONArray().apply {
            dashboards.forEach { put(JSONObject().put("title", it.title).put("path", it.path)) }
        })
        put("home_dashboard", homeDashboard)
        put("kiosk", kiosk)
        put("zoom", zoom)
        put("keep_screen_on", keepScreenOn)
        put("launch_on_boot", launchOnBoot)
        put("reload_minutes", reloadMinutes)
        put("rotate_seconds", rotateSeconds)
        put("return_home_minutes", returnHomeMinutes)
        put("reopen_minutes", reopenMinutes)
        put("render_stream", renderStream)
        put("home_playlist", JSONArray().apply {
            homePlaylist.forEach { put(JSONObject().put("url", it.url).put("kind", it.kind).put("seconds", it.seconds)) }
        })
        put("rev", rev)
    }

    /**
     * Applies a partial update. Only keys present in [update] change. Any update
     * that doesn't carry its own `rev` clears it, so Home Assistant can tell that
     * someone else changed the configuration.
     *
     * @throws IllegalArgumentException when a value is invalid.
     */
    fun merge(update: JSONObject, strict: Boolean = true): Config {
        var c = this
        for (key in update.keys()) {
            c = when (key) {
                "device_name" -> c.copy(deviceName = update.getString(key).trim().take(64))
                "ha_url" -> c.copy(haUrl = normalizeBaseUrl(update.getString(key)))
                "ha_token" -> c.copy(haToken = update.getString(key).trim())
                "dashboards" -> c.copy(dashboards = parseDashboards(update.getJSONArray(key)))
                "home_dashboard" -> c.copy(homeDashboard = normalizeTarget(update.getString(key)))
                "kiosk" -> c.copy(kiosk = update.getString(key).also {
                    require(it in KIOSK_MODES) { "kiosk must be one of ${KIOSK_MODES.joinToString()}" }
                })
                "zoom" -> c.copy(zoom = intIn(update, key, 25, 400))
                "keep_screen_on" -> c.copy(keepScreenOn = update.getBoolean(key))
                "launch_on_boot" -> c.copy(launchOnBoot = update.getBoolean(key))
                "reload_minutes" -> c.copy(reloadMinutes = intIn(update, key, 0, 7 * 24 * 60))
                "rotate_seconds" -> c.copy(rotateSeconds = intIn(update, key, 0, 24 * 60 * 60))
                "return_home_minutes" -> c.copy(returnHomeMinutes = intIn(update, key, 0, 24 * 60))
                "reopen_minutes" -> c.copy(reopenMinutes = intIn(update, key, 0, 24 * 60))
                "render_stream" -> c.copy(renderStream = update.getString(key).trim().also {
                    require(it.isEmpty() || isHttpUrl(it)) { "render_stream must start with http:// or https://" }
                })
                "home_playlist" -> c.copy(homePlaylist = parsePlaylist(update.getJSONArray(key)))
                "rev" -> c.copy(rev = update.getString(key).take(64))
                "ha_token_set" -> c // read-only, echoed back by clients that round-trip GET /api/config
                else -> if (strict) throw IllegalArgumentException("unknown config key: $key") else c
            }
        }
        // The token only ever goes to the Home Assistant it was made for: whoever points the TV
        // somewhere else has to bring a token for that place too.
        if (c.haToken.isNotEmpty() && !update.has("ha_token") && originOf(c.haUrl) != haOrigin) c = c.copy(haToken = "")
        return if (update.has("rev")) c else c.copy(rev = "")
    }

    companion object {
        const val KIOSK_OFF = "off"
        const val KIOSK_SIDEBAR = "sidebar"
        const val KIOSK_FULL = "full"
        private val KIOSK_MODES = listOf(KIOSK_OFF, KIOSK_SIDEBAR, KIOSK_FULL)

        /** Loads stored configuration, ignoring keys this version doesn't know. */
        fun fromJson(json: JSONObject): Config = Config().merge(json, strict = false)

        fun isHttpUrl(value: String): Boolean =
            value.startsWith("http://", ignoreCase = true) || value.startsWith("https://", ignoreCase = true)

        /** Home Assistant serves these itself, outside its frontend: files in www, HACS, the API. */
        private val SERVED_PATHS = listOf("/local/", "/hacsfiles/", "/api/", "/auth/", "/static/", "/media/")

        /** A page of Home Assistant's frontend, which it can switch to without a page load. */
        fun isFrontendPath(path: String): Boolean = SERVED_PATHS.none { path.startsWith(it) }

        /** Path, query and fragment of a URL, e.g. `/lovelace/0?edit=1`. */
        fun pathOf(url: String): String {
            val uri = runCatching { URI(url) }.getOrNull() ?: return "/"
            val path = uri.rawPath.orEmpty().ifEmpty { "/" }
            val query = uri.rawQuery?.let { "?$it" }.orEmpty()
            val fragment = uri.rawFragment?.let { "#$it" }.orEmpty()
            return path + query + fragment
        }

        fun originOf(url: String): String? {
            val uri = runCatching { URI(url) }.getOrNull() ?: return null
            val scheme = uri.scheme?.lowercase() ?: return null
            val host = uri.host?.lowercase() ?: return null
            if (scheme != "http" && scheme != "https") return null
            val port = uri.port
            val defaultPort = if (scheme == "https") 443 else 80
            return if (port == -1 || port == defaultPort) "$scheme://$host" else "$scheme://$host:$port"
        }

        /** Validates a Home Assistant base URL and strips any path, e.g. `http://ha.local:8123`. */
        fun normalizeBaseUrl(raw: String): String {
            val value = raw.trim()
            if (value.isEmpty()) return ""
            require(isHttpUrl(value)) { "ha_url must start with http:// or https://" }
            val uri = runCatching { URI(value) }.getOrNull()
            require(uri?.host != null) { "ha_url is not a valid URL" }
            val port = if (uri!!.port == -1) "" else ":${uri.port}"
            val host = if (uri.host.contains(':') && !uri.host.startsWith("[")) "[${uri.host}]" else uri.host
            return "${uri.scheme.lowercase()}://$host$port"
        }

        fun normalizePath(raw: String): String {
            val value = raw.trim()
            if (value.isEmpty()) return "/"
            return if (value.startsWith("/")) value else "/$value"
        }

        /** A dashboard target is either an absolute http(s) URL or a path on Home Assistant. */
        fun normalizeTarget(raw: String): String {
            val value = raw.trim()
            if (value.isEmpty()) return ""
            return if (isHttpUrl(value)) value else normalizePath(value)
        }

        private fun parseDashboards(array: JSONArray): List<Dashboard> {
            require(array.length() <= 50) { "at most 50 dashboards are supported" }
            return (0 until array.length()).map { i ->
                val item = array.getJSONObject(i)
                val path = normalizeTarget(item.optString("path"))
                require(path.isNotEmpty()) { "dashboard $i has no path" }
                Dashboard(item.optString("title").trim().ifEmpty { path }.take(64), path)
            }
        }

        private fun parsePlaylist(array: JSONArray): List<PlaylistItem> {
            require(array.length() <= 500) { "a playlist has at most 500 items" }
            return (0 until array.length()).map { i ->
                val item = array.getJSONObject(i)
                val url = item.optString("url").trim()
                require(isHttpUrl(url)) { "playlist item $i needs an http(s) url" }
                val kind = item.optString("kind", PlaylistItem.VIDEO)
                require(kind in PlaylistItem.KINDS) { "playlist item $i: kind must be video, audio or image" }
                val seconds = item.optInt("seconds", 10)
                require(seconds in 1..86_400) { "playlist item $i: seconds must be from 1 to 86400" }
                PlaylistItem(url, kind, seconds)
            }
        }

        private fun intIn(json: JSONObject, key: String, min: Int, max: Int): Int {
            val value = json.get(key)
            val number = when (value) {
                is Number -> value.toDouble()
                is String -> value.trim().toDoubleOrNull()
                else -> null
            } ?: throw IllegalArgumentException("$key must be a number")
            require(number >= min && number <= max) { "$key must be between $min and $max" }
            return number.toInt()
        }
    }
}
