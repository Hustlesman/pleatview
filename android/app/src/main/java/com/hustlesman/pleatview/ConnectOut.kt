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

    val url: String? get() = prefs.getString(URL, null)

    private val wake = LinkedBlockingQueue<Unit>()

    @Volatile
    private var token: String? = null

    @Volatile
    private var socket: WebSocketClient? = null

    @Volatile
    private var stopped = false

    @Volatile
    private var waitMs = FIRST_WAIT_MS

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
        prefs.edit().putString(URL, ProtocolV2.address(url)).putString(PIN, pin?.lowercase()).putBoolean(ENROLLED, false).apply()
        this.token = token
        waitMs = FIRST_WAIT_MS
        socket?.close(1000)
        wake.offer(Unit)
    }

    private fun forget() {
        prefs.edit().remove(URL).remove(PIN).remove(ENROLLED).apply()
        token = null
        setCode(null)
    }

    private fun setCode(value: Pair<String, String>?) {
        if (code == value) return
        code = value
        codeChanged()
    }

    private fun run() {
        while (!stopped) {
            if (url == null) {
                wake.take()
                continue
            }
            val closeCode = try {
                session()
            } catch (e: Exception) {
                Log.i(TAG, "Can't reach the controller at $url: ${e.message}")
                null
            }
            online = false
            when (closeCode) {
                ProtocolV2.UNKNOWN_KEY, ProtocolV2.REVOKED -> {
                    Log.i(TAG, "The controller removed this screen ($closeCode)")
                    forget()
                    continue
                }
                ProtocolV2.REPLACED -> {
                    wake.take()
                    continue
                }
                ProtocolV2.CODE_EXPIRED -> waitMs = FIRST_WAIT_MS
            }
            wake.poll((waitMs * (0.8 + Math.random() * 0.4)).toLong(), TimeUnit.MILLISECONDS)
            waitMs = minOf(waitMs * 2, LONGEST_WAIT_MS)
        }
    }

    /** One connection, from the challenge until it closes: its close code. */
    private fun session(): Int? {
        val address = url ?: return null
        val ws = connector(address, prefs.getString(PIN, null))
        socket = ws
        val pinger = Executors.newSingleThreadScheduledExecutor()
        val answers = Executors.newSingleThreadExecutor()
        var listener: ((String) -> Unit)? = null
        try {
            val challenge = ws.receive()
            val nonce = challenge.optString("nonce")
            if (challenge.optString("type") != "challenge" || !ProtocolV2.isHex64(nonce)) throw IOException("expected a challenge")
            val hello = JSONObject().put("signature", signer.sign(ProtocolV2.signedText(address, nonce))).put("info", info())
            if (prefs.getBoolean(ENROLLED, false)) {
                ws.send(hello.put("type", "auth").put("key", signer.keyId))
            } else {
                ws.send(hello.put("type", "enrol").put("key", signer.public).apply { token?.let { put("token", it) } })
            }
            while (true) { // a code first, perhaps, then welcome
                val message = ws.receive()
                when (message.optString("type")) {
                    "code" -> setCode(message.optString("code") to (URI(address).host ?: ""))
                    "ping" -> ws.send(JSONObject().put("type", "pong"))
                    "welcome" -> break
                }
            }
            if (!prefs.getBoolean(ENROLLED, false)) {
                prefs.edit().putBoolean(ENROLLED, true).apply()
                token = null
                Log.i(TAG, "Enrolled with the controller at $address")
            }
            setCode(null)
            online = true
            waitMs = FIRST_WAIT_MS
            ws.send(JSONObject().put("type", "state").put("state", JSONObject(stateJson())))
            val sendState: (String) -> Unit = { json ->
                try {
                    ws.send(JSONObject().put("type", "state").put("state", JSONObject(json)))
                } catch (_: IOException) {
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
                }
            }
        } catch (closed: WebSocketClient.Closed) {
            return closed.code
        } finally {
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

    private companion object {
        const val TAG = "PleatView"
        const val URL = "v2_url"
        const val PIN = "v2_pin"
        const val ENROLLED = "v2_enrolled"
        const val PING_MS = 20_000L
        const val SILENCE_MS = 60_000L
        const val FIRST_WAIT_MS = 2_000L
        const val LONGEST_WAIT_MS = 60_000L
    }
}
