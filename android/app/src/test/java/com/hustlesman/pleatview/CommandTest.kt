package com.hustlesman.pleatview

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

class CommandTest {
    private fun parse(json: String) = Command.parse(JSONObject(json))

    private fun assertBad(json: String, fragment: String) {
        try {
            parse(json)
            fail("expected BadCommand for $json")
        } catch (e: BadCommand) {
            assertTrue("'${e.message}' should mention '$fragment'", e.message!!.contains(fragment))
        }
    }

    @Test
    fun `notifications have a level and, past a notice, a tone`() {
        val notice = parse("""{"command":"notify","message":"Hi","tone":"chime"}""") as Command.Notify
        assertEquals(Command.Level.NOTICE, notice.level)
        assertNull(notice.tone) // a notice is a card only
        assertEquals(10, notice.duration)
        val warning = parse("""{"command":"notify","message":"Hi","level":"warning","tone":"beep"}""") as Command.Notify
        assertEquals(Command.Level.WARNING to "beep", warning.level to warning.tone)
        val alert = parse("""{"command":"notify","message":"Hi","level":"alert","tone":"siren"}""") as Command.Notify
        assertEquals(Command.Level.ALERT to "siren", alert.level to alert.tone)
        assertEquals(30, alert.duration) // an alert stays longer
        val silent = parse("""{"command":"notify","message":"Hi","level":"alert","tone":"none"}""") as Command.Notify
        assertNull(silent.tone)
        val newer = parse("""{"command":"notify","message":"Hi","level":"emergency","tone":"klaxon"}""") as Command.Notify
        assertEquals(Command.Level.NOTICE, newer.level) // still a card
    }

    @Test
    fun `show dashboard normalizes path`() {
        assertEquals(Command.ShowDashboard("/lovelace/cams", 30), parse("""{"command":"show_dashboard","path":"lovelace/cams","duration":30}"""))
        assertEquals(Command.ShowDashboard("/x", null), parse("""{"command":"show_dashboard","path":"/x","duration":0}"""))
    }

    @Test
    fun `audio defaults to background and video to fullscreen`() {
        val audio = parse("""{"command":"play_media","url":"http://ha/media/a.mp3"}""") as Command.PlayMedia
        assertEquals(Command.Mode.BACKGROUND, audio.layout.mode)
        val hls = parse("""{"command":"play_media","url":"http://ha/api/hls/x/master_playlist.m3u8","mime_type":"application/vnd.apple.mpegurl"}""") as Command.PlayMedia
        assertEquals(Command.Mode.FULLSCREEN, hls.layout.mode)
        val rtsp = parse("""{"command":"play_media","url":"rtsp://cam/stream","mode":"pip","position":"top-left","size":25}""") as Command.PlayMedia
        assertEquals(Command.Layout(Command.Mode.PIP, Command.Position.TOP_LEFT, 25), rtsp.layout)
        assertEquals(null, rtsp.fallbackUrl)
    }

    @Test
    fun `a camera's video carries its picture stream to fall back on`() {
        val camera = parse(
            """{"command":"play_media","url":"http://ha/api/hls/x/master_playlist.m3u8","mode":"pip",""" +
                """"fallback_url":"http://ha/api/camera_proxy_stream/camera.door?authSig=x"}""",
        ) as Command.PlayMedia
        assertEquals("http://ha/api/camera_proxy_stream/camera.door?authSig=x", camera.fallbackUrl)
    }

    @Test
    fun `images and mjpeg become web overlays`() {
        val image = parse("""{"command":"play_media","url":"http://ha/local/p.JPG?x=1","duration":20}""") as Command.ShowWeb
        assertEquals(Command.WebKind.IMAGE, image.kind)
        assertEquals(20, image.duration)
        val mjpeg = parse("""{"command":"play_media","url":"http://ha/api/camera_proxy_stream/camera.door","mime_type":"multipart/x-mixed-replace"}""")
        assertTrue(mjpeg is Command.ShowWeb)
    }

    @Test
    fun `announce keeps audio even for image extensions`() {
        val announce = parse("""{"command":"play_media","url":"http://ha/tts.mp3","announce":true,"volume":0.5}""") as Command.PlayMedia
        assertTrue(announce.announce)
        assertEquals(0.5f, announce.volume)
    }

    @Test
    fun `notify defaults`() {
        val notify = parse("""{"command":"notify","message":"Door"}""") as Command.Notify
        assertEquals(10, notify.duration)
        assertEquals(Command.Position.TOP_RIGHT, notify.position)
        assertNull(notify.title)
    }

    @Test
    fun `simple commands`() {
        assertEquals(Command.Home, parse("""{"command":"home"}"""))
        assertEquals(Command.Seek(12.5), parse("""{"command":"seek","position":12.5}"""))
        assertEquals(Command.Mute(true), parse("""{"command":"mute","muted":true}"""))
    }

    @Test
    fun `invalid commands are rejected with a reason`() {
        assertBad("""{}""", "command")
        assertBad("""{"command":"explode"}""", "unknown command")
        assertBad("""{"command":"play_media","url":"file:///sdcard/x.mp4"}""", "url")
        assertBad("""{"command":"set_volume","level":1.5}""", "between 0 and 1")
        assertBad("""{"command":"seek","position":-1}""", "at least 0")
        assertBad("""{"command":"show_url","url":"javascript:alert(1)"}""", "http")
        assertBad("""{"command":"play_media","url":"http://x/a.mp4","mode":"sideways"}""", "mode")
        assertBad("""{"command":"mute","muted":"yes"}""", "true or false")
    }
}
