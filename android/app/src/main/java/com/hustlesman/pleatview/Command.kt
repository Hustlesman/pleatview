package com.hustlesman.pleatview

import org.json.JSONObject
import java.util.Locale

class BadCommand(message: String) : IllegalArgumentException(message)

/** A validated instruction from Home Assistant (or the setup page). See docs/protocol.md. */
sealed class Command {
    /** Commands that should bring PleatView to the front when it's in the background. */
    open val needsDisplay: Boolean get() = false

    data class ShowDashboard(val path: String, val duration: Int?) : Command() {
        override val needsDisplay get() = true
    }

    /** A web page or an image (including MJPEG streams) on top of the dashboard. */
    data class ShowWeb(val url: String, val kind: WebKind, val layout: Layout, val duration: Int?) : Command() {
        override val needsDisplay get() = true
    }

    data class PlayMedia(
        val url: String,
        val mimeType: String?,
        val title: String?,
        val artist: String?,
        val imageUrl: String?,
        val layout: Layout,
        val duration: Int?,
        val loop: Boolean,
        val announce: Boolean,
        val volume: Float?,
        val startPosition: Double?,
        /** A camera's picture stream (MJPEG), shown instead when its video won't play. */
        val fallbackUrl: String? = null,
    ) : Command() {
        override val needsDisplay get() = !announce
        val isAudio get() = layout.mode == Mode.BACKGROUND
    }

    data class Notify(
        val title: String?,
        val message: String,
        val imageUrl: String?,
        val duration: Int,
        val position: Position,
    ) : Command()

    data class Seek(val position: Double) : Command()
    data class SetVolume(val level: Float) : Command()
    data class Mute(val muted: Boolean) : Command()

    object Home : Command() {
        override val needsDisplay get() = true
    }

    object TurnOn : Command() {
        override val needsDisplay get() = true
    }

    object TurnOff : Command()
    object Reload : Command()

    /** Forgets the sign-ins saved for [site], or all of them. */
    data class ForgetSignIns(val site: String?) : Command()
    object Play : Command()
    object Pause : Command()
    object Stop : Command()
    object NextDashboard : Command()
    object PreviousDashboard : Command()

    enum class WebKind { PAGE, IMAGE }
    enum class Mode(val wire: String) { FULLSCREEN("fullscreen"), PIP("pip"), BACKGROUND("background") }
    enum class Position(val wire: String) {
        TOP_LEFT("top-left"), TOP_RIGHT("top-right"), BOTTOM_LEFT("bottom-left"),
        BOTTOM_RIGHT("bottom-right"), CENTER("center"),
    }

    data class Layout(val mode: Mode, val position: Position = Position.BOTTOM_RIGHT, val size: Int = 35)

    companion object {
        private const val MAX_DURATION = 24 * 60 * 60
        private val AUDIO_EXT = setOf("mp3", "aac", "m4a", "flac", "ogg", "oga", "opus", "wav", "wma")
        private val IMAGE_EXT = setOf("jpg", "jpeg", "png", "gif", "webp", "bmp", "svg", "avif", "heic")

        fun parse(json: JSONObject): Command {
            val name = json.optString("command").ifEmpty { throw BadCommand("missing 'command'") }
            return when (name) {
                "show_dashboard" -> ShowDashboard(
                    path = Config.normalizeTarget(requireString(json, "path")),
                    duration = duration(json),
                )
                "show_url" -> ShowWeb(httpUrl(json, "url"), WebKind.PAGE, layout(json, Mode.FULLSCREEN), duration(json))
                "show_image" -> ShowWeb(httpUrl(json, "url"), WebKind.IMAGE, layout(json, Mode.FULLSCREEN), duration(json))
                "play_media" -> parsePlayMedia(json)
                "notify" -> Notify(
                    title = optString(json, "title", 200),
                    message = requireString(json, "message").take(2000),
                    imageUrl = optHttpUrl(json, "image_url"),
                    duration = duration(json)?.takeIf { it > 0 } ?: 10,
                    position = position(json, Position.TOP_RIGHT),
                )
                "seek" -> Seek(number(json, "position", 0.0, Double.MAX_VALUE))
                "set_volume" -> SetVolume(number(json, "level", 0.0, 1.0).toFloat())
                "mute" -> Mute(boolean(json, "muted"))
                "home" -> Home
                "reload" -> Reload
                "forget_sign_ins" -> ForgetSignIns(json.optString("site").trim().ifEmpty { null })
                "play" -> Play
                "pause" -> Pause
                "stop" -> Stop
                "turn_on" -> TurnOn
                "turn_off" -> TurnOff
                "next_dashboard" -> NextDashboard
                "previous_dashboard" -> PreviousDashboard
                else -> throw BadCommand("unknown command '$name'")
            }
        }

        private fun parsePlayMedia(json: JSONObject): Command {
            val url = requireString(json, "url")
            val scheme = url.substringBefore(':', "").lowercase(Locale.ROOT)
            if (scheme !in setOf("http", "https", "rtsp", "rtsps")) {
                throw BadCommand("url must be http(s) or rtsp(s)")
            }
            val mime = optString(json, "mime_type", 100)?.lowercase(Locale.ROOT)
            val kind = mediaKind(url, mime)
            val announce = json.optBoolean("announce", false)
            if (kind == MediaKind.IMAGE && !announce) {
                if (scheme != "http" && scheme != "https") throw BadCommand("images must be http(s)")
                return ShowWeb(url, WebKind.IMAGE, layout(json, Mode.FULLSCREEN), duration(json))
            }
            val defaultMode = if (kind == MediaKind.AUDIO) Mode.BACKGROUND else Mode.FULLSCREEN
            return PlayMedia(
                url = url,
                mimeType = mime,
                title = optString(json, "title", 300),
                artist = optString(json, "artist", 300),
                imageUrl = optHttpUrl(json, "image_url"),
                layout = layout(json, defaultMode),
                duration = duration(json),
                loop = json.optBoolean("loop", false),
                announce = announce,
                volume = if (json.has("volume")) number(json, "volume", 0.0, 1.0).toFloat() else null,
                startPosition = if (json.has("start_position")) number(json, "start_position", 0.0, Double.MAX_VALUE) else null,
                fallbackUrl = optHttpUrl(json, "fallback_url"),
            )
        }

        enum class MediaKind { AUDIO, VIDEO, IMAGE }

        fun mediaKind(url: String, mime: String?): MediaKind {
            if (mime != null) {
                when {
                    mime.startsWith("audio/") -> return MediaKind.AUDIO
                    mime.startsWith("video/") -> return MediaKind.VIDEO
                    mime.startsWith("image/") || mime.startsWith("multipart/x-mixed-replace") -> return MediaKind.IMAGE
                    mime.contains("mpegurl") || mime.contains("dash+xml") -> return MediaKind.VIDEO
                }
            }
            val ext = url.substringBefore('?').substringBefore('#').substringAfterLast('/')
                .substringAfterLast('.', "").lowercase(Locale.ROOT)
            return when (ext) {
                in AUDIO_EXT -> MediaKind.AUDIO
                in IMAGE_EXT -> MediaKind.IMAGE
                else -> MediaKind.VIDEO
            }
        }

        private fun layout(json: JSONObject, default: Mode): Layout {
            val mode = when (val raw = json.optString("mode")) {
                "" -> default
                else -> Mode.entries.firstOrNull { it.wire == raw } ?: throw BadCommand("unknown mode '$raw'")
            }
            val size = if (json.has("size")) number(json, "size", 10.0, 100.0).toInt() else 35
            return Layout(mode, position(json, Position.BOTTOM_RIGHT), size)
        }

        private fun position(json: JSONObject, default: Position): Position {
            val raw = json.optString("position")
            if (raw.isEmpty()) return default
            return Position.entries.firstOrNull { it.wire == raw } ?: throw BadCommand("unknown position '$raw'")
        }

        private fun duration(json: JSONObject): Int? {
            if (!json.has("duration") || json.isNull("duration")) return null
            return number(json, "duration", 0.0, MAX_DURATION.toDouble()).toInt().takeIf { it > 0 }
        }

        private fun requireString(json: JSONObject, key: String): String {
            val value = json.opt(key)
            if (value !is String || value.isBlank()) throw BadCommand("'$key' is required")
            return value.trim()
        }

        private fun optString(json: JSONObject, key: String, max: Int): String? {
            if (!json.has(key) || json.isNull(key)) return null
            return json.optString(key).trim().take(max).ifEmpty { null }
        }

        private fun httpUrl(json: JSONObject, key: String): String {
            val url = requireString(json, key)
            if (!Config.isHttpUrl(url)) throw BadCommand("'$key' must be an http(s) URL")
            return url
        }

        private fun optHttpUrl(json: JSONObject, key: String): String? {
            val url = optString(json, key, 4096) ?: return null
            if (!Config.isHttpUrl(url)) throw BadCommand("'$key' must be an http(s) URL")
            return url
        }

        private fun number(json: JSONObject, key: String, min: Double, max: Double): Double {
            val value = when (val raw = json.opt(key)) {
                is Number -> raw.toDouble()
                is String -> raw.trim().toDoubleOrNull()
                else -> null
            } ?: throw BadCommand("'$key' must be a number")
            if (value.isNaN() || value < min || value > max) {
                throw BadCommand(
                    if (max == Double.MAX_VALUE) "'$key' must be at least ${fmt(min)}"
                    else "'$key' must be between ${fmt(min)} and ${fmt(max)}",
                )
            }
            return value
        }

        private fun fmt(value: Double) = if (value % 1.0 == 0.0) value.toLong().toString() else value.toString()

        private fun boolean(json: JSONObject, key: String): Boolean {
            val raw = json.opt(key)
            if (raw !is Boolean) throw BadCommand("'$key' must be true or false")
            return raw
        }
    }
}
