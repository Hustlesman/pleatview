"""Tests for the PleatView Renderer, run inside its image: python3 -m unittest discover tests."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aiohttp.test_utils import AioHTTPTestCase

from pleatview_renderer import page, pairing, server, session
from pleatview_renderer.session import Screen

APP_HELPER = Path(__file__).parents[2] / "android/app/src/main/assets/pleatview.js"


class ScreenTest(unittest.TestCase):
    def test_a_request_becomes_a_screen(self) -> None:
        screen = server.parse_screen({
            "url": "http://ha.local:8123/home", "width": 1281, "height": 721, "scale": 1.3333,
            "kiosk": "sidebar", "ha_url": "http://ha.local:8123", "token": "t",
        })
        self.assertEqual(screen, Screen("http://ha.local:8123/home", 1280, 720, 1.333, "sidebar", "http://ha.local:8123", "t"))
        self.assertEqual(screen.ha_origin, "http://ha.local:8123")
        self.assertNotIn("token=", repr(screen))  # the token stays out of logs

    def test_bad_requests_are_refused(self) -> None:
        for body in (
            [], {"url": "file:///etc/passwd"}, {"url": "javascript:alert(1)"}, {"url": "http://"},
            {"url": "http://a", "width": 10_000}, {"url": "http://a", "scale": 0}, {"url": "http://a", "kiosk": "on"},
            {"url": "http://a", "token": 5},
        ):
            with self.subTest(body=body), self.assertRaises(ValueError):
                server.parse_screen(body)

    def test_only_size_and_scale_restart_chromium(self) -> None:
        screen = Screen("http://a/1")
        self.assertFalse(screen.restart_needed(Screen("http://a/2", kiosk="off", token="x")))
        self.assertTrue(screen.restart_needed(Screen("http://a/1", scale=1.5)))
        self.assertTrue(screen.restart_needed(Screen("http://a/1", width=1280, height=720)))


class ChromiumTest(unittest.TestCase):
    def test_the_window_fills_the_display_at_any_scale(self) -> None:
        args = session.chromium_args(Screen("http://a", 1920, 1080, 1.34), Path("/p"), 9310)
        self.assertIn("--window-size=1433,806", args)
        self.assertIn("--force-device-scale-factor=1.34", args)
        self.assertIn("--remote-debugging-address=127.0.0.1", args)
        self.assertEqual(args[-1], "about:blank")

    def test_every_segment_starts_with_a_keyframe(self) -> None:
        for encoder in ("vaapi", "x264"):
            args = session.encoder_args(encoder, Screen("http://a"))
            self.assertEqual(args[args.index("-g") + 1], str(session.FPS * session.SEGMENT_SECONDS))
        self.assertIn("h264_vaapi", session.encoder_args("vaapi", Screen("http://a")))
        self.assertIn("6000000", session.encoder_args("x264", Screen("http://a")))
        self.assertIn("1500000", session.encoder_args("x264", Screen("http://a", 640, 360)))


class PageTest(unittest.TestCase):
    def test_the_helper_is_the_apps_own(self) -> None:
        if not APP_HELPER.exists():
            self.skipTest("outside the repository")
        self.assertEqual(page.HELPER, APP_HELPER.read_text())

    def test_only_home_assistant_pages_get_the_token(self) -> None:
        source = page.script("http://ha.local:8123", 'tok"en', "full")
        self.assertTrue(source.startswith('if ("http://ha.local:8123" && location.origin === "http://ha.local:8123"'))
        self.assertIn(json.dumps('tok"en'), source)
        self.assertIn('"externalAuth": true', source)
        self.assertNotIn("__PLEATVIEW_CONFIG__", source)
        self.assertNotIn("__TOKEN__", source)
        # Without a Home Assistant, the script does nothing anywhere.
        self.assertTrue(page.script("", "", "off").startswith('if ("" && '))


class AddressTest(unittest.TestCase):
    def test_the_log_names_the_address_to_use(self) -> None:
        # The documented docker run listens on 127.0.0.1 only; the add-on on all addresses.
        self.assertEqual(server.advertised_address({"HOST": "127.0.0.1", "HOSTNAME": "media"}, 8780), "http://127.0.0.1:8780")
        self.assertEqual(server.advertised_address({"HOSTNAME": "a0d7b954-pleatview-renderer"}, 8780),
                         "http://a0d7b954-pleatview-renderer:8780")


class ApiTest(AioHTTPTestCase):
    async def get_application(self):
        self.root = tempfile.TemporaryDirectory()
        patches = [mock.patch.object(session.Session, name, new=mock.AsyncMock()) for name in ("stop", "update")]
        patches.append(mock.patch.object(session.Session, "start", new=mock.Mock()))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.root.cleanup)
        return server.create_app(Path(self.root.name), "x264", max_sessions=2)

    async def test_screens_are_shown_and_limited(self) -> None:
        body = {"url": "http://ha.local:8123/home"}
        response = await self.client.put("/api/sessions/tv-1", json=body)
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {"playlist": "/hls/tv-1/index.m3u8"})
        self.assertEqual((await self.client.put("/api/sessions/tv-2", json=body)).status, 200)
        self.assertEqual((await self.client.put("/api/sessions/tv-3", json=body)).status, 429)
        self.assertEqual((await self.client.put("/api/sessions/bad key!", json=body)).status, 404)
        self.assertEqual((await self.client.put("/api/sessions/tv-1", json={"url": "ftp://x"})).status, 400)
        info = await (await self.client.get("/api/info")).json()
        self.assertEqual(sorted(info["sessions"]), ["tv-1", "tv-2"])
        self.assertEqual((await self.client.delete("/api/sessions/tv-2")).status, 204)

    async def test_only_stream_files_are_served(self) -> None:
        await self.client.put("/api/sessions/tv-1", json={"url": "http://ha.local:8123/home"})
        folder = Path(self.root.name) / "tv-1"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "s3.ts").write_bytes(b"\x47" * 188)
        response = await self.client.get("/hls/tv-1/s3.ts")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, "video/mp2t")
        self.assertEqual((await self.client.get("/hls/tv-1/s4.ts")).status, 404)
        self.assertEqual((await self.client.get("/hls/tv-1/..%2Fprofiles")).status, 404)
        self.assertEqual((await self.client.get("/hls/other/s3.ts")).status, 404)


class PairedApiTest(AioHTTPTestCase):
    """A renderer on the network: nothing without a paired client's token."""

    async def get_application(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        patches = [mock.patch.object(session.Session, name, new=mock.AsyncMock()) for name in ("stop", "update")]
        patches.append(mock.patch.object(session.Session, "start", new=mock.Mock()))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.stored: list[str] = []
        self.pairing = pairing.Pairing(lambda: None, self.stored.append)
        return server.create_app(Path(self.root.name), "x264", max_sessions=2, pairing=self.pairing,
                                 require_token=True, identity={"id": "r1", "name": "Rack A"})

    async def test_pairing_and_tokens(self) -> None:
        body = {"url": "http://ha.local:8123/home"}
        self.assertEqual((await self.client.put("/api/sessions/tv-1", json=body)).status, 401)
        self.assertEqual((await self.client.get("/hls/tv-1/index.m3u8")).status, 401)
        info = await (await self.client.get("/api/info")).json()
        self.assertEqual((info["name"], info["paired"], info["max_screens"]), ("Rack A", False, 2))
        self.assertNotIn("sessions", info)  # nothing about its screens without a token
        self.assertEqual((await self.client.post("/api/pair", json={"pin": "000000x"})).status, 403)
        reply = await (await self.client.post("/api/pair", json={"pin": self.pairing.pin, "client": "HA"})).json()
        auth = {"Authorization": f"Bearer {reply['token']}"}
        self.assertNotIn(reply["token"], self.stored[-1])
        self.assertEqual((await self.client.put("/api/sessions/tv-1", json=body, headers=auth)).status, 200)
        info = await (await self.client.get("/api/info", headers=auth)).json()
        self.assertIn("tv-1", info["sessions"])
        self.assertIn("cpu", info["load"])
        self.assertEqual((await self.client.put("/api/sessions/tv-1", json=body,
                                                headers={"Authorization": "Bearer nope"})).status, 401)
        proofs = (await (await self.client.post("/api/verify", json={"nonce": "ab" * 16})).json())["proofs"]
        self.assertEqual(len(proofs), 1)
        self.assertEqual((await self.client.post("/api/unpair", headers=auth)).status, 200)
        self.assertEqual((await self.client.put("/api/sessions/tv-1", json=body, headers=auth)).status, 401)


class SharedCode(unittest.TestCase):
    def test_same_as_the_light_client(self) -> None:
        light = Path(__file__).resolve().parents[2] / "light" / "pleatview_light"
        if not light.is_dir():
            self.skipTest("the repository's light client isn't here")
        for name in ("mdns.py", "pairing.py"):
            self.assertEqual((light / name).read_text(), (Path(server.__file__).parent / name).read_text(), name)


if __name__ == "__main__":
    unittest.main()
