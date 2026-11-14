package com.hustlesman.pleatview

import org.json.JSONArray
import org.json.JSONException
import org.json.JSONObject
import java.io.OutputStream
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

/**
 * Requests name the screen by its address, not another site's name: a web page elsewhere that
 * points its own name at the screen (DNS rebinding) is refused (docs/protocol.md).
 */
fun hostAllowed(value: String?): Boolean {
    var host = value?.trim()?.lowercase() ?: return true
    if (host.isEmpty()) return true // HTTP/1.0 clients send none; browsers always do
    host = when {
        host.startsWith("[") -> host.substring(1, host.indexOf(']').takeIf { it > 0 } ?: host.length)
        host.count { it == ':' } == 1 -> host.substringBeforeLast(':')
        else -> host
    }
    if (host.matches(Regex("^[0-9.]+$")) && host.split('.').size == 4 && host.split('.').all { it.toIntOrNull() in 0..255 }) return true
    if (host.contains(':') && host.matches(Regex("^[0-9a-f:.]+$"))) return true
    host = host.trimEnd('.')
    return host == "localhost" || !host.contains('.') || LOCAL_SUFFIXES.any { host.endsWith(it) }
}

private val LOCAL_SUFFIXES = listOf(".local", ".lan", ".home", ".home.arpa", ".internal")

/**
 * Changes come from the screen's own pages or from programs, never from another site's page:
 * a browser names the page that sent a request (Origin), so one from elsewhere is refused, and
 * bodies are JSON (a page can't send that to another site without asking first). Even a page
 * shown on this very screen can't change its settings or pair it (docs/protocol.md).
 */
fun crossSite(method: String, origin: String?, host: String?, contentType: String?, bodySize: Int): Pair<Int, String>? {
    if (method !in setOf("POST", "PUT", "PATCH", "DELETE")) return null
    val from = origin?.trimEnd('/').orEmpty()
    if (from.isNotEmpty() && from != "http://${host.orEmpty()}") return 403 to "cross_site"
    if (bodySize > 0 && contentType?.lowercase()?.startsWith("application/json") != true) return 415 to "json_required"
    return null
}

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

        /** Someone asked to pair without the PIN: ask the person at the screen. */
        fun approvalRequested()

        /**
         * Protocol v2: connect out to the controller at [url], with a one-time [token] from one
         * paired over v1, or to show a code. [fingerprint] pins its own certificate.
         * @throws IllegalArgumentException for an address or fingerprint that can't be.
         */
        fun enrol(url: String, token: String?, fingerprint: String?): Unit = throw IllegalArgumentException("not supported")

        /** Upkeep's report (docs/protocol.md, "Upkeep"). */
        fun diagnostics(): JSONObject = throw HttpError(404, "not found")
    }

    private val openStreams = AtomicInteger()

    fun handle(request: HttpRequest): HttpResponse {
        if (!hostAllowed(request.header("host"))) return HttpResponse.error(421, "misdirected")
        crossSite(request.method, request.header("origin"), request.header("host"), request.header("content-type"), request.body.size)
            ?.let { (status, error) -> return HttpResponse.error(status, error) }
        val path = request.path.trimEnd('/').ifEmpty { "/" }
        return when (path) {
            "/", "/setup" -> get(request) { HttpResponse.html(setupPage()) }
            "/api/info" -> get(request) {
                HttpResponse.json(200, backend.info().put("paired", pairing.isPaired).put("pairing", JSONArray(listOf("pin", "approve"))))
            }
            "/api/pair" -> post(request) { pair(request) }
            // Pairing approved with OK on the screen instead of the PIN.
            "/api/pair/request" -> post(request) { requestApproval(request) }
            // Lets Home Assistant check that a TV announcing itself at a new address is the one
            // it paired with, before it sends that address its token.
            "/api/verify" -> post(request) { verify(request) }
            // The phone that scanned the TV's sign-in code has its PIN instead of a token.
            "/api/signin" -> post(request) { signIn(request) }
            // The phone page (with the PIN), or a paired client: connect out to a controller
            // elsewhere, which then shows a code to add the TV there with (protocol v2).
            "/api/connect" -> post(request) { connect(request) }
            else -> {
                if (path.startsWith("/api/pair/request/")) return get(request) { approvalState(path.substringAfterLast('/')) }
                if (!path.startsWith("/api/")) return HttpResponse.error(404, "not found")
                if (!pairing.isValid(request.bearerToken())) return HttpResponse.error(401, "unauthorized")
                when (path) {
                    "/api/state" -> get(request) { HttpResponse.json(200, backend.stateJson()) }
                    "/api/events" -> get(request) { events() }
                    "/api/diagnostics" -> get(request) { HttpResponse.json(200, backend.diagnostics()) }
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
                    // A controller paired over v1 moves this screen to protocol v2 (docs/protocol-v2.md).
                    "/api/enrol" -> post(request) { enrol(request) }
                    "/api/unpair" -> post(request) {
                        pairing.revoke(request.bearerToken().orEmpty())
                        ok()
                    }
                    else -> HttpResponse.error(404, "not found")
                }
            }
        }
    }

    /**
     * Protocol v2's calls (docs/protocol-v2.md): v1's endpoints for a controller that proved who
     * it is over its own connection, so no token. The response is what v1 would answer.
     */
    fun call(method: String, params: JSONObject): HttpResponse {
        val request = HttpRequest("POST", "/", emptyMap(), emptyMap(), params.toString().toByteArray(), null)
        return try {
            when (method) {
                "info" -> HttpResponse.json(200, backend.info().put("paired", pairing.isPaired).put("pairing", JSONArray(listOf("pin", "approve"))))
                "state" -> HttpResponse.json(200, backend.stateJson())
                "command" -> command(request)
                "config" -> HttpResponse.json(200, backend.config().toJson())
                "set_config" -> updateConfig(request)
                "diagnostics" -> HttpResponse.json(200, backend.diagnostics())
                "signins" -> HttpResponse.json(200, JSONObject().put("sign_ins", backend.signIns()))
                "forget_signins" -> HttpResponse.json(
                    200, JSONObject().put("forgotten", backend.forgetSignIns(params.optString("site").trim().ifEmpty { null })),
                )
                "signin" -> {
                    val password = params.optString("password")
                    when {
                        password.isEmpty() -> HttpResponse.error(400, "'password' is required")
                        backend.submitSignIn(params.optString("username").trim(), password, params.optBoolean("remember", true)) -> ok()
                        else -> HttpResponse.error(409, "no_sign_in_pending")
                    }
                }
                else -> HttpResponse.error(400, "unknown method $method")
            }
        } catch (e: HttpError) {
            HttpResponse.error(e.status, e.message ?: "bad request")
        }
    }

    private fun connect(request: HttpRequest): HttpResponse {
        val body = request.jsonBody()
        pinOrToken(request, body)?.let { return it }
        val url = body.optString("url")
        if (url.isEmpty()) return HttpResponse.error(400, "'url' is required")
        return try {
            backend.enrol(url, null, null)
            HttpResponse.json(202, JSONObject())
        } catch (e: IllegalArgumentException) {
            HttpResponse.error(400, e.message ?: "invalid")
        }
    }

    /** Null when the request has a paired token or the right PIN; otherwise the refusal. */
    private fun pinOrToken(request: HttpRequest, body: JSONObject): HttpResponse? {
        if (pairing.isValid(request.bearerToken())) return null
        return when (val result = pairing.verify(body.optString("pin"))) {
            Pairing.Result.Verified -> null
            is Pairing.Result.LockedOut ->
                HttpResponse.error(429, "too_many_attempts", mapOf("retry_after" to result.retryAfterSeconds))
            else -> HttpResponse.error(403, "invalid_pin")
        }
    }

    private fun enrol(request: HttpRequest): HttpResponse {
        val body = request.jsonBody()
        val url = body.optString("url")
        val token = body.optString("token")
        val fingerprint = if (body.isNull("fingerprint")) null else body.optString("fingerprint").ifEmpty { null }
        if (url.isEmpty() || token.isEmpty()) return HttpResponse.error(400, "'url' and 'token' are required")
        return try {
            backend.enrol(url, token, fingerprint)
            HttpResponse.json(202, JSONObject())
        } catch (e: IllegalArgumentException) {
            HttpResponse.error(400, e.message ?: "invalid")
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

    private fun requestApproval(request: HttpRequest): HttpResponse {
        val address = request.remote?.hostAddress ?: "?"
        val approval = pairing.requestApproval(request.jsonBody().optString("client", "client"), address)
            ?: return HttpResponse.error(429, "busy", mapOf("retry_after" to 30))
        backend.approvalRequested()
        val left = ((approval.expires - System.currentTimeMillis()) / 1000).coerceAtLeast(1)
        return HttpResponse.json(202, JSONObject().put("request", approval.id).put("expires_in", left))
    }

    private fun approvalState(id: String): HttpResponse {
        val (state, token) = pairing.collect(id) ?: return HttpResponse.error(404, "no_such_request")
        val body = JSONObject().put("state", state)
        if (token != null) body.put("token", token)
        return HttpResponse.json(200, body)
    }

    private fun verify(request: HttpRequest): HttpResponse {
        val nonce = request.jsonBody().optString("nonce")
        if (!NONCE.matches(nonce)) return HttpResponse.error(400, "'nonce' must be 32 to 128 hex digits")
        return HttpResponse.json(200, JSONObject().put("proofs", JSONArray(pairing.prove(nonce))))
    }

    private fun signIn(request: HttpRequest): HttpResponse {
        val body = request.jsonBody()
        pinOrToken(request, body)?.let { return it }
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
