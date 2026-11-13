"""PleatView Light tests: python3 -m unittest discover -v tests (from light/). No mpv needed."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import struct
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pleatview_light import mdns  # noqa: E402
from pleatview_light.api import Api  # noqa: E402
from pleatview_light.config import Config  # noqa: E402
from pleatview_light.pairing import Pairing, sha256  # noqa: E402
from pleatview_light.screen import Screen  # noqa: E402


class FakePlayer:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def play(self, url, **kw):
        self.calls.append(("play", url, kw))

    async def blank(self):
        self.calls.append(("blank",))

    async def text(self, message, seconds=3600):
        self.calls.append(("text", message, seconds))

    async def clear_text(self):
        self.calls.append(("clear",))

    async def set(self, name, value):
        self.calls.append(("set", name, value))

    async def command(self, *args):
        self.calls.append(("command", *args))


def make_screen(config: Config | None = None) -> tuple[Screen, FakePlayer, list[Config]]:
    player, saved = FakePlayer(), []
    screen = Screen(config=config or Config(), save_config=saved.append, player=player,  # type: ignore[arg-type]
                    setup=lambda: {"name": "Zero", "address": "10.0.0.5", "port": "8765", "pin": "123456"},
                    version="1.0.16", size=(1280, 720))
    return screen, player, saved


class Pairings(unittest.TestCase):
    def test_pin_tokens_and_lockout(self) -> None:
        stored = []
        now = [0.0]
        pairing = Pairing(lambda: None, stored.append, clock=lambda: now[0])
        pin = pairing.pin
        result = pairing.pair(pin, "Home Assistant")
        self.assertEqual(result["result"], "paired")
        self.assertTrue(pairing.valid(result["token"]))
        self.assertNotIn(result["token"], stored[-1])  # only hashes are kept
        self.assertNotEqual(pairing.pin, pin)  # used up
        for _ in range(5):
            self.assertEqual(pairing.pair("000000x", "x")["result"], "invalid_pin")
        self.assertEqual(pairing.pair(pairing.pin, "x")["result"], "locked_out")
        now[0] += 61
        self.assertEqual(pairing.pair(pairing.pin, "x")["result"], "paired")
        for i in range(10):
            now[0] += 1
            pairing.pair(pairing.pin, f"c{i}")
        self.assertEqual(len(pairing.records), 8)
        self.assertFalse(pairing.valid(result["token"]))  # the oldest went

    def test_proof_matches_the_protocol_test_vector(self) -> None:
        pairing = Pairing(lambda: json.dumps([{"hash": sha256("token-1"), "client": "x"}]), lambda _: None)
        self.assertEqual(pairing.prove("00112233445566778899aabbccddeeff"),
                         ["cfd2cb0b0606646c50484f870216c1870afd7f225109845b6435fdfd40887c7e"])


class Configs(unittest.TestCase):
    def test_merge(self) -> None:
        config = Config().merge({"ha_url": "http://HA.local:8123/lovelace/0", "ha_token": "t", "rev": "r1",
                                 "dashboards": [{"title": "Cams", "path": "dashboard-cams"}], "zoom": 80})
        self.assertEqual(config.ha_url, "http://ha.local:8123")
        self.assertEqual(config.home_path, "/dashboard-cams")
        self.assertEqual(config.resolve(config.home_path), "http://ha.local:8123/dashboard-cams")
        self.assertEqual(config.public()["ha_token_set"], True)
        self.assertNotIn("ha_token", config.public())
        self.assertEqual(config.merge({"kiosk": "off"}).rev, "")
        self.assertEqual(config.merge({"ha_url": "http://other:8123"}).ha_token, "")
        for bad in ({"nope": 1}, {"render_stream": "file:///x"}, {"zoom": 5}, {"kiosk": "x"}):
            with self.assertRaises(ValueError):
                Config().merge(bad)
        self.assertTrue(Config().merge({"home_dashboard": "https://signage.example/p"}).configured)
        self.assertEqual(Config.load({**config.stored(), "rev": "kept"}).rev, "kept")


class Screens(unittest.IsolatedAsyncioTestCase):
    async def test_stream_dashboards_media_and_standby(self) -> None:
        screen, player, saved = make_screen()
        await screen.show()
        self.assertEqual(screen.state["view"], "setup")
        self.assertIn("PIN  123 456", player.calls[-1][1])
        await screen.configure({"ha_url": "http://ha:8123", "dashboards": [{"title": "A", "path": "/a"},
                                {"title": "B", "path": "/b"}], "render_stream": "http://ha:8123/s/index.m3u8", "rev": "1"})
        self.assertEqual(saved[-1].rev, "1")
        self.assertEqual(screen.state["url"], "http://ha:8123/a")
        self.assertEqual(screen.state["dashboard"], "/a")
        self.assertIn(("play", "http://ha:8123/s/index.m3u8", {"live": True, "volume": 1.0}), player.calls)
        await screen.command({"command": "next_dashboard"})
        self.assertEqual(screen.state["dashboard"], "/b")
        await screen.command({"command": "show_dashboard", "path": "/c", "duration": 0.05})
        self.assertEqual(screen.state["dashboard"], "/c")
        await asyncio.sleep(0.1)
        self.assertEqual(screen.state["dashboard"], "/a")  # home again
        await screen.command({"command": "set_volume", "level": 0.5})
        await screen.command({"command": "play_media", "url": "http://x/clip.mp4", "title": "Clip", "volume": 0.2})
        self.assertEqual(screen.state["media"]["title"], "Clip")
        await screen.on_player({"event": "end-file", "reason": "eof"})
        self.assertIsNone(screen.state["media"])
        # Back to the stream, and its sound at the screen's volume, not the clip's.
        self.assertEqual(player.calls[-2], ("play", "http://ha:8123/s/index.m3u8", {"live": True, "volume": 0.5}))
        await screen.command({"command": "turn_off"})
        self.assertEqual((screen.state["power"], player.calls[-1]), ("standby", ("blank",)))
        with self.assertRaises(ValueError):
            await screen.command({"command": "play_media", "url": "file:///etc/passwd"})
        with self.assertRaises(ValueError):
            await screen.command({"command": "reboot"})
        self.assertEqual(screen.state["features"], ["render_stream", "home_without_ha", "home_playlist", "stream_overlays", "rotation"])
        self.assertEqual(screen.state["screen"]["width"], 1280)


class Playlists(unittest.IsolatedAsyncioTestCase):
    async def test_home_playlist_plays_from_the_cache(self) -> None:
        import tempfile
        from pleatview_light.playlist import MediaCache

        with tempfile.TemporaryDirectory() as tmp:
            cache = MediaCache(Path(tmp))
            screen, player, _ = make_screen()
            screen.cache = cache
            items = [{"url": "http://media.example/a.mp4", "kind": "video"},
                     {"url": "http://media.example/b.png", "kind": "image", "seconds": 5}]
            cache.path(items[1]["url"]).write_bytes(b"png")  # the picture is cached, the video not yet
            cache.keep = lambda items: None  # no downloads in tests
            await screen.configure({"home_playlist": items, "render_stream": "http://ha/s.m3u8"})
            self.assertTrue(screen.state["view"] == "dashboard")
            self.assertEqual(player.calls[-1][:2], ("play", "http://media.example/a.mp4"))  # not cached: streamed
            await screen.on_player({"event": "end-file", "reason": "eof"})
            self.assertEqual(player.calls[-1], ("play", str(cache.path(items[1]["url"])), {"image_seconds": 5, "volume": 1.0}))
            await screen.command({"command": "show_dashboard", "path": "/other"})
            await asyncio.sleep(0.05)
            plays = [c for c in player.calls if c[0] == "play"]
            self.assertEqual(plays[-1][:2], ("play", "http://ha/s.m3u8"))  # away from home: rendered
            with self.assertRaises(ValueError):
                Config().merge({"home_playlist": [{"url": "file:///etc/passwd"}]})


class ApiRoutes(unittest.IsolatedAsyncioTestCase):
    async def test_routes(self) -> None:
        screen, player, _ = make_screen()
        pairing = Pairing(lambda: None, lambda _: None)
        api = Api(screen, pairing, lambda: {"id": "abc", "name": "Zero"})
        status, info = await api.route("GET", "/api/info", {}, None)
        self.assertEqual((status, info["paired"]), (200, False))
        self.assertIn("PIN", player.calls[-1][1])  # someone's about to pair: the PIN shows
        self.assertEqual((await api.route("GET", "/api/state", {}, None))[0], 401)
        self.assertEqual((await api.route("POST", "/api/pair", {"pin": "1"}, None))[0], 403)
        status, reply = await api.route("POST", "/api/pair", {"pin": pairing.pin, "client": "HA"}, None)
        token = reply["token"]
        self.assertEqual((await api.route("POST", "/api/config", {"bogus": 1}, token))[0], 400)
        status, config = await api.route("POST", "/api/config", {"ha_url": "http://ha:8123", "rev": "x"}, token)
        self.assertEqual((status, config["rev"]), (200, "x"))
        self.assertEqual((await api.route("POST", "/api/command", {"command": "notify", "message": "Hi"}, token))[0], 200)
        self.assertEqual((await api.route("POST", "/api/verify", {"nonce": "zz"}, None))[0], 400)
        self.assertEqual((await api.route("POST", "/api/unpair", {}, token))[0], 200)
        self.assertEqual((await api.route("GET", "/api/state", {}, token))[0], 401)


class Announcing(unittest.TestCase):
    def test_answers_the_services_question(self) -> None:
        responder = mdns.Responder(lambda: {"id": "0123456789abcdef", "name": "Zero", "api": "1", "version": "1.0.16"}, 8765)
        query = struct.pack("!6H", 0, 0, 1, 0, 0, 0) + mdns.encode_name(mdns.SERVICE) + struct.pack("!HH", mdns.PTR, 1)
        answers, extra = responder.answer(mdns.questions(query))
        packet = responder.message(answers, extra)
        self.assertIn(b"\x04Zero\x0a_pleatview", packet)
        self.assertIn(b"id=0123456789abcdef", packet)
        self.assertIn(struct.pack("!H", 8765), packet)
        self.assertEqual(len(answers) + len(extra), 4)
        self.assertIsNone(responder.answer(mdns.questions(struct.pack("!6H", 0, 0, 1, 0, 0, 0)
                                                          + mdns.encode_name("_other._tcp.local.") + struct.pack("!HH", 12, 1))))
        self.assertEqual(mdns.questions(b"\x00" * 3), [])


if __name__ == "__main__":
    unittest.main()


class Rotation(unittest.TestCase):
    def test_text_turns_with_the_screen_in_a_box_of_its_own(self) -> None:
        from pleatview_light.player import turned_text
        box, text = turned_text("PleatView\nIn Home Assistant: Settings → Devices & services → PleatView {x}", 90).split("\n")
        self.assertIn("\\frz90", box)
        self.assertIn("\\p1}m 0 0 l 660", box)  # a box as wide as the screen is that way round
        self.assertIn("\\frz90", text)
        self.assertIn("\\N", text)  # wrapped to fit
        self.assertIn("\\{x}", text)  # a brace is text, not a tag
        self.assertEqual(Config().merge({"rotation": 270}).rotation, 270)
        with self.assertRaises(ValueError):
            Config().merge({"rotation": 45})
