package com.hustlesman.pleatview

import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.io.BufferedReader
import java.io.InputStreamReader
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.CopyOnWriteArrayList

/** Runs the real HTTP server and API routes against a fake backend. */
class ApiTest {
    private class FakeBackend : Api.Backend {
        val commands = CopyOnWriteArrayList<Command>()
        val listeners = CopyOnWriteArrayList<(String) -> Unit>()
        var state = """{"power":"on"}"""
        var config = Config()

        override fun info(): JSONObject = JSONObject().put("id", "abc").put("name", "Test TV")
        override fun stateJson() = state
        override fun addStateListener(listener: (String) -> Unit) {
            listeners += listener
        }
        override fun removeStateListener(listener: (String) -> Unit) {
            listeners -= listener
        }
        override fun dispatch(command: Command) {
            commands += command
        }
        override fun config() = config
        override fun updateConfig(update: JSONObject): Config = config.merge(update).also { config = it }

        val signIns = CopyOnWriteArrayList<Triple<String, String, Boolean>>()
        var signInPending = true
        var forgotten: String? = "nothing yet"

        override fun submitSignIn(username: String, password: String, remember: Boolean): Boolean {
            if (signInPending) signIns += Triple(username, password, remember)
            return signInPending
        }
        override fun signIns(): JSONArray =
            JSONArray().put(JSONObject().put("kind", "form").put("site", "http://grafana.lan:3000").put("username", "kiosk"))
        override fun forgetSignIns(site: String?): Int {
            forgotten = site
            return 1
        }
        var approvals = 0
        override fun approvalRequested() {
            approvals++
        }

        fun publish(json: String) {
            state = json
            listeners.forEach { it(json) }
        }

        val enrolments = CopyOnWriteArrayList<Triple<String, String?, String?>>()
        override fun enrol(url: String, token: String?, fingerprint: String?) {
            require(url.startsWith("wss://")) { "a controller's address is wss://host[:port]/api/pleatview/v2" }
            enrolments += Triple(url, token, fingerprint)
        }
    }

    private class MemoryStore : Pairing.Store {
        var json: String? = null
        override fun loadTokens() = json
        override fun saveTokens(json: String) {
            this.json = json
        }
    }

    private val backend = FakeBackend()
    private val pairing = Pairing(MemoryStore())
    private val api = Api(backend, pairing) { "<html>setup</html>".toByteArray() }
    private val server = HttpServer(0, api::handle)
    private lateinit var base: String

    @Before
    fun start() {
        server.start()
        base = "http://127.0.0.1:${server.localPort}"
    }

    @After
    fun stop() = server.stop()

    private data class Reply(val status: Int, val body: String) {
        val json get() = JSONObject(body)
    }

    private fun request(method: String, path: String, body: String? = null, token: String? = null): Reply {
        val connection = URL(base + path).openConnection() as HttpURLConnection
        connection.requestMethod = method
        connection.connectTimeout = 2000
        connection.readTimeout = 4000
        token?.let { connection.setRequestProperty("Authorization", "Bearer $it") }
        if (body != null) {
            connection.doOutput = true
            connection.setRequestProperty("Content-Type", "application/json")
            connection.outputStream.use { it.write(body.toByteArray()) }
        }
        val status = connection.responseCode
        val stream = if (status >= 400) connection.errorStream else connection.inputStream
        val text = stream?.bufferedReader()?.use { it.readText() }.orEmpty()
        connection.disconnect()
        return Reply(status, text)
    }

    private fun pair(): String =
        request("POST", "/api/pair", """{"pin":"${pairing.pin}","client":"test"}""").json.getString("token")

    @Test
    fun `pairing approved on the screen hands the token to the client that asked, once`() {
        assertEquals("[\"pin\",\"approve\"]", request("GET", "/api/info").json.getJSONArray("pairing").toString())
        val asked = request("POST", "/api/pair/request", """{"client":"Home Assistant"}""")
        assertEquals(202, asked.status)
        assertEquals(1, backend.approvals)
        val id = asked.json.getString("request")
        assertEquals("pending", request("GET", "/api/pair/request/$id").json.getString("state"))
        assertEquals(404, request("GET", "/api/pair/request/someone-else").status)
        val approval = pairing.pendingApproval!!
        assertEquals("Home Assistant", approval.client)
        pairing.decide(id, true)
        val done = request("GET", "/api/pair/request/$id").json
        assertEquals("approved", done.getString("state"))
        assertEquals(200, request("GET", "/api/state", token = done.getString("token")).status)
        assertEquals(404, request("GET", "/api/pair/request/$id").status) // collected once
    }

    @Test
    fun `info is public and reports pairing`() {
        val info = request("GET", "/api/info")
        assertEquals(200, info.status)
        assertEquals("Test TV", info.json.getString("name"))
        assertEquals(false, info.json.getBoolean("paired"))
        assertTrue(request("GET", "/").body.contains("setup"))
    }

    @Test
    fun `everything else needs a paired token`() {
        assertEquals(401, request("GET", "/api/state").status)
        assertEquals(401, request("GET", "/api/state", token = "forged").status)
        assertEquals(403, request("POST", "/api/pair", """{"pin":"x"}""").status)
        val token = pair()
        val state = request("GET", "/api/state", token = token)
        assertEquals(200, state.status)
        assertEquals("on", state.json.getString("power"))
        assertEquals(true, request("GET", "/api/info").json.getBoolean("paired"))
    }

    @Test
    fun `verify is public and proves the pairing`() {
        assertEquals(400, request("POST", "/api/verify", """{"nonce":"short"}""").status)
        val nonce = "0123456789abcdef0123456789abcdef"
        assertEquals(0, request("POST", "/api/verify", """{"nonce":"$nonce"}""").json.getJSONArray("proofs").length())
        pair()
        val proofs = request("POST", "/api/verify", """{"nonce":"$nonce"}""").json.getJSONArray("proofs")
        assertEquals(pairing.prove(nonce), (0 until proofs.length()).map(proofs::getString))
    }

    @Test
    fun `commands are validated then dispatched`() {
        val token = pair()
        val ok = request("POST", "/api/command", """{"command":"show_dashboard","path":"/lovelace/1"}""", token)
        assertEquals(200, ok.status)
        assertEquals(listOf<Command>(Command.ShowDashboard("/lovelace/1", null)), backend.commands.toList())
        val bad = request("POST", "/api/command", """{"command":"set_volume","level":7}""", token)
        assertEquals(400, bad.status)
        assertTrue(bad.json.getString("error").contains("level"))
        assertEquals(400, request("POST", "/api/command", "not json", token).status)
        assertEquals(405, request("GET", "/api/command", token = token).status)
    }

    @Test
    fun `config round trip never returns the token`() {
        val token = pair()
        val saved = request("POST", "/api/config", """{"ha_url":"http://ha:8123/","ha_token":"llat","rev":"r1"}""", token)
        assertEquals(200, saved.status)
        assertEquals("http://ha:8123", saved.json.getString("ha_url"))
        assertEquals(true, saved.json.getBoolean("ha_token_set"))
        assertTrue(!saved.body.contains("llat"))
        assertEquals(400, request("POST", "/api/config", """{"zoom":5}""", token).status)
    }

    @Test
    fun `unpair revokes the calling token`() {
        val token = pair()
        assertEquals(200, request("POST", "/api/unpair", "", token).status)
        assertEquals(401, request("GET", "/api/state", token = token).status)
    }

    @Test(timeout = 10_000)
    fun `event stream sends current state then updates`() {
        val token = pair()
        val connection = URL("$base/api/events").openConnection() as HttpURLConnection
        connection.setRequestProperty("Authorization", "Bearer $token")
        connection.readTimeout = 5000
        assertEquals(200, connection.responseCode)
        assertTrue(connection.contentType.startsWith("text/event-stream"))
        val reader = BufferedReader(InputStreamReader(connection.inputStream))
        fun nextData(): String {
            while (true) {
                val line = reader.readLine() ?: error("stream closed")
                if (line.startsWith("data: ")) return line.removePrefix("data: ")
            }
        }
        assertEquals("""{"power":"on"}""", nextData())
        // Wait until the stream has registered its listener, then publish.
        val deadline = System.currentTimeMillis() + 3000
        while (backend.listeners.isEmpty() && System.currentTimeMillis() < deadline) Thread.sleep(10)
        backend.publish("""{"power":"standby"}""")
        assertEquals("""{"power":"standby"}""", nextData())
        connection.disconnect()
    }

    @Test
    fun `the pin from the TV's sign-in code works once`() {
        val pin = pairing.pin
        val body = """{"pin":"$pin","username":"kiosk","password":"s3cret"}"""
        assertEquals(200, request("POST", "/api/signin", body).status)
        assertEquals(listOf(Triple("kiosk", "s3cret", true)), backend.signIns.toList())
        assertEquals(403, request("POST", "/api/signin", body).status) // the PIN changed
        assertEquals(403, request("POST", "/api/signin", """{"username":"a","password":"b"}""").status)
    }

    @Test
    fun `a paired client signs in with its token, and only while a page asks`() {
        val token = pair()
        assertEquals(400, request("POST", "/api/signin", """{"username":"a"}""", token).status)
        assertEquals(200, request("POST", "/api/signin", """{"username":"a","password":"b","remember":false}""", token).status)
        assertEquals(Triple("a", "b", false), backend.signIns.last())
        backend.signInPending = false
        assertEquals(409, request("POST", "/api/signin", """{"username":"a","password":"b"}""", token).status)
    }

    @Test
    fun `saved sign-ins are listed without passwords and forgotten with a token`() {
        assertEquals(401, request("GET", "/api/signins").status)
        val token = pair()
        val list = request("GET", "/api/signins", token = token).json.getJSONArray("sign_ins")
        assertEquals("kiosk", list.getJSONObject(0).getString("username"))
        assertTrue(!list.toString().contains("password"))
        assertEquals(200, request("DELETE", "/api/signins", """{"site":"http://grafana.lan:3000"}""", token).status)
        assertEquals("http://grafana.lan:3000", backend.forgotten)
        request("DELETE", "/api/signins", token = token)
        assertEquals(null, backend.forgotten)
    }

    @Test
    fun `protocol v2 - a paired controller moves the screen over`() {
        val url = "wss://c.example/api/pleatview/v2"
        assertEquals(401, request("POST", "/api/enrol", """{"url":"$url","token":"t"}""").status)
        val token = pair()
        assertEquals(400, request("POST", "/api/enrol", """{"url":"$url"}""", token).status)
        assertEquals(400, request("POST", "/api/enrol", """{"url":"https://c.example/","token":"t"}""", token).status)
        assertEquals(202, request("POST", "/api/enrol", """{"url":"$url","token":"t","fingerprint":"${"ab".repeat(32)}"}""", token).status)
        assertEquals(Triple(url, "t", "ab".repeat(32)), backend.enrolments.last())
    }

    @Test
    fun `protocol v2's calls are v1's, without a token`() {
        assertEquals("Test TV", JSONObject(String(api.call("info", JSONObject()).body)).getString("name"))
        val ok = api.call("command", JSONObject().put("command", "home"))
        assertEquals(200, ok.status)
        assertEquals(Command.Home, backend.commands.last())
        assertEquals(400, api.call("command", JSONObject().put("command", "explode")).status)
        assertEquals(400, api.call("pair", JSONObject()).status)
        assertEquals("unknown method pair", JSONObject(String(api.call("pair", JSONObject()).body)).getString("error"))
    }

    @Test
    fun `protocol v2 - the phone page connects the TV to a controller elsewhere with the PIN`() {
        val url = "wss://c.example/api/pleatview/v2"
        assertEquals(403, request("POST", "/api/connect", """{"url":"$url","pin":"000000"}""").status)
        assertEquals(400, request("POST", "/api/connect", """{"url":"https://c.example/","pin":"${pairing.pin}"}""").status)
        assertEquals(202, request("POST", "/api/connect", """{"url":"$url","pin":"${pairing.pin}"}""").status)
        assertEquals(Triple(url, null, null), backend.enrolments.last())
        assertEquals(202, request("POST", "/api/connect", """{"url":"$url"}""", pair()).status) // or a paired client
    }

    @Test
    fun `only the screen's own names - no DNS rebinding`() {
        for (host in listOf(null, "", "192.168.1.20", "192.168.1.20:8765", "[fe80::1]:8765", "[::1]", "localhost:8765", "tv",
            "tv.local", "tv.lan:8765", "tv.home.arpa", "tv.internal", "TV.LOCAL.")) assertTrue(host.toString(), hostAllowed(host))
        for (host in listOf("rebind.evil.example", "rebind.evil.example:8765", "192.168.1.20.nip.io", "localhost.evil.example")) {
            assertFalse(host, hostAllowed(host))
        }
    }
}
