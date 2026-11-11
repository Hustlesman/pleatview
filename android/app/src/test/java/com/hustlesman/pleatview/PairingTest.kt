package com.hustlesman.pleatview

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

class PairingTest {
    private class MemoryStore : Pairing.Store {
        var json: String? = null
        override fun loadTokens() = json
        override fun saveTokens(json: String) {
            this.json = json
        }
    }

    private var now = 1_000_000L
    private val store = MemoryStore()
    private fun pairing() = Pairing(store, clock = { now })

    @Test
    fun `approvals wait for the screen, expire, and are refused for other addresses meanwhile`() {
        val pairing = pairing()
        val first = pairing.requestApproval("Home Assistant", "10.0.0.2")!!
        assertEquals(first, pairing.requestApproval("Home Assistant", "10.0.0.2")) // the same request
        assertEquals(null, pairing.requestApproval("Intruder", "10.0.0.9")) // one at a time
        pairing.decide(first.id, false)
        assertEquals("denied" to null, pairing.collect(first.id))
        val second = pairing.requestApproval("Home Assistant", "10.0.0.2")!!
        now += Pairing.APPROVAL_MS + 1
        assertEquals(null, pairing.pendingApproval)
        pairing.decide(second.id, true) // too late
        assertEquals("expired" to null, pairing.collect(second.id))
        assertFalse(pairing.isPaired)
        repeat(Pairing.MAX_APPROVALS_PER_MINUTE) { pairing.decide(pairing.requestApproval("x", "10.0.0.$it")?.id ?: "", false) }
        assertEquals(null, pairing.requestApproval("y", "10.0.0.99")) // a few a minute at most
    }

    @Test
    fun `correct pin issues a token and rotates the pin`() {
        val pairing = pairing()
        val pin = pairing.pin
        val result = pairing.pair(pin.chunked(3).joinToString(" "), "Home Assistant") as Pairing.Result.Paired
        assertTrue(pairing.isValid(result.token))
        assertFalse(pairing.isValid("nope"))
        assertNotEquals(pin, pairing.pin)
        assertTrue(pairing.isPaired)
        assertFalse(store.json!!.contains(result.token)) // only hashes are stored
        assertTrue(pairing().isValid(result.token)) // survives restarts
    }

    @Test
    fun `repeated failures lock pairing and change the pin`() {
        val pairing = pairing()
        val pin = pairing.pin
        val wrong = if (pin == "000000") "111111" else "000000"
        repeat(Pairing.MAX_FAILURES) { assertEquals(Pairing.Result.InvalidPin, pairing.pair(wrong, "x")) }
        assertNotEquals(pin, pairing.pin)
        assertTrue(pairing.pair(pairing.pin, "x") is Pairing.Result.LockedOut)
        now += Pairing.LOCKOUT_MS + 1
        assertTrue(pairing.pair(pairing.pin, "x") is Pairing.Result.Paired)
    }

    @Test
    fun `revoking removes only that token`() {
        val pairing = pairing()
        val a = (pairing.pair(pairing.pin, "a") as Pairing.Result.Paired).token
        val b = (pairing.pair(pairing.pin, "b") as Pairing.Result.Paired).token
        pairing.revoke(a)
        assertFalse(pairing.isValid(a))
        assertTrue(pairing.isValid(b))
        pairing.revokeAll()
        assertFalse(pairing.isValid(b))
    }

    @Test
    fun `verifying the pin uses it up without pairing a client`() {
        val pairing = pairing()
        val pin = pairing.pin
        assertEquals(Pairing.Result.Verified, pairing.verify(pin))
        assertNotEquals(pin, pairing.pin)
        assertFalse(pairing.isPaired)
        assertEquals(Pairing.Result.InvalidPin, pairing.verify(pin))
    }

    @Test
    fun `proofs answer a nonce for each paired client without revealing tokens`() {
        val pairing = pairing()
        val token = (pairing.pair(pairing.pin, "a") as Pairing.Result.Paired).token
        val nonce = "00112233445566778899aabbccddeeff"
        // What a client holding the token computes on its side.
        val mac = Mac.getInstance("HmacSHA256").apply { init(SecretKeySpec(Pairing.sha256(token).toByteArray(), "HmacSHA256")) }
        val expected = mac.doFinal(nonce.toByteArray()).joinToString("") { "%02x".format(it) }
        assertEquals(listOf(expected), pairing.prove(nonce))
        assertNotEquals(pairing.prove(nonce), pairing.prove("ffeeddccbbaa99887766554433221100"))
        assertFalse(pairing.prove(nonce).single().contains(token))
    }

    @Test
    fun `proof matches the protocol's test vector`() {
        store.json = """[{"hash":"${Pairing.sha256("token-1")}","client":"a","created":0}]"""
        assertEquals(
            listOf("cfd2cb0b0606646c50484f870216c1870afd7f225109845b6435fdfd40887c7e"),
            pairing().prove("00112233445566778899aabbccddeeff"),
        )
    }
}
