package com.hustlesman.pleatview

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ConfigTest {
    @Test
    fun `base url is normalized to scheme host and port`() {
        assertEquals("http://ha.local:8123", Config.normalizeBaseUrl(" http://ha.local:8123/lovelace/0 "))
        assertEquals("https://example.org", Config.normalizeBaseUrl("HTTPS://example.org/"))
        assertEquals("http://[fe80::1]:8123", Config.normalizeBaseUrl("http://[fe80::1]:8123"))
        assertEquals("", Config.normalizeBaseUrl("  "))
    }

    @Test
    fun `reopen minutes default to ten and can be turned off`() {
        assertEquals(10, Config().reopenMinutes)
        assertEquals(0, Config().merge(JSONObject().put("reopen_minutes", 0)).reopenMinutes)
        assertEquals(10, Config.fromJson(Config().toJson(includeSecrets = true)).reopenMinutes)
    }

    @Test(expected = IllegalArgumentException::class)
    fun `non http base url is rejected`() {
        Config.normalizeBaseUrl("ftp://ha.local")
    }

    @Test
    fun `merge applies known keys and clears rev unless given`() {
        val base = Config(rev = "abc")
        val merged = base.merge(
            JSONObject()
                .put("ha_url", "http://10.0.0.2:8123/")
                .put("zoom", 75)
                .put("kiosk", "sidebar")
                .put("dashboards", JSONArray().put(JSONObject().put("title", "Cams").put("path", "dashboard-cams"))),
        )
        assertEquals("http://10.0.0.2:8123", merged.haUrl)
        assertEquals(75, merged.zoom)
        assertEquals("sidebar", merged.kiosk)
        assertEquals(listOf(Dashboard("Cams", "/dashboard-cams")), merged.dashboards)
        assertEquals("", merged.rev)

        val managed = base.merge(JSONObject().put("zoom", 90).put("rev", "r2"))
        assertEquals("r2", managed.rev)
    }

    @Test(expected = IllegalArgumentException::class)
    fun `unknown keys are rejected`() {
        Config().merge(JSONObject().put("bogus", 1))
    }

    @Test(expected = IllegalArgumentException::class)
    fun `zoom outside range is rejected`() {
        Config().merge(JSONObject().put("zoom", 10))
    }

    @Test
    fun `stored config tolerates unknown keys`() {
        val config = Config.fromJson(JSONObject().put("ha_url", "http://ha:8123").put("from_the_future", true).put("rev", "x"))
        assertEquals("http://ha:8123", config.haUrl)
        assertEquals("x", config.rev)
    }

    @Test
    fun `resolve and origin checks`() {
        val config = Config(haUrl = "http://ha.local:8123")
        assertEquals("http://ha.local:8123/lovelace/0", config.resolve("lovelace/0"))
        assertEquals("https://grafana.example/d/1", config.resolve("https://grafana.example/d/1"))
        assertTrue(config.isHaUrl("http://HA.local:8123/energy"))
        assertFalse(config.isHaUrl("http://ha.local:8124/"))
        assertTrue(Config(haUrl = "https://ha.example").isHaUrl("https://ha.example:443/x"))
        assertEquals("/lovelace/0?kiosk", Config.pathOf("http://ha.local:8123/lovelace/0?kiosk"))
        assertEquals("/", Config.pathOf("http://ha.local:8123"))
    }

    @Test
    fun `files Home Assistant serves itself aren't frontend pages`() {
        assertTrue(Config.isFrontendPath("/lovelace/0"))
        assertTrue(Config.isFrontendPath("/dashboard-streams/live?kiosk"))
        assertTrue(Config.isFrontendPath("/media-browser"))
        assertFalse(Config.isFrontendPath("/local/streams.html?v=a,b"))
        assertFalse(Config.isFrontendPath("/hacsfiles/card/card.js"))
        assertFalse(Config.isFrontendPath("/api/camera_proxy_stream/camera.door"))
    }

    @Test
    fun `home path falls back to first dashboard then HA default`() {
        assertEquals("/", Config().homePath)
        assertEquals("/a", Config(dashboards = listOf(Dashboard("A", "/a"))).homePath)
        assertEquals("/b", Config(homeDashboard = "/b", dashboards = listOf(Dashboard("A", "/a"))).homePath)
    }

    @Test
    fun `secrets are only serialized on request`() {
        val config = Config(haToken = "secret")
        assertFalse(config.toJson().has("ha_token"))
        assertTrue(config.toJson().getBoolean("ha_token_set"))
        assertEquals("secret", config.toJson(includeSecrets = true).getString("ha_token"))
    }

    @Test
    fun `moving to another Home Assistant drops the token unless a new one comes with it`() {
        val config = Config(haUrl = "http://ha.local:8123", haToken = "secret")
        assertEquals("secret", config.merge(JSONObject().put("ha_url", "http://HA.local:8123/lovelace")).haToken)
        assertEquals("", config.merge(JSONObject().put("ha_url", "http://elsewhere:8123")).haToken)
        assertEquals("new", config.merge(JSONObject().put("ha_url", "http://elsewhere:8123").put("ha_token", "new")).haToken)
        assertEquals("secret", Config.fromJson(config.toJson(includeSecrets = true)).haToken)
    }
}
