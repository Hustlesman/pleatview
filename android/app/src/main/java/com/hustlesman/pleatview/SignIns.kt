package com.hustlesman.pleatview

import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import org.json.JSONArray
import org.json.JSONObject
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * A dashboard page asking for a username and password: a browser password prompt
 * (HTTP basic auth, identified by host and realm) or a login form (identified by origin).
 */
data class SignInRequest(
    val kind: SignIn.Kind,
    val site: String,
    val realm: String,
    /** The dashboard's name, or the site's host for pages that aren't a dashboard. */
    val title: String,
    /** The last sign-in for this page didn't work. */
    val failed: Boolean = false,
) {
    /** Where the password goes, for the phone page: `nas.lan:5001`, or a password prompt's host. */
    val address: String get() = site.substringAfter("://")

    fun toJson(): JSONObject = JSONObject()
        .put("kind", kind.wire).put("title", title).put("site", site).put("failed", failed)
}

/**
 * A username and password remembered for one site. A password prompt names only its host, so
 * its sign-in also keeps the origin of the [dashboard] that asked, and is offered only there:
 * another port or scheme on the same host has to ask again. Sign-ins saved before 1.0.5 have
 * none; the first page they work on becomes theirs.
 */
data class SignIn(
    val kind: Kind,
    val site: String,
    val realm: String,
    val username: String,
    val password: String,
    val dashboard: String = "",
) {
    enum class Kind(val wire: String) { PROMPT("prompt"), FORM("form") }

    fun matches(kind: Kind, site: String, realm: String, dashboard: String = "") =
        this.kind == kind && this.site.equals(site, ignoreCase = true) && this.realm == realm &&
            (this.dashboard.isEmpty() || this.dashboard.equals(dashboard, ignoreCase = true))
}

/**
 * Sign-ins remembered on this TV and nowhere else. They are encrypted with a key that never
 * leaves the Android keystore, and each one is only ever offered to the site it was made for.
 */
class SignIns(private val store: Store, private val vault: Vault) {
    interface Store {
        fun loadSignIns(): String?
        fun saveSignIns(value: String?)
    }

    interface Vault {
        fun seal(plain: ByteArray): ByteArray
        fun open(sealed: ByteArray): ByteArray
    }

    private var loaded: MutableList<SignIn>? = null

    @Synchronized
    fun find(kind: SignIn.Kind, site: String, realm: String = "", dashboard: String = ""): SignIn? =
        entries().firstOrNull { it.matches(kind, site, realm, dashboard) }

    /** Replaces the sign-in for the same place, and one from before 1.0.5 for its host. */
    @Synchronized
    fun save(signIn: SignIn) {
        val list = entries()
        list.removeAll { it.matches(signIn.kind, signIn.site, signIn.realm, signIn.dashboard) }
        list += signIn
        persist(list)
    }

    /** Forgets the sign-ins for [site], or all of them. */
    @Synchronized
    fun forget(site: String? = null): Int {
        val list = entries()
        val before = list.size
        if (site == null) list.clear() else list.removeAll { it.site.equals(site, ignoreCase = true) }
        if (list.size != before) persist(list)
        return before - list.size
    }

    /** What's saved, without the passwords. */
    @Synchronized
    fun describe(): JSONArray = JSONArray().apply {
        entries().forEach { put(JSONObject().put("kind", it.kind.wire).put("site", it.site).put("username", it.username)) }
    }

    val count: Int @Synchronized get() = entries().size

    private fun entries(): MutableList<SignIn> = loaded ?: load().also { loaded = it }

    private fun load(): MutableList<SignIn> {
        val sealed = store.loadSignIns() ?: return mutableListOf()
        return runCatching {
            val array = JSONArray(String(vault.open(unhex(sealed))))
            (0 until array.length()).map { i ->
                val o = array.getJSONObject(i)
                SignIn(
                    SignIn.Kind.entries.first { it.wire == o.getString("kind") },
                    o.getString("site"), o.optString("realm"), o.optString("username"), o.getString("password"),
                    o.optString("dashboard"),
                )
            }.toMutableList()
        }.getOrElse { mutableListOf() } // a lost keystore key: start over rather than fail
    }

    private fun persist(list: List<SignIn>) {
        if (list.isEmpty()) return store.saveSignIns(null)
        val array = JSONArray()
        list.forEach {
            array.put(
                JSONObject().put("kind", it.kind.wire).put("site", it.site).put("realm", it.realm)
                    .put("username", it.username).put("password", it.password).put("dashboard", it.dashboard),
            )
        }
        store.saveSignIns(hex(vault.seal(array.toString().toByteArray())))
    }

    private fun hex(bytes: ByteArray): String = bytes.joinToString("") { "%02x".format(it) }

    private fun unhex(text: String): ByteArray = ByteArray(text.length / 2) { text.substring(it * 2, it * 2 + 2).toInt(16).toByte() }

    companion object {
        /**
         * A site's own name from its page title, without "Sign in" and the like:
         * "Login | Proxmox Virtual Environment" → "Proxmox Virtual Environment". Null if nothing's left.
         */
        fun siteName(pageTitle: String?): String? {
            val title = pageTitle?.trim().orEmpty()
            if (title.isEmpty() || "://" in title) return null // no <title>: the web view shows the address
            return title.split(" | ", " - ", " – ", " — ", " · ", ": ")
                .map { it.trim().replace(SIGN_IN_PREFIX, "").trim() }
                .lastOrNull { it.isNotEmpty() && !GENERIC.matches(it) }
                ?.take(40)
        }

        private val SIGN_IN_PREFIX = Regex("(?i)^(sign in|log in|login|welcome) to ")
        private val GENERIC = Regex("(?i)(please )?(sign[ -]?in|log[ -]?in|log on|welcome|authentication|authenticate|account|home)")

        /** Script for a page: "form" when it shows a password field. */
        const val DETECT_SCRIPT = """(function () {
  function shown(el) { var r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== "hidden"; }
  return Array.prototype.some.call(document.querySelectorAll('input[type="password"]'), shown) ? "form" : "";
})()"""

        /**
         * Script that fills a login form and submits it, but only on [origin]: the page may have
         * moved on between the check in the app and the script running.
         */
        fun fillScript(origin: String, username: String, password: String): String = """(function (origin, username, password) {
  if (location.origin !== origin) return "elsewhere";
  function shown(el) { var r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== "hidden"; }
  var fields = Array.prototype.filter.call(document.querySelectorAll("input"), shown);
  var pass = fields.filter(function (el) { return el.type === "password"; })[0];
  if (!pass) return "no form";
  var user = fields.slice(0, fields.indexOf(pass)).filter(function (el) {
    return ["text", "email", "tel", ""].indexOf(el.type) >= 0 || el.autocomplete === "username";
  }).pop();
  // The native setter and an input event, so frameworks such as React notice the new value.
  var setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
  function set(el, value) {
    el.focus();
    setter.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }
  if (user && username) set(user, username);
  set(pass, password);
  var scope = pass.form || document;
  var button = scope.querySelector('button[type="submit"], input[type="submit"]') ||
    Array.prototype.filter.call(scope.querySelectorAll("button"), shown)[0];
  setTimeout(function () {
    if (button) button.click();
    else if (pass.form && pass.form.requestSubmit) pass.form.requestSubmit();
    else if (pass.form) pass.form.submit();
  }, 100);
  return "submitted";
})(${JSONObject.quote(origin)}, ${JSONObject.quote(username)}, ${JSONObject.quote(password)})"""
    }
}

/** AES-GCM with a key generated inside the Android keystore; the key can't be exported. */
class KeystoreVault : SignIns.Vault {
    private fun key(): SecretKey {
        val keyStore = KeyStore.getInstance(KEYSTORE).apply { load(null) }
        (keyStore.getKey(ALIAS, null) as? SecretKey)?.let { return it }
        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, KEYSTORE)
        generator.init(
            KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build(),
        )
        return generator.generateKey()
    }

    override fun seal(plain: ByteArray): ByteArray {
        val cipher = Cipher.getInstance(TRANSFORMATION).apply { init(Cipher.ENCRYPT_MODE, key()) }
        return cipher.iv + cipher.doFinal(plain)
    }

    override fun open(sealed: ByteArray): ByteArray {
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, sealed, 0, IV_BYTES))
        return cipher.doFinal(sealed, IV_BYTES, sealed.size - IV_BYTES)
    }

    private companion object {
        const val KEYSTORE = "AndroidKeyStore"
        const val ALIAS = "pleatview-sign-ins"
        const val TRANSFORMATION = "AES/GCM/NoPadding"
        const val IV_BYTES = 12
    }
}
