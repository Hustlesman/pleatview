package com.hustlesman.pleatview

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class SignInsTest {
    private class MemoryStore : SignIns.Store {
        var value: String? = null
        override fun loadSignIns() = value
        override fun saveSignIns(value: String?) {
            this.value = value
        }
    }

    /** Stands in for the keystore: reversible, and visibly not plain text. */
    private object FlipVault : SignIns.Vault {
        override fun seal(plain: ByteArray) = ByteArray(plain.size) { (plain[it].toInt() xor 0x5A).toByte() }
        override fun open(sealed: ByteArray) = seal(sealed)
    }

    private val store = MemoryStore()
    private fun signIns() = SignIns(store, FlipVault)

    private val grafana = SignIn(SignIn.Kind.FORM, "http://grafana.lan:3000", "", "kiosk", "s3cret")
    private val router = SignIn(SignIn.Kind.PROMPT, "192.168.1.1", "Router", "admin", "hunter2")

    @Test
    fun `a sign-in is only offered to its own site`() {
        val signIns = signIns()
        signIns.save(grafana)
        signIns.save(router)
        assertEquals(grafana, signIns.find(SignIn.Kind.FORM, "http://grafana.lan:3000"))
        assertEquals(grafana, signIns.find(SignIn.Kind.FORM, "HTTP://GRAFANA.LAN:3000")) // hosts are case-insensitive
        assertNull(signIns.find(SignIn.Kind.FORM, "https://grafana.lan:3000")) // another scheme is another site
        assertNull(signIns.find(SignIn.Kind.FORM, "http://grafana.lan")) // and so is another port
        assertNull(signIns.find(SignIn.Kind.PROMPT, "http://grafana.lan:3000"))
        assertEquals(router, signIns.find(SignIn.Kind.PROMPT, "192.168.1.1", "Router"))
        assertNull(signIns.find(SignIn.Kind.PROMPT, "192.168.1.1", "Other realm"))
    }

    @Test
    fun `a password prompt's sign-in is offered only on the dashboard it was saved on`() {
        val signIns = signIns()
        val nas = SignIn(SignIn.Kind.PROMPT, "nas.lan", "NAS", "admin", "pw", dashboard = "https://nas.lan")
        signIns.save(nas)
        assertEquals(nas, signIns.find(SignIn.Kind.PROMPT, "nas.lan", "NAS", "https://nas.lan"))
        assertNull(signIns.find(SignIn.Kind.PROMPT, "nas.lan", "NAS", "http://nas.lan:9000"))
        assertNull(signIns.find(SignIn.Kind.PROMPT, "nas.lan", "NAS", ""))
        // Another dashboard on the same host keeps a sign-in of its own.
        signIns.save(nas.copy(password = "other", dashboard = "http://nas.lan:9000"))
        assertEquals(2, signIns.count)
        assertEquals("pw", signIns().find(SignIn.Kind.PROMPT, "nas.lan", "NAS", "https://nas.lan")!!.password)
    }

    @Test
    fun `a prompt sign-in from before 1_0_5 belongs to the first dashboard it's saved for`() {
        val sealed = """[{"kind":"prompt","site":"nas.lan","realm":"NAS","username":"admin","password":"pw"}]"""
        store.value = sealed.toByteArray().let(FlipVault::seal).joinToString("") { "%02x".format(it) }
        val signIns = signIns()
        val legacy = signIns.find(SignIn.Kind.PROMPT, "nas.lan", "NAS", "https://nas.lan")!!
        assertEquals("", legacy.dashboard)
        assertEquals(legacy, signIns.find(SignIn.Kind.PROMPT, "nas.lan", "NAS", "http://nas.lan:9000"))
        signIns.save(legacy.copy(dashboard = "https://nas.lan"))
        assertEquals(1, signIns.count)
        assertNull(signIns().find(SignIn.Kind.PROMPT, "nas.lan", "NAS", "http://nas.lan:9000"))
    }

    @Test
    fun `the phone page shows where the password goes`() {
        assertEquals("pve.lan:8006", SignInRequest(SignIn.Kind.FORM, "https://pve.lan:8006", "", "Proxmox").address)
        assertEquals("[fe80::1]:3000", SignInRequest(SignIn.Kind.FORM, "http://[fe80::1]:3000", "", "Grafana").address)
        assertEquals("192.168.1.1", SignInRequest(SignIn.Kind.PROMPT, "192.168.1.1", "Router", "Router").address)
    }

    @Test
    fun `sign-ins survive a restart and are stored sealed`() {
        signIns().save(grafana)
        assertFalse(store.value!!.contains("s3cret"))
        assertFalse(store.value!!.contains("kiosk"))
        assertEquals(grafana, signIns().find(SignIn.Kind.FORM, grafana.site))
    }

    @Test
    fun `saving again replaces the sign-in`() {
        val signIns = signIns()
        signIns.save(grafana)
        signIns.save(grafana.copy(password = "new"))
        assertEquals(1, signIns.count)
        assertEquals("new", signIns().find(SignIn.Kind.FORM, grafana.site)!!.password)
    }

    @Test
    fun `forgetting one site or everything`() {
        val signIns = signIns()
        signIns.save(grafana)
        signIns.save(router)
        assertEquals(1, signIns.forget("http://grafana.lan:3000"))
        assertEquals(1, signIns.count)
        assertEquals(1, signIns.forget())
        assertNull(store.value)
    }

    @Test
    fun `the description leaves out passwords`() {
        val signIns = signIns()
        signIns.save(grafana)
        val text = signIns.describe().toString()
        assertTrue(text.contains("kiosk"))
        assertFalse(text.contains("s3cret"))
    }

    @Test
    fun `a store that can't be opened starts empty`() {
        store.value = "zz-not-sealed"
        assertEquals(0, signIns().count)
    }

    @Test
    fun `the fill script checks the site and quotes its values`() {
        val script = SignIns.fillScript("http://grafana.lan:3000", "kiosk", "a\"b'c\\d")
        assertTrue(script.contains("location.origin !== origin"))
        assertTrue(script.contains("\"a\\\"b'c\\\\d\""))
    }

    @Test
    fun `a site's name comes from its page title`() {
        assertEquals("Grafana", SignIns.siteName("Grafana"))
        assertEquals("Proxmox Virtual Environment", SignIns.siteName("Login | Proxmox Virtual Environment"))
        assertEquals("Gitea", SignIns.siteName("Sign in · Gitea"))
        assertEquals("UniFi", SignIns.siteName("Sign in to UniFi"))
        assertEquals("Portainer", SignIns.siteName("Portainer - Login"))
        assertNull(SignIns.siteName("Login"))
        assertNull(SignIns.siteName("Please sign in"))
        assertNull(SignIns.siteName("http://192.168.1.5:3000/login"))
        assertNull(SignIns.siteName(""))
        assertNull(SignIns.siteName(null))
    }
}
