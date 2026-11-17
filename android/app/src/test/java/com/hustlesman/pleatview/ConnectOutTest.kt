package com.hustlesman.pleatview

import android.content.SharedPreferences
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.io.DataInputStream
import java.io.File
import java.io.FilterOutputStream
import java.io.OutputStream
import java.net.ServerSocket
import java.net.Socket
import java.net.URI
import java.security.KeyFactory
import java.security.KeyPairGenerator
import java.security.PrivateKey
import java.security.PublicKey
import java.security.Signature
import java.security.spec.ECGenParameterSpec
import java.security.spec.PKCS8EncodedKeySpec
import java.security.spec.X509EncodedKeySpec
import java.util.Base64
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit

class ConnectOutTest {
    private val vectors = JSONObject(File("../../tests/fixtures/protocol-v2.json").readText())

    /** A key in memory, where the app's is in the Android Keystore. */
    private class SoftwareSigner(private val private: PrivateKey, publicKey: PublicKey) : ConnectOut.Signer {
        override val public: String = B64.encode(publicKey.encoded)
        override val keyId: String = ProtocolV2.sha256Hex(publicKey.encoded)
        override fun sign(text: String): String = B64.encode(
            Signature.getInstance("SHA256withECDSA").apply { initSign(private); update(text.toByteArray()) }.sign(),
        )

        companion object {
            fun fresh(): SoftwareSigner {
                val pair = KeyPairGenerator.getInstance("EC").apply { initialize(ECGenParameterSpec("secp256r1")) }.generateKeyPair()
                return SoftwareSigner(pair.private, pair.public)
            }
        }
    }

    private fun verifies(public: String, signature: String, text: String): Boolean {
        val key = KeyFactory.getInstance("EC").generatePublic(X509EncodedKeySpec(B64.decode(public)))
        return Signature.getInstance("SHA256withECDSA").apply { initVerify(key); update(text.toByteArray()) }.verify(B64.decode(signature))
    }

    @Test
    fun `addresses, the signed text and the key are the shared test vectors'`() {
        val addresses = vectors.getJSONArray("addresses")
        for (i in 0 until addresses.length()) {
            val case = addresses.getJSONObject(i)
            assertEquals(case.getString("address"), ProtocolV2.address(case.getString("given")))
        }
        val signed = vectors.getJSONObject("signed")
        assertEquals(signed.getString("text"), ProtocolV2.signedText(signed.getString("address"), signed.getString("nonce")))
        val key = vectors.getJSONObject("key")
        val pem = key.getString("private_pem").lines().filterNot { it.startsWith("-----") }.joinToString("")
        val private = KeyFactory.getInstance("EC").generatePrivate(PKCS8EncodedKeySpec(B64.decode(pem)))
        val public = KeyFactory.getInstance("EC").generatePublic(X509EncodedKeySpec(B64.decode(key.getString("public"))))
        val signer = SoftwareSigner(private, public)
        assertEquals(key.getString("public"), signer.public)
        assertEquals(key.getString("id"), signer.keyId)
        assertTrue(verifies(signer.public, signer.sign(signed.getString("text")), signed.getString("text")))
        val signatures = vectors.getJSONArray("signatures")
        for (i in 0 until signatures.length()) {
            val case = signatures.getJSONObject(i)
            val text = ProtocolV2.signedText(case.getString("address"), case.getString("nonce"))
            assertEquals(case.getBoolean("valid"), verifies(signer.public, case.getString("signature"), text))
        }
        try {
            ProtocolV2.address("https://controller.example/")
            fail("an https address isn't a controller's")
        } catch (_: IllegalArgumentException) {
        }
    }

    @Test
    fun `base64 matches the JVM's, both ways`() {
        for (size in 0..40) {
            val data = ByteArray(size) { (it * 37 + size).toByte() }
            if (size > 0) assertEquals(Base64.getEncoder().encodeToString(data), B64.encode(data))
            if (size > 0) assertTrue(data.contentEquals(B64.decode(Base64.getEncoder().encodeToString(data))))
        }
    }

    /** SharedPreferences in memory. */
    private class Prefs : SharedPreferences {
        val values = HashMap<String, Any?>()
        override fun getAll(): Map<String, *> = values
        override fun getString(key: String, defValue: String?) = values[key] as String? ?: defValue
        override fun getStringSet(key: String, defValues: Set<String>?) = defValues
        override fun getInt(key: String, defValue: Int) = values[key] as Int? ?: defValue
        override fun getLong(key: String, defValue: Long) = values[key] as Long? ?: defValue
        override fun getFloat(key: String, defValue: Float) = values[key] as Float? ?: defValue
        override fun getBoolean(key: String, defValue: Boolean) = values[key] as Boolean? ?: defValue
        override fun contains(key: String) = key in values
        override fun registerOnSharedPreferenceChangeListener(l: SharedPreferences.OnSharedPreferenceChangeListener) = Unit
        override fun unregisterOnSharedPreferenceChangeListener(l: SharedPreferences.OnSharedPreferenceChangeListener) = Unit
        override fun edit(): SharedPreferences.Editor = object : SharedPreferences.Editor {
            val changes = HashMap<String, Any?>()
            override fun putString(key: String, value: String?) = apply { changes[key] = value }
            override fun putStringSet(key: String, values: Set<String>?) = apply { changes[key] = values }
            override fun putInt(key: String, value: Int) = apply { changes[key] = value }
            override fun putLong(key: String, value: Long) = apply { changes[key] = value }
            override fun putFloat(key: String, value: Float) = apply { changes[key] = value }
            override fun putBoolean(key: String, value: Boolean) = apply { changes[key] = value }
            override fun remove(key: String) = apply { changes[key] = REMOVED }
            override fun clear() = apply { values.clear() }
            override fun commit(): Boolean {
                apply()
                return true
            }
            override fun apply() {
                synchronized(values) {
                    for ((k, v) in changes) if (v === REMOVED) values.remove(k) else values[k] = v
                }
            }
        }

        companion object {
            val REMOVED = Any()
        }
    }

    /** A controller's end of protocol v2 over a plain socket, one screen at a time. */
    private inner class FakeController(val keys: HashMap<String, String> = HashMap()) {
        val server = ServerSocket(0)
        val url = "wss://127.0.0.1:${server.localPort}/api/pleatview/v2"
        val hellos = LinkedBlockingQueue<JSONObject>()
        val messages = LinkedBlockingQueue<JSONObject>()
        @Volatile var closeWith: Int? = null
        /** Ask the person at the screen first, as this organisation ("Allowed on the screen"). */
        @Volatile var confirm: String? = null
        /** Hand out a code, then close with this (4408: it expired) instead of letting it in. */
        @Volatile var codeThenClose: Int? = null
        @Volatile var expiresIn = 600
        @Volatile var codesSent = 0
        @Volatile var socket: Socket? = null
        private val nonce = "ab".repeat(32)

        init {
            Thread {
                while (!server.isClosed) {
                    val s = try { server.accept() } catch (_: Exception) { return@Thread }
                    socket = s
                    try { serve(s) } catch (_: Exception) { }
                }
            }.apply { isDaemon = true }.start()
        }

        fun send(message: JSONObject) {
            val out = socket!!.getOutputStream()
            synchronized(out) { out.write(WebSocketClient.encode(WebSocketClient.TEXT, message.toString().toByteArray(), mask = false)); out.flush() }
        }

        private fun receive(input: DataInputStream): JSONObject {
            while (true) {
                val frame = WebSocketClient.readFrame(input)
                if (frame.opcode == WebSocketClient.TEXT) return JSONObject(String(frame.payload))
                if (frame.opcode == WebSocketClient.CLOSE) throw java.io.IOException("closed")
            }
        }

        private fun serve(s: Socket) {
            val raw = s.getInputStream()
            val head = StringBuilder()
            while (!head.endsWith("\r\n\r\n")) head.append(raw.read().toChar())
            val key = Regex("(?i)sec-websocket-key: *(\\S+)").find(head)!!.groupValues[1]
            s.getOutputStream().write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n" +
                "Sec-WebSocket-Accept: ${WebSocketClient.acceptKey(key)}\r\nSec-WebSocket-Protocol: pleatview.v2\r\n\r\n").toByteArray())
            val input = DataInputStream(raw)
            send(JSONObject().put("type", "challenge").put("nonce", nonce))
            val hello = receive(input)
            hellos.put(hello)
            closeWith?.let { code ->
                s.getOutputStream().write(WebSocketClient.encode(WebSocketClient.CLOSE, byteArrayOf((code shr 8).toByte(), code.toByte()), mask = false))
                s.close()
                return
            }
            val public = if (hello.getString("type") == "enrol") hello.getString("key") else keys.getValue(hello.getString("key"))
            assertTrue(verifies(public, hello.getString("signature"), ProtocolV2.signedText(ProtocolV2.address(url), nonce)))
            keys[ProtocolV2.sha256Hex(B64.decode(public))] = public
            if (hello.getString("type") == "enrol" && !hello.has("token")) {
                send(JSONObject().put("type", "code").put("code", "K7RM-W2XD").put("expires_in", expiresIn))
                codesSent++
                codeThenClose?.let { code ->
                    s.getOutputStream().write(WebSocketClient.encode(WebSocketClient.CLOSE, byteArrayOf((code shr 8).toByte(), code.toByte()), mask = false))
                    s.close()
                    return
                }
                confirm?.let { organisation ->
                    send(JSONObject().put("type", "confirm").put("organisation", organisation))
                    val answer = receive(input)
                    messages.put(answer)
                    if (answer.getString("type") != "confirmed") {
                        s.getOutputStream().write(WebSocketClient.encode(WebSocketClient.CLOSE, byteArrayOf((ProtocolV2.REVOKED shr 8).toByte(), ProtocolV2.REVOKED.toByte()), mask = false))
                        s.close()
                        return
                    }
                }
            }
            send(JSONObject().put("type", "welcome"))
            while (true) messages.put(receive(input))
        }

        fun next(type: String): JSONObject {
            while (true) {
                val message = messages.poll(5, TimeUnit.SECONDS) ?: fail("no $type") as Nothing
                if (message.getString("type") == type) return message
            }
        }
    }

    private val controller = FakeController()
    private val prefs = Prefs()
    private val calls = CopyOnWriteArrayList<Pair<String, String>>()
    private val listeners = CopyOnWriteArrayList<(String) -> Unit>()
    private val codes = CopyOnWriteArrayList<Pair<String, String>?>()
    private lateinit var connection: ConnectOut
    private val writers = CopyOnWriteArrayList<String>()

    /** A socket that notes which thread writes to it. */
    private inner class Watched(port: Int) : Socket("127.0.0.1", port) {
        override fun getOutputStream(): OutputStream {
            val real = super.getOutputStream()
            return object : FilterOutputStream(real) {
                override fun write(b: Int) {
                    writers += Thread.currentThread().name
                    real.write(b)
                }

                override fun write(b: ByteArray, off: Int, len: Int) {
                    writers += Thread.currentThread().name
                    real.write(b, off, len)
                }
            }
        }
    }

    private var now = 1_000_000L

    private fun newConnection(firstWaitMs: Long = 2000, builtIn: String? = null): ConnectOut = ConnectOut(
        prefs = prefs,
        signer = SoftwareSigner.fresh(),
        info = { JSONObject().put("id", "tv-1").put("name", "Den TV").put("app_version", "1.0.19") },
        call = { method, params ->
            calls += method to params.toString()
            if (params.optString("command") == "explode") HttpResponse.error(400, "unknown command 'explode'")
            else HttpResponse.json(200, JSONObject().put("ok", method))
        },
        stateJson = { """{"power":"on"}""" },
        addStateListener = { listeners += it },
        removeStateListener = { listeners -= it },
        codeChanged = { codes += connection.code },
        // The fake controller has no TLS: the tests check the protocol, the device the TLS.
        connector = { url, _ -> WebSocketClient.open(Watched(URI(url).port), URI(url), ProtocolV2.SUBPROTOCOL) },
        firstWaitMs = firstWaitMs,
        clock = { now },
        builtInAddress = { builtIn },
    ).also { connection = it }

    @After
    fun tearDown() {
        if (::connection.isInitialized) connection.stop()
        controller.server.close()
    }

    private fun until(check: () -> Boolean) {
        val end = System.currentTimeMillis() + 8000
        while (!check()) {
            if (System.currentTimeMillis() > end) fail("timed out")
            Thread.sleep(25)
        }
    }

    @Test
    fun `enrols with a code, answers calls, pushes state and signs in again`() {
        val c = newConnection()
        c.start()
        c.enrol(controller.url)
        until { c.online }
        assertTrue(("K7RM-W2XD" to "127.0.0.1") in codes) // shown while the controller waited
        assertNull(c.code) // gone once it's let in
        val hello = controller.hellos.take()
        assertEquals("enrol", hello.getString("type"))
        assertEquals("tv-1", hello.getJSONObject("info").getString("id"))
        assertFalse("no name before it's added", hello.getJSONObject("info").has("name"))
        assertFalse(hello.has("token"))
        assertEquals(true, prefs.values["v2_enrolled"])
        assertEquals("on", controller.next("state").getJSONObject("state").getString("power"))
        controller.send(JSONObject().put("type", "request").put("id", 1).put("method", "command").put("params", JSONObject().put("command", "home")))
        assertEquals("command", controller.next("response").getJSONObject("result").getString("ok"))
        controller.send(JSONObject().put("type", "request").put("id", 2).put("method", "command").put("params", JSONObject().put("command", "explode")))
        assertEquals("unknown command 'explode'", controller.next("response").getString("error"))
        // State changes come on Android's main thread, which may not touch the network.
        Thread({ listeners.forEach { it("""{"power":"standby"}""") } }, "main-thread").apply { start(); join() }
        assertEquals("standby", controller.next("state").getJSONObject("state").getString("power"))
        assertFalse("sent from the main thread", "main-thread" in writers)
        controller.socket!!.close() // dropped: back by itself, with its key
        val again = controller.hellos.poll(8, TimeUnit.SECONDS)!!
        assertEquals("auth", again.getString("type"))
        assertEquals("Den TV", again.getJSONObject("info").getString("name")) // all of it, once it's in
    }

    @Test
    fun `Connect pressed again while a code shows starts over, off the main thread`() {
        controller.confirm = "Acme" // the controller holds the connection open while it asks
        val c = newConnection()
        c.start()
        c.enrol(controller.url)
        until { c.asking != null }
        assertEquals("enrol", controller.hellos.take().getString("type"))
        // Connect is pressed on Android's main thread, where touching the network stops the app.
        Thread({ c.enrol(controller.url) }, "main-thread").apply { start(); join() }
        val again = controller.hellos.poll(8, TimeUnit.SECONDS)!!
        assertEquals("enrol", again.getString("type")) // a new code for the address given
        assertFalse("closed from the main thread", "main-thread" in writers)
    }

    @Test
    fun `allowed on the screen, asked, allowed and let in`() {
        controller.confirm = "Acme"
        val c = newConnection()
        c.start()
        c.enrol("127.0.0.1:${controller.server.localPort}") // a host, as typed on the TV
        until { c.asking != null }
        assertEquals("Acme", c.asking)
        c.decide(true)
        assertEquals("confirmed", controller.next("confirmed").getString("type"))
        until { c.online }
        assertNull(c.asking)
        assertNull(c.notAdded)
    }

    @Test
    fun `the built-in address is never shown, and who it was added to is`() {
        controller.confirm = "Acme"
        val c = newConnection(builtIn = controller.url)
        c.start()
        c.enrol(c.builtIn!!)
        until { c.asking != null }
        assertTrue("the code without the host", ("K7RM-W2XD" to "") in codes)
        assertEquals("", c.shownHost())
        assertNull(c.addedTo) // not yet
        c.decide(true)
        until { c.online }
        assertEquals("Acme", c.addedTo)
        c.enrol(controller.url) // connecting again: not added there yet
        assertNull(c.addedTo)
    }

    @Test
    fun `a typed address is shown, and an unnamed organisation is named by it unless built in`() {
        controller.confirm = ""
        val c = newConnection(builtIn = "wss://screens.example.com/api/pleatview/v2")
        c.start()
        c.enrol("127.0.0.1:${controller.server.localPort}")
        until { c.asking != null }
        assertEquals("127.0.0.1", c.asking)
        assertEquals("127.0.0.1", c.shownHost())
        assertTrue(("K7RM-W2XD" to "127.0.0.1") in codes)
        c.stop()
        val built = newConnection(builtIn = controller.url)
        built.start()
        built.enrol(controller.url) // the built-in one, typed: still not shown
        until { built.asking != null }
        assertEquals("", built.asking)
    }

    @Test
    fun `declined on the screen, refused, forgotten and said so`() {
        controller.confirm = "Acme"
        val c = newConnection()
        c.start()
        c.enrol(controller.url)
        until { c.asking != null }
        c.decide(false)
        assertEquals("declined", controller.next("declined").getString("type"))
        until { c.url == null }
        assertEquals(ConnectOut.DECLINED, c.notAdded)
        assertNull(c.code)
        c.enrol(controller.url) // connecting again clears it
        assertNull(c.notAdded)
    }

    @Test
    fun `unanswered within the code's time, it stops and says so`() {
        controller.confirm = "Acme"
        controller.expiresIn = 1
        val c = newConnection()
        c.start()
        c.enrol(controller.url)
        until { c.asking != null }
        until { c.notAdded == ConnectOut.UNANSWERED }
        until { c.url == null }
        assertNull(c.asking)
    }

    @Test
    fun `not for ever, after six codes or an hour it stops connecting`() {
        controller.codeThenClose = ProtocolV2.CODE_EXPIRED
        val c = newConnection(firstWaitMs = 20)
        c.start()
        c.enrol(controller.url)
        until { c.notAdded == ConnectOut.GAVE_UP }
        assertEquals(6, controller.codesSent)
        assertNull(c.url)
        // An hour since connecting, however few codes.
        controller.codesSent = 0
        c.enrol(controller.url)
        until { controller.codesSent >= 1 }
        now += 3_600_001
        until { c.notAdded == ConnectOut.GAVE_UP }
        assertTrue(controller.codesSent < 6)
    }

    @Test
    fun `addresses as typed, and the one built in`() {
        assertEquals("wss://screens.example.com/api/pleatview/v2", ProtocolV2.typed(" screens.example.com "))
        assertEquals("wss://screens.example.com:8790/api/pleatview/v2", ProtocolV2.typed("https://Screens.example.com:8790/anything"))
        assertEquals("wss://screens.example.com/api/pleatview/v2", ProtocolV2.typed("wss://screens.example.com/api/pleatview/v2"))
        for (bad in listOf("", "ftp://x", "http://screens.example.com")) assertThrows(IllegalArgumentException::class.java) { ProtocolV2.typed(bad) }
        val encoded = Base64.getEncoder().encodeToString("wss://screens.example.com/api/pleatview/v2".toByteArray().reversedArray())
        assertEquals("wss://screens.example.com/api/pleatview/v2", ProtocolV2.builtIn(encoded))
        assertNull(ProtocolV2.builtIn(""))
    }

    @Test
    fun `enrols with a token without a code, and forgets a controller that removed it`() {
        val c = newConnection()
        c.start()
        c.enrol(controller.url, token = "one-time")
        until { c.online }
        assertEquals("one-time", controller.hellos.take().getString("token"))
        assertTrue(codes.all { it == null })
        controller.closeWith = ProtocolV2.REVOKED
        controller.socket!!.close()
        until { prefs.values["v2_url"] == null }
        assertNull(c.url)
    }

    @Test
    fun `a move is kept once the new address lets it in`() {
        val c = newConnection()
        c.start()
        c.enrol(controller.url, token = "one-time")
        until { c.online }
        val moved = FakeController(controller.keys) // the same controller, at another address
        try {
            controller.send(JSONObject().put("type", "move").put("url", "https://elsewhere.example/api/pleatview/v2"))
            controller.send(JSONObject().put("type", "move").put("url", moved.url))
            assertEquals("auth", moved.hellos.poll(8, TimeUnit.SECONDS)!!.getString("type"))
            until { prefs.values["v2_url"] == ProtocolV2.address(moved.url) }
            assertNull(c.moving)
        } finally {
            moved.server.close()
        }
    }

    @Test
    fun `a move that doesn't let it in goes back, without forgetting the controller`() {
        val c = newConnection(firstWaitMs = 50)
        c.start()
        c.enrol(controller.url, token = "one-time")
        until { c.online }
        controller.hellos.take()
        val refusing = FakeController().apply { closeWith = ProtocolV2.REVOKED }
        try {
            controller.send(JSONObject().put("type", "move").put("url", refusing.url))
            until { refusing.hellos.size == 3 }
            assertEquals("auth", controller.hellos.poll(8, TimeUnit.SECONDS)!!.getString("type"))
            assertEquals(ProtocolV2.address(controller.url), prefs.values["v2_url"])
            assertEquals(true, prefs.values["v2_enrolled"])
            assertNull(c.moving)
        } finally {
            refusing.server.close()
        }
    }

    @Test
    fun `retry says how long to wait, and is ignored out of range`() {
        val c = newConnection()
        c.start()
        c.enrol(controller.url, token = "one-time")
        until { c.online }
        controller.send(JSONObject().put("type", "retry").put("after", 7200))
        controller.send(JSONObject().put("type", "retry").put("after", 1))
        until { c.retryAfter == 1 }
    }

    @Test
    fun `wrong addresses and fingerprints are refused`() {
        val c = newConnection()
        for (bad in listOf<() -> Unit>({ c.enrol("http://c.example/") }, { c.enrol("ftp://c.example/") }, { c.enrol(controller.url, pin = "not-hex") })) {
            try {
                bad()
                fail("expected a refusal")
            } catch (_: IllegalArgumentException) {
            }
        }
    }
}
