package com.hustlesman.pleatview

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class WallTest {
    @Test
    fun wallAddressesListTheirStreams() {
        val ids = listOf("rFZHOHl-L8A", "ROboUHfPUrU", "XsOU8JnEpNM")
        assertEquals(ids, Wall.ids("http://192.168.1.10:8123/api/pleatview/wall?v=rFZHOHl-L8A,ROboUHfPUrU,XsOU8JnEpNM"))
        assertEquals(ids, Wall.ids("https://ha.example.org/api/pleatview/wall/?v=rFZHOHl-L8A,ROboUHfPUrU,XsOU8JnEpNM"))
        // 1.0.14's address on the website.
        assertEquals(ids, Wall.ids("https://pleatview.hustlesman.com/wall?v=rFZHOHl-L8A,ROboUHfPUrU,XsOU8JnEpNM"))
        assertEquals(ids, Wall.ids("https://pleatview.hustlesman.com/wall/?v=rFZHOHl-L8A%2CROboUHfPUrU%2CXsOU8JnEpNM"))
        assertEquals(ids, Wall.ids("http://PleatView.Hustlesman.com/wall.html?x=1&v=rFZHOHl-L8A,%20ROboUHfPUrU,XsOU8JnEpNM#top"))
    }

    @Test
    fun onlyVideoIdsReachThePage() {
        assertEquals(listOf("rFZHOHl-L8A"), Wall.ids("https://pleatview.hustlesman.com/wall?v=rFZHOHl-L8A,%22%3E%3Cscript%3E,,short"))
        assertNull(Wall.ids("https://pleatview.hustlesman.com/wall?v=%22%3E%3Cscript%3E"))
        assertNull(Wall.ids("https://pleatview.hustlesman.com/wall"))
        val many = (10..40).map { "video-id-$it" }
        assertEquals(many.take(Wall.MAX_STREAMS), Wall.ids("https://pleatview.hustlesman.com/wall?v=" + many.joinToString(",")))
    }

    @Test
    fun otherAddressesAreNotWalls() {
        assertNull(Wall.ids("https://pleatview.hustlesman.com/?v=rFZHOHl-L8A"))
        assertNull(Wall.ids("https://pleatview.hustlesman.com/wallpaper?v=rFZHOHl-L8A"))
        assertNull(Wall.ids("https://example.com/wall?v=rFZHOHl-L8A"))
        assertNull(Wall.ids("http://192.168.1.10:8123/api/pleatview/wall2?v=rFZHOHl-L8A"))
        assertNull(Wall.ids("http://192.168.1.10:8123/local/api/pleatview/wall?v=rFZHOHl-L8A"))
        assertNull(Wall.ids("http://homeassistant.local:8123/local/streams.html?v=rFZHOHl-L8A"))
        assertNull(Wall.ids("/lovelace/0"))
    }

    @Test
    fun wallsAreAsSquareAsTheyCanBe() {
        assertEquals(listOf(1, 2, 2, 2, 3, 3, 3, 3, 3, 4), (1..10).map(Wall::columns))
        assertEquals(4, Wall.columns(16))
    }

    @Test
    fun aRowIsMutedPlayersAtThePagesZoom() {
        val html = Wall.html(listOf("rFZHOHl-L8A", "ROboUHfPUrU"), 3, 1433)
        assertEquals(2, Regex("<iframe ").findAll(html).count())
        assertTrue(html.contains("https://www.youtube-nocookie.com/embed/rFZHOHl-L8A?autoplay=1&amp;mute=1&amp;controls=0"))
        assertTrue(html.contains("grid-template-columns:repeat(3,1fr)"))
        assertTrue(html.contains("content=\"width=1433\""))
        assertTrue(html.contains("referrerpolicy=\"strict-origin-when-cross-origin\""))
        assertTrue(Wall.html(listOf("rFZHOHl-L8A"), 1, 0).contains("width=device-width"))
        assertFalse(html.contains("allowfullscreen"))
    }
}
