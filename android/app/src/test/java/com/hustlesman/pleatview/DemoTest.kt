package com.hustlesman.pleatview

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

class DemoTest {
    private val assets = File("src/main/assets")

    @Test
    fun demoAddressesMapToTheDemoFolder() {
        assertEquals("demo/index.html", Demo.asset(Demo.HOME))
        assertEquals("demo/camera.html", Demo.asset(Demo.DOOR_CAMERA))
        assertEquals("demo/clip.mp4", Demo.asset(Demo.BASE + "clip.mp4#t=2"))
    }

    @Test
    fun otherAddressesAreNotTheDemos() {
        assertNull(Demo.asset("http://homeassistant.local:8123/lovelace/0"))
        assertNull(Demo.asset("https://appassets.androidplatform.net/setup.html"))
        assertNull(Demo.asset(Demo.BASE))
        assertNull(Demo.asset(Demo.BASE + "../setup.html"))
        assertNull(Demo.asset(Demo.BASE + "photos//door.webp"))
    }

    @Test
    fun everythingTheTourShowsIsInTheApp() {
        for (url in listOf(Demo.HOME, Demo.ENERGY, Demo.CAMERAS, Demo.DOOR_CAMERA, Demo.VIDEO)) {
            val file = File(assets, Demo.asset(url)!!)
            assertTrue("$url: no ${file.path}", file.isFile)
        }
    }

    @Test
    fun everyFileThePagesUseIsInTheApp() {
        val folder = File(assets, "demo")
        val pages = folder.listFiles { file -> file.extension == "html" }!!.toList()
        assertTrue(pages.size >= 4)
        val references = Regex("""(?:src|href)="([^"#?]+)""")
        for (page in pages) {
            for (match in references.findAll(page.readText())) {
                val file = File(folder, match.groupValues[1])
                assertTrue("${page.name} uses ${match.groupValues[1]}, which isn't there", file.isFile)
            }
        }
        // The single camera picks its picture by name.
        for (camera in listOf("front-door", "driveway", "garden", "living-room")) {
            assertTrue(File(folder, "$camera.webp").isFile)
        }
    }

    @Test
    fun everyLanguageHasEveryStringOfThePages() {
        val folder = File(assets, "demo")
        // The English: data-t elements, t("key", "English") calls and demo.js's [key, English] tables.
        val english = mutableMapOf<String, String>()
        for (page in folder.listFiles { file -> file.extension == "html" }!!.sorted()) {
            for (match in Regex("""<(\w+)\b[^>]*\bdata-t="(\w+)"[^>]*>(.*?)</\1>""", RegexOption.DOT_MATCHES_ALL).findAll(page.readText())) {
                english.putIfAbsent(match.groupValues[2], match.groupValues[3])
            }
        }
        val script = File(folder, "demo.js").readText()
        for (match in Regex("""\bt\("(\w+)", "([^"]*)"\)""").findAll(script)) english.putIfAbsent(match.groupValues[1], match.groupValues[2])
        for (match in Regex("""\["(\w+)", "([^"]*)"""").findAll(script)) english.putIfAbsent(match.groupValues[1], match.groupValues[2])
        assertTrue(english.size >= 40)

        val source = File(folder, "translations.js").readText()
        val translations = JSONObject(source.substringAfter("const TRANSLATIONS = ").substringBeforeLast(";"))
        assertTrue(translations.length() >= 10)
        val tags = { text: String -> Regex("<[^>]+>").findAll(text).map { it.value }.sorted().toList() }
        for (language in translations.keys()) {
            val strings = translations.getJSONObject(language)
            assertEquals(language, english.keys, strings.keys().asSequence().toSet())
            for ((key, text) in english) {
                val translated = strings.getString(key)
                assertEquals("$language $key", tags(text), tags(translated))
                assertFalse("$language $key runs script", Regex("""<script|\son\w+=""", RegexOption.IGNORE_CASE).containsMatchIn(translated))
            }
        }
    }
}
