"""Tests for the PleatView Renderer, run inside its image: python3 -m unittest discover tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aiohttp.test_utils import AioHTTPTestCase

from pleatview_renderer import actions, page, pairing, server, session
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
        # WebGL pages (maps) need a GL on Xvfb; the rest stays drawn in software, which costs less.
        self.assertTrue({"--use-angle=gl", "--ignore-gpu-blocklist", "--disable-gpu-compositing"} <= set(args))
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


class OverlayTest(unittest.TestCase):
    def test_overlays_are_checked(self) -> None:
        item = server.parse_overlay({"kind": "notify", "message": "Someone's at the door", "title": "Front door"})
        self.assertEqual((item["position"], item["duration"]), ("top-right", 10.0))
        camera = server.parse_overlay({"kind": "image", "url": "http://ha:8123/api/camera_proxy_stream/camera.door?authSig=x"})
        self.assertEqual((camera["size"], camera["position"]), (30, "bottom-right"))
        full = server.parse_overlay({"kind": "image", "url": "http://ha/x.jpg", "mode": "fullscreen"})
        self.assertEqual((full["size"], full["position"]), (100, "center"))
        self.assertIsNone(server.parse_overlay({"kind": "clear"}))
        for bad in ({"kind": "notify"}, {"kind": "image", "url": "javascript:alert(1)"}, {"kind": "notify", "message": "x", "position": "nowhere"},
                    {"kind": "notify", "message": "x", "duration": 99999}, {"kind": "script"}):
            with self.assertRaises(ValueError, msg=bad):
                server.parse_overlay(bad)

    def test_the_script_draws_text_as_text(self) -> None:
        script = page.overlay_script([{"kind": "notify", "message": "<img src=x onerror=alert(1)>", "title": "", "image_url": "",
                                      "position": "top-right", "duration": 10}])
        self.assertIn("textContent", script)
        self.assertNotIn("innerHTML", script)


class StepsTest(unittest.TestCase):
    MAP = "https://map.example/live?x=1#pleatview=click:Reject%20all;click:Collapse;wait:1.5;drag:70,60,35,45;rightdrag:50,60,50,50;scroll:50,50,-300"

    def test_the_steps_come_out_of_the_address(self) -> None:
        url, steps = actions.split(self.MAP)
        self.assertEqual(url, "https://map.example/live?x=1")
        self.assertEqual([s.kind for s in steps], ["click", "click", "wait", "drag", "rightdrag", "scroll"])
        self.assertEqual((steps[0].text, steps[2].numbers, steps[3].numbers), ("Reject all", (1.5,), (70, 60, 35, 45)))
        # The page keeps the rest of its own fragment; addresses without steps are left alone.
        self.assertEqual(actions.split("https://a.example/#/route&pleatview=wait:1")[0], "https://a.example/#/route")
        for plain in ("https://a.example/", "https://a.example/#/route", "https://a.example/#top"):
            self.assertEqual(actions.split(plain), (plain, ()))
        self.assertEqual(actions.split("https://a.example/#pleatview=click:40,60")[1][0].numbers, (40, 60))

    def test_wrong_steps_are_refused(self) -> None:
        for bad in ("jump:1", "wait:600", "drag:1,2,3", "drag:0,0,0,101", "scroll:50,50", "click:", "wait:" + ";wait:1" * 40):
            with self.assertRaises(ValueError, msg=bad):
                actions.parse(bad)
        with self.assertRaisesRegex(ValueError, "steps"):
            server.parse_screen({"url": "https://a.example/#pleatview=fly:1"})
        self.assertEqual(server.parse_screen({"url": self.MAP}).page[0], "https://map.example/live?x=1")

    def test_the_steps_move_the_mouse(self) -> None:
        class Browser:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict]] = []

            async def call(self, method: str, **params):
                self.calls.append((method, params))
                if method == "Runtime.evaluate":
                    if params["expression"] == "[innerWidth, innerHeight]":
                        return {"result": {"value": [1000, 500]}}
                    return {"result": {"value": [10, 20] if '"collapse"' in params["expression"] else None}}
                return {}

        browser = Browser()
        steps = actions.parse("click:Collapse;click:Missing;drag:50,50,10,50;rightdrag:50,50,50,40;scroll:20,40,-100")
        with mock.patch.object(actions, "FIND_SECONDS", 0), mock.patch.object(actions, "PAUSE", 0):
            asyncio.run(actions.perform(browser, steps))
        mouse = [p for m, p in browser.calls if m == "Input.dispatchMouseEvent"]
        presses = [p for p in mouse if p["type"] == "mousePressed"]
        self.assertEqual([(p["x"], p["y"], p["button"]) for p in presses], [(10, 20, "left"), (500, 250, "left"), (500, 250, "right")])
        released = [p for p in mouse if p["type"] == "mouseReleased"]
        self.assertEqual([(p["x"], p["y"]) for p in released], [(10, 20), (100, 250), (500, 200)])
        drag = [p for p in mouse if p["type"] == "mouseMoved" and p.get("buttons") == 1]
        self.assertEqual(len(drag), actions.DRAG_MOVES)
        wheel = [p for p in mouse if p["type"] == "mouseWheel"]
        self.assertEqual([(w["x"], w["y"], w["deltaY"]) for w in wheel], [(200, 200, -100)])


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


class Certificate(unittest.TestCase):
    def test_made_once_and_private(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cert, key = Path(tmp) / "cert.pem", Path(tmp) / "key.pem"
            server.make_certificate(cert, key)
            first = server.certificate_fingerprint(cert)
            server.make_certificate(cert, key)
            self.assertEqual(first, server.certificate_fingerprint(cert))
            self.assertEqual(key.stat().st_mode & 0o777, 0o600)
            self.assertRegex(first, r"^([0-9A-F]{2}:){31}[0-9A-F]{2}$")


class SharedCode(unittest.TestCase):
    def test_same_as_the_light_client(self) -> None:
        light = Path(__file__).resolve().parents[2] / "light" / "pleatview_light"
        if not light.is_dir():
            self.skipTest("the repository's light client isn't here")
        for name in ("mdns.py", "pairing.py"):
            self.assertEqual((light / name).read_text(), (Path(server.__file__).parent / name).read_text(), name)


if __name__ == "__main__":
    unittest.main()
