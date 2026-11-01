package com.hustlesman.pleatview

import org.json.JSONArray
import org.json.JSONException
import org.json.JSONObject
import java.io.OutputStream
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

/** Routes for the local control API. The wire format is documented in docs/protocol.md. */
class Api(
    private val backend: Backend,
    private val pairing: Pairing,
    private val setupPage: () -> ByteArray,
) {
    interface Backend {
        fun info(): JSONObject
        fun stateJson(): String
        fun addStateListener(listener: (String) -> Unit)
        fun removeStateListener(listener: (String) -> Unit)
        fun dispatch(command: Command)
        fun config(): Config

        /** @throws IllegalArgumentException for invalid values. */
        fun updateConfig(update: JSONObject): Config

        /** Passes a username and password to the page waiting for one; false when none is. */
        fun submitSignIn(username: String, password: String, remember: Boolean): Boolean
        fun signIns(): JSONArray
        fun forgetSignIns(site: String?): Int
    }

    private val openStreams = AtomicInteger()

    fun handle(request: HttpRequest): HttpResponse {
        val path = request.path.trimEnd('/').ifEmpty { "/" }
        return when (path) {
            "/", "/setup" -> get(request) { HttpResponse.html(setupPage()) }
            "/api/info" -> get(request) { HttpResponse.json(200, backend.info().put("paired", pairing.isPaired)) }
            "/api/pair" -> post(request) { pair(request) }
            // Lets Home Assistant check that a TV announcing itself at a new address is the one
            // it paired with, before it sends that address its token.
            "/api/verify" -> post(request) { verify(request) }
            // The phone that scanned the TV's sign-in code has its PIN instead of a token.
            "/api/signin" -> post(request) { signIn(request) }
            else -> {
                if (!path.startsWith("/api/")) return HttpResponse.error(404, "not found")
                if (!pairing.isValid(request.bearerToken())) return HttpResponse.error(401, "unauthorized")
                when (path) {
                    "/api/state" -> get(request) { HttpResponse.json(200, backend.stateJson()) }
                    "/api/events" -> get(request) { events() }
                    "/api/command" -> post(request) { command(request) }
                    "/api/config" -> when (request.method) {
                        "GET", "HEAD" -> HttpResponse.json(200, backend.config().toJson())
                        "POST" -> updateConfig(request)
                        else -> HttpResponse.error(405, "method not allowed")
                    }
                    "/api/signins" -> when (request.method) {
                        "GET", "HEAD" -> HttpResponse.json(200, JSONObject().put("sign_ins", backend.signIns()))
                        "DELETE" -> {
                            val site = request.jsonBody().optString("site").trim().ifEmpty { null }
                            HttpResponse.json(200, JSONObject().put("forgotten", backend.forgetSignIns(site)))
                        }
                        else -> HttpResponse.error(405, "method not allowed")
                    }
                    "/api/unpair" -> post(request) {
                        pairing.revoke(request.bearerToken().orEmpty())
                        ok()
                    }
                    else -> HttpResponse.error(404, "not found")
                }
            }
        }
    }

    private fun pair(request: HttpRequest): HttpResponse {
        val body = request.jsonBody()
        val pin = body.optString("pin")
        if (pin.isBlank()) return HttpResponse.error(400, "'pin' is required")
        return when (val result = pairing.pair(pin, body.optString("client", "client"))) {
            is Pairing.Result.Paired -> HttpResponse.json(200, JSONObject().put("token", result.token))
            Pairing.Result.Verified -> HttpResponse.error(500, "unexpected pairing result")
            Pairing.Result.InvalidPin -> HttpResponse.error(403, "invalid_pin")
            is Pairing.Result.LockedOut ->
                HttpResponse.error(429, "too_many_attempts", mapOf("retry_after" to result.retryAfterSeconds))
        }
    }

    private fun verify(request: HttpRequest): HttpResponse {
        val nonce = request.jsonBody().optString("nonce")
        if (!NONCE.matches(nonce)) return HttpResponse.error(400, "'nonce' must be 32 to 128 hex digits")
        return HttpResponse.json(200, JSONObject().put("proofs", JSONArray(pairing.prove(nonce))))
    }

    private fun signIn(request: HttpRequest): HttpResponse {
        val body = request.jsonBody()
        if (!pairing.isValid(request.bearerToken())) {
            when (val result = pairing.verify(body.optString("pin"))) {
                Pairing.Result.Verified -> Unit
                is Pairing.Result.LockedOut ->
                    return HttpResponse.error(429, "too_many_attempts", mapOf("retry_after" to result.retryAfterSeconds))
                else -> return HttpResponse.error(403, "invalid_pin")
            }
        }
        val password = body.optString("password")
        if (password.isEmpty()) return HttpResponse.error(400, "'password' is required")
        val accepted = backend.submitSignIn(body.optString("username").trim(), password, body.optBoolean("remember", true))
        return if (accepted) ok() else HttpResponse.error(409, "no_sign_in_pending")
    }

    private fun command(request: HttpRequest): HttpResponse {
        val command = try {
            Command.parse(request.jsonBody())
        } catch (e: BadCommand) {
            return HttpResponse.error(400, e.message ?: "bad command")
        }
        backend.dispatch(command)
        return ok()
    }

    private fun updateConfig(request: HttpRequest): HttpResponse {
        val config = try {
            backend.updateConfig(request.jsonBody())
        } catch (e: IllegalArgumentException) {
            return HttpResponse.error(400, e.message ?: "invalid config")
        } catch (e: JSONException) {
            return HttpResponse.error(400, e.message ?: "invalid config")
        }
        return HttpResponse.json(200, config.toJson())
    }

    /** Server-Sent Events: the full state on connect and after every change, pings in between. */
    private fun events(): HttpResponse {
        if (openStreams.incrementAndGet() > MAX_STREAMS) {
            openStreams.decrementAndGet()
            return HttpResponse.error(503, "too many event streams")
        }
        return HttpResponse(
            status = 200,
            contentType = "text/event-stream; charset=utf-8",
            headers = mapOf("X-Accel-Buffering" to "no"),
        ) { out ->
            val queue = LinkedBlockingQueue<String>()
            val listener: (String) -> Unit = { queue.offer(it) }
            backend.addStateListener(listener)
            try {
                out.write("retry: 3000\n\n".toByteArray())
                sendState(out, backend.stateJson())
                while (true) {
                    val next = queue.poll(PING_SECONDS, TimeUnit.SECONDS)
                    if (next == null) {
                        out.write(": ping\n\n".toByteArray())
                    } else {
                        // Coalesce bursts: only the newest snapshot matters.
                        var latest = next
                        while (true) latest = queue.poll() ?: break
                        sendState(out, latest)
                    }
                    out.flush()
                }
            } catch (_: InterruptedException) {
                // Server shutting down.
            } finally {
                backend.removeStateListener(listener)
                openStreams.decrementAndGet()
            }
        }
    }

    private fun sendState(out: OutputStream, json: String) {
        out.write("event: state\ndata: $json\n\n".toByteArray(Charsets.UTF_8))
        out.flush()
    }

    private inline fun get(request: HttpRequest, block: () -> HttpResponse) =
        if (request.method == "GET" || request.method == "HEAD") block() else HttpResponse.error(405, "method not allowed")

    private inline fun post(request: HttpRequest, block: () -> HttpResponse) =
        if (request.method == "POST") block() else HttpResponse.error(405, "method not allowed")

    private fun ok() = HttpResponse.json(200, JSONObject().put("ok", true))

    companion object {
        const val API_VERSION = 1
        private const val MAX_STREAMS = 8
        private const val PING_SECONDS = 20L
        private val NONCE = Regex("^[0-9a-f]{32,128}$")
    }
}
