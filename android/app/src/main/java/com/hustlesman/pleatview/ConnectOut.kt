package com.hustlesman.pleatview

import android.content.SharedPreferences
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Log
import org.json.JSONException
import org.json.JSONObject
import java.io.IOException
import java.net.URI
import java.security.KeyPairGenerator
import java.security.KeyStore
import java.security.MessageDigest
import java.security.PrivateKey
import java.security.Signature
import java.security.spec.ECGenParameterSpec
import java.util.concurrent.Executors
import java.util.concurrent.RejectedExecutionException
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicLong

/** Protocol v2's parts every implementation agrees on (docs/protocol-v2.md, tests/fixtures/protocol-v2.json). */
object ProtocolV2 {
    const val SUBPROTOCOL = "pleatview.v2"
    const val PATH = "/api/pleatview/v2"
    const val UNKNOWN_KEY = 4401
    const val REVOKED = 4403
    const val CODE_EXPIRED = 4408
    const val REPLACED = 4409
    private val HEX64 = Regex("^[0-9a-f]{64}$")

    /** A controller's address as signatures name it: wss, host in lower case, no :443. */
    fun address(given: String): String {
        val uri = try {
            URI(given.trim())
        } catch (e: Exception) {
            throw IllegalArgumentException("a controller's address is wss://host[:port]/api/pleatview/v2")
        }
        require(uri.scheme?.lowercase() == "wss" && !uri.host.isNullOrEmpty()) { "a controller's address is wss://host[:port]/api/pleatview/v2" }
        val port = if (uri.port > 0 && uri.port != 443) ":${uri.port}" else ""
        return "wss://${uri.host.lowercase()}$port$PATH"
    }

    /**
     * An address as a person types it, on the TV or the phone page: a host (`screens.example.com`,
     * `host:8790`), `https://…` or `wss://…`. Always the controller's v2 endpoint.
     */
    fun typed(text: String): String {
        val given = text.trim()
        require(given.isNotEmpty()) { "Type the controller's address" }
        val withScheme = when {
            given.startsWith("wss://", ignoreCase = true) -> given
            given.startsWith("https://", ignoreCase = true) -> "wss://" + given.substring(8)
            given.contains("://") -> throw IllegalArgumentException("a controller's address is a host name, https:// or wss://")
            else -> "wss://$given"
        }
        return address(withScheme)
    }

    /**
     * The address this build was made with (Gradle's `pleatview.controller`, never the source):
     * kept encoded, so what scans apps for addresses doesn't list it. Empty in builds from the source.
     */
    fun builtIn(encoded: String = BuildConfig.CONTROLLER): String? =
        if (encoded.isEmpty()) null else runCatching { String(B64.decode(encoded).reversedArray()) }.getOrNull()?.takeIf { it.isNotBlank() }

    fun signedText(controller: String, nonce: String) = "pleatview-v2\n$controller\n$nonce"

    fun isHex64(text: String) = HEX64.matches(text)

    fun sha256Hex(data: ByteArray): String = MessageDigest.getInstance("SHA-256").digest(data).joinToString("") { "%02x".format(it) }
}

/**
 * Protocol v2: the screen's one connection out to a controller anywhere, which signs in with
 * the screen's own key and answers v1's calls ([Api.call]) over it. Its own thread; it keeps
 * coming back with growing waits until the controller removes the screen.
 */
class ConnectOut(
    private val prefs: SharedPreferences,
    private val signer: Signer,
    private val info: () -> JSONObject,
    private val call: (String, JSONObject) -> HttpResponse,
    private val stateJson: () -> String,
    private val addStateListener: ((String) -> Unit) -> Unit,
    private val removeStateListener: ((String) -> Unit) -> Unit,
    private val codeChanged: () -> Unit,
    private val connector: (String, String?) -> WebSocketClient = { url, pin -> WebSocketClient.connect(url, ProtocolV2.SUBPROTOCOL, pin) },
    private val firstWaitMs: Long = FIRST_WAIT_MS,
    private val clock: () -> Long = System::currentTimeMillis,
    private val builtInAddress: () -> String? = { ProtocolV2.builtIn() },
) {
    interface Signer {
        /** Base64 of the public key's DER SubjectPublicKeyInfo. */
        val public: String
        val keyId: String

        /** Base64 of the DER ECDSA-SHA256 signature over [text]. */
        fun sign(text: String): String
    }

    /** The code to show while a controller waits for it, and that controller's host. */
    @Volatile
    var code: Pair<String, String>? = null
        private set

    @Volatile
    var online = false
        private set

    /** The organisation that asks to be allowed, while someone at the screen decides (`confirm`). */
    @Volatile
    var asking: String? = null
        private set

    /** Why it wasn't added (declined, unanswered, gave up after an hour), until it connects again. */
    val notAdded: String? get() = prefs.getString(NOT_ADDED, null)

    val url: String? get() = prefs.getString(URL, null)

    /** The address this build came with, as signatures name it (null in builds without one). */
    val builtIn: String? get() = builtInAddress()?.let { runCatching { ProtocolV2.address(it) }.getOrNull() }

    /**
     * The controller's host, to show the person at the screen: never the built-in address's,
     * which stays unlisted (blank instead).
     */
    fun shownHost(address: String? = url): String =
        if (address == null || address == builtIn) "" else runCatching { URI(address).host }.getOrNull() ?: ""

    /** Added to a controller (it welcomed this screen): the organisation that was allowed, or "". */
    val addedTo: String? get() = if (url != null && prefs.getBoolean(ENROLLED, false)) prefs.getString(ORGANISATION, null) ?: "" else null

    private val wake = LinkedBlockingQueue<Unit>()

    @Volatile
    private var token: String? = null

    @Volatile
    private var socket: WebSocketClient? = null

    @Volatile
    private var stopped = false

    @Volatile
    private var waitMs = firstWaitMs

    /** The controller's own wait in seconds, from `retry` or a 503. */
    @Volatile
    var retryAfter: Int? = null
        private set

    /** A new address and its pin, until it lets this screen in (docs/protocol-v2.md, "Moving a controller"). */
    @Volatile
    var moving: Pair<String, String?>? = null
        private set
    private var moveTries = 0

    private var thread: Thread? = null

    fun start() {
        if (thread != null) return
        thread = Thread(::run, "pleatview-connect-out").apply {
            isDaemon = true
            start()
        }
    }

    fun stop() {
        stopped = true
        socket?.close(1001)
        wake.offer(Unit)
    }

    /** Connect to this controller: with a token from one paired over v1, or to show a code. */
    fun enrol(url: String, token: String? = null, pin: String? = null) {
        require(pin == null || ProtocolV2.isHex64(pin.lowercase())) { "fingerprint is the hex of the certificate's SHA-256" }
        prefs.edit().putString(URL, ProtocolV2.typed(url)).putString(PIN, pin?.lowercase()).putBoolean(ENROLLED, false)
            .putLong(SINCE, clock()).putInt(CODES, 0).remove(NOT_ADDED).remove(ORGANISATION).apply()
        this.token = token
        waitMs = firstWaitMs
        moving = null
        socket?.close(1000)
        wake.offer(Unit)
        codeChanged()
    }

    /** The person at the screen allows the organisation that asked, or doesn't. */
    fun decide(allow: Boolean) {
        val ws = socket ?: return
        val organisation = asking ?: return
        asking = null
        prefs.edit().apply { if (allow) putString(ORGANISATION, organisation) else putString(NOT_ADDED, DECLINED) }.apply()
        codeChanged()
        Thread({
            try {
                ws.send(JSONObject().put("type", if (allow) "confirmed" else "declined"))
            } catch (_: IOException) {
            }
        }, "pleatview-decide").start()
    }

    private fun forget() {
        prefs.edit().remove(URL).remove(PIN).remove(ENROLLED).remove(SINCE).remove(CODES).remove(ORGANISATION).apply()
        token = null
        asking = null
        setCode(null)
    }

    /** Not added: forget the controller, stop connecting, and say why until it connects again. */
    private fun giveUp(why: String) {
        Log.i(TAG, "Not added to the controller at $url: $why")
        forget()
        prefs.edit().putString(NOT_ADDED, why).apply()
        codeChanged()
    }

    /** Before it's added: no name, which often holds a person's; the rest once it's in. */
    private fun littleSaid(full: JSONObject): JSONObject {
        val out = JSONObject()
        for (key in BEFORE_ADDED) if (full.has(key)) out.put(key, full.get(key))
        return out
    }

    private fun setCode(value: Pair<String, String>?) {
        if (code == value) return
        code = value
        codeChanged()
    }

    /** The controller has a new address: try it, keeping the old one until it lets us in. */
    private fun move(message: JSONObject) {
        val address = try {
            ProtocolV2.address(message.optString("url"))
        } catch (e: IllegalArgumentException) {
            Log.w(TAG, "Ignoring a move to an address that isn't wss://")
            return
        }
        val pin = if (message.isNull("fingerprint")) null else message.optString("fingerprint").lowercase()
        if (pin != null && !ProtocolV2.isHex64(pin)) return
        if (address == url) return
        moving = address to pin
        moveTries = 0
        Log.i(TAG, "The controller asks this screen to use $address")
        socket?.close(1000)
    }

    /** A move ends: kept once the new address let this screen in, given up after three tries. */
    private fun moved(ok: Boolean) {
        val (address, pin) = moving ?: return
        if (ok) {
            Log.i(TAG, "The controller moved to $address")
            prefs.edit().putString(URL, address).putString(PIN, pin).apply()
        } else if (++moveTries >= MOVE_TRIES) {
            Log.w(TAG, "The controller's new address $address didn't let this screen in; staying at $url")
        } else {
            return
        }
        moving = null
    }

    private fun run() {
        while (!stopped) {
            if (url == null) {
                wake.take()
                continue
            }
            val wasMoving = moving
            val closeCode = try {
                session()
            } catch (busy: WebSocketClient.Busy) {
                retryAfter = busy.retryAfter
                null
            } catch (e: Exception) {
                Log.i(TAG, "Can't reach the controller at ${(moving?.first ?: url)}: ${e.message}")
                null
            }
            online = false
            when {
                // Whatever the new address said, 4401, 4403 and 4409 included, the old one stands.
                wasMoving != null && moving === wasMoving -> {
                    moved(false)
                    waitMs = firstWaitMs
                }
                moving != null && wasMoving == null -> continue // just told to move: go now
                notAdded != null && !prefs.getBoolean(ENROLLED, false) -> { // declined, or nobody answered
                    forget()
                    codeChanged()
                    continue
                }
                closeCode == ProtocolV2.UNKNOWN_KEY || closeCode == ProtocolV2.REVOKED -> {
                    Log.i(TAG, "The controller removed this screen ($closeCode)")
                    forget()
                    continue
                }
                closeCode == ProtocolV2.REPLACED -> {
                    wake.take()
                    continue
                }
                closeCode == ProtocolV2.CODE_EXPIRED -> waitMs = firstWaitMs
            }
            if (url != null && !prefs.getBoolean(ENROLLED, false) && token == null &&
                (prefs.getInt(CODES, 0) >= MOST_CODES || clock() - prefs.getLong(SINCE, clock()) > ENROL_HOUR_MS)
            ) {
                giveUp(GAVE_UP) // nobody added it in an hour: nothing keeps calling a controller
                continue
            }
            val retry = retryAfter
            val pauseMs = if (retry != null) {
                retryAfter = null
                (1000 * (1 + Math.random() * (minOf(retry, LONGEST_RETRY_S) - 1))).toLong()
            } else {
                (waitMs * (0.5 + Math.random() * 0.5)).toLong()
            }
            wake.poll(pauseMs, TimeUnit.MILLISECONDS)
            waitMs = minOf(waitMs * 2, LONGEST_WAIT_MS)
        }
    }

    /** One connection, from the challenge until it closes: its close code. */
    private fun session(): Int? {
        val target = moving
        val address = target?.first ?: url ?: return null
        val ws = connector(address, if (target != null) target.second else prefs.getString(PIN, null))
        socket = ws
        val pinger = Executors.newSingleThreadScheduledExecutor()
        val answers = Executors.newSingleThreadExecutor()
        var listener: ((String) -> Unit)? = null
        try {
            val challenge = ws.receive()
            val nonce = challenge.optString("nonce")
            if (challenge.optString("type") != "challenge" || !ProtocolV2.isHex64(nonce)) throw IOException("expected a challenge")
            val enrolled = prefs.getBoolean(ENROLLED, false)
            val hello = JSONObject().put("signature", signer.sign(ProtocolV2.signedText(address, nonce)))
                .put("info", if (enrolled) info() else littleSaid(info()))
            if (enrolled) {
                ws.send(hello.put("type", "auth").put("key", signer.keyId))
            } else {
                ws.send(hello.put("type", "enrol").put("key", signer.public).apply { token?.let { put("token", it) } })
            }
            var codeEnds = clock() + DEFAULT_CODE_MS
            while (true) { // a code first, perhaps, then welcome
                val message = ws.receive()
                when (message.optString("type")) {
                    "code" -> {
                        prefs.edit().putInt(CODES, prefs.getInt(CODES, 0) + 1).apply()
                        codeEnds = clock() + message.optInt("expires_in", 600).coerceIn(1, 3600) * 1000L
                        setCode(message.optString("code") to shownHost(address))
                    }
                    "confirm" -> { // the code was typed: the person at the screen allows it, or not
                        asking = message.optString("organisation").take(100).ifBlank { shownHost(address) }
                        codeChanged()
                        val asked = asking
                        pinger.schedule({
                            if (asking != null && asking === asked) { // unanswered for the code's time
                                asking = null
                                prefs.edit().putString(NOT_ADDED, UNANSWERED).apply()
                                codeChanged()
                                ws.close(1000)
                            }
                        }, maxOf(codeEnds - clock(), 1000L), TimeUnit.MILLISECONDS)
                    }
                    "ping" -> ws.send(JSONObject().put("type", "pong"))
                    "welcome" -> break
                }
            }
            asking = null
            if (target != null && moving === target) moved(true)
            if (!prefs.getBoolean(ENROLLED, false)) {
                prefs.edit().putBoolean(ENROLLED, true).remove(SINCE).remove(CODES).remove(NOT_ADDED).apply()
                token = null
                Log.i(TAG, "Enrolled with the controller at $address")
            }
            setCode(null)
            online = true
            waitMs = firstWaitMs
            ws.send(JSONObject().put("type", "state").put("state", JSONObject(stateJson())))
            // Called on the main thread, where Android allows no network (over TLS it stops the
            // app): sent from the session's own thread, in order.
            val sendState: (String) -> Unit = { json ->
                try {
                    pinger.execute {
                        try {
                            ws.send(JSONObject().put("type", "state").put("state", JSONObject(json)))
                        } catch (_: IOException) {
                        }
                    }
                } catch (_: RejectedExecutionException) { // the session just ended
                }
            }
            listener = sendState
            addStateListener(sendState)
            val heard = AtomicLong(System.currentTimeMillis())
            pinger.scheduleAtFixedRate({
                if (System.currentTimeMillis() - heard.get() > SILENCE_MS) {
                    ws.close(1001)
                } else {
                    try {
                        ws.send(JSONObject().put("type", "ping"))
                    } catch (_: IOException) {
                    }
                }
            }, PING_MS, PING_MS, TimeUnit.MILLISECONDS)
            while (true) {
                val message = ws.receive()
                heard.set(System.currentTimeMillis())
                when (message.optString("type")) {
                    "request" -> answers.execute { answer(ws, message) }
                    "ping" -> ws.send(JSONObject().put("type", "pong"))
                    "retry" -> {
                        val after = message.opt("after")
                        if (after is Int && after in 1..LONGEST_RETRY_S) retryAfter = after
                    }
                    "move" -> move(message)
                }
            }
        } catch (closed: WebSocketClient.Closed) {
            return closed.code
        } finally {
            if (asking != null) {
                asking = null // the controller stopped waiting: the question goes
                codeChanged()
            }
            listener?.let(removeStateListener)
            pinger.shutdownNow()
            answers.shutdownNow()
            socket = null
            ws.close()
        }
    }

    private fun answer(ws: WebSocketClient, request: JSONObject) {
        val reply = JSONObject().put("type", "response").put("id", request.opt("id"))
        try {
            val response = call(request.optString("method"), request.optJSONObject("params") ?: JSONObject())
            val body = if (response.body.isEmpty()) JSONObject() else JSONObject(String(response.body, Charsets.UTF_8))
            if (response.status < 300) reply.put("result", body) else reply.put("error", body.optString("error", "HTTP ${response.status}"))
        } catch (e: JSONException) {
            reply.put("error", "bad request")
        }
        try {
            ws.send(reply)
        } catch (_: IOException) {
        }
    }

    /** The key in the Android Keystore: made on the device, it never leaves it. */
    class KeystoreSigner : Signer {
        private val keyStore = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }

        init {
            if (!keyStore.containsAlias(ALIAS)) {
                KeyPairGenerator.getInstance(KeyProperties.KEY_ALGORITHM_EC, "AndroidKeyStore").apply {
                    initialize(
                        KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_SIGN)
                            .setAlgorithmParameterSpec(ECGenParameterSpec("secp256r1"))
                            .setDigests(KeyProperties.DIGEST_SHA256)
                            .build(),
                    )
                }.generateKeyPair()
            }
        }

        private val der: ByteArray = keyStore.getCertificate(ALIAS).publicKey.encoded
        override val public: String = B64.encode(der)
        override val keyId: String = ProtocolV2.sha256Hex(der)

        override fun sign(text: String): String {
            val signature = Signature.getInstance("SHA256withECDSA")
            signature.initSign(keyStore.getKey(ALIAS, null) as PrivateKey)
            signature.update(text.toByteArray())
            return B64.encode(signature.sign())
        }

        private companion object {
            const val ALIAS = "pleatview_v2"
        }
    }

    companion object {
        const val DECLINED = "declined"
        const val UNANSWERED = "unanswered"
        const val GAVE_UP = "gave_up"
        private const val TAG = "PleatView"
        private const val URL = "v2_url"
        private const val PIN = "v2_pin"
        private const val ENROLLED = "v2_enrolled"
        private const val SINCE = "v2_since"
        private const val CODES = "v2_codes"
        private const val NOT_ADDED = "v2_not_added"
        private const val ORGANISATION = "v2_organisation"
        private const val MOST_CODES = 6
        private const val ENROL_HOUR_MS = 3_600_000L
        private const val DEFAULT_CODE_MS = 600_000L
        private val BEFORE_ADDED = listOf("id", "manufacturer", "model", "app_version", "api_version", "features")
        private const val PING_MS = 20_000L
        private const val SILENCE_MS = 60_000L
        private const val FIRST_WAIT_MS = 2_000L
        private const val LONGEST_WAIT_MS = 60_000L
        private const val LONGEST_RETRY_S = 3600
        private const val MOVE_TRIES = 3
    }
}
