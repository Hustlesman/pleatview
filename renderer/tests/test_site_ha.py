"""Home Assistant at the site, shared with the controller (site_ha.py): only what the site lists,
never its token, cameras streamed here, and its screens moved only to this renderer's own
controller. Run inside the renderer's image (it runs ffmpeg)."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import aiohttp
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer

from pleatview_renderer import connect, server, site_ha
from pleatview_renderer.site_ha import Access, HomeAssistant, Shared

TOKEN = "the-sites-own-token-0123456789"
STREAM_TOKEN = "hlsaccesstoken"


def media(folder: Path) -> tuple[bytes, list[bytes], bytes]:
    """A camera's stream as Home Assistant serves it (fMP4: an init segment and 1-second
    segments), and a picture."""
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=25", "-f", "lavfi",
                    "-i", "sine=frequency=440", "-t", "20", "-c:v", "libx264", "-g", "25", "-c:a", "aac",
                    "-f", "hls", "-hls_segment_type", "fmp4", "-hls_time", "1", "-hls_list_size", "0",
                    "-hls_fmp4_init_filename", "init.mp4", "-hls_segment_filename", str(folder / "seg%d.m4s"),
                    str(folder / "playlist.m3u8")], check=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x180", "-frames:v", "1",
                    str(folder / "still.jpg")], check=True)
    segments = [(folder / f"seg{n}.m4s").read_bytes() for n in range(19)]
    return (folder / "init.mp4").read_bytes(), segments, (folder / "still.jpg").read_bytes()


class FakeHomeAssistant:
    """Enough of Home Assistant's API for the renderer: REST, the websocket, a camera's HLS
    stream and picture stream, and PleatView's two services."""

    def __init__(self, init: bytes, segments: list[bytes], still: bytes) -> None:
        self.init, self.segments, self.still = init, segments, still
        self.started = time.monotonic()
        self.seen_tokens: list[str] = []
        self.enrolled: list[dict] = []
        self.services_found = True

    def authorized(self, request: web.Request) -> bool:
        header = request.headers.get("Authorization", "")
        self.seen_tokens.append(header)
        return header == f"Bearer {TOKEN}"

    def app(self) -> web.Application:
        @web.middleware
        async def guard(request: web.Request, handler):
            # HLS addresses carry their own token, as Home Assistant's do; the rest needs the bearer.
            if not request.path.startswith(f"/api/hls/{STREAM_TOKEN}/") and request.path != "/api/websocket" \
                    and not self.authorized(request):
                return web.json_response({"message": "Unauthorized"}, status=401)
            return await handler(request)

        app = web.Application(middlewares=[guard])
        app.router.add_get("/api/config", lambda r: web.json_response({"location_name": "Harbour office", "version": "2026.11.1"}))
        app.router.add_get("/api/states", self.states)
        app.router.add_get("/api/camera_proxy/{entity}", lambda r: web.Response(body=self.still, content_type="image/jpeg"))
        app.router.add_get("/api/camera_proxy_stream/{entity}", self.pictures)
        app.router.add_get("/api/calendars/{entity}", self.calendar)
        app.router.add_get("/api/websocket", self.websocket)
        app.router.add_get(f"/api/hls/{STREAM_TOKEN}/master_playlist.m3u8", self.master)
        app.router.add_get(f"/api/hls/{STREAM_TOKEN}/playlist.m3u8", self.playlist)
        app.router.add_get(f"/api/hls/{STREAM_TOKEN}/init.mp4", lambda r: web.Response(body=self.init, content_type="video/mp4"))
        app.router.add_get(f"/api/hls/{STREAM_TOKEN}/segment/{{n:\\d+}}.m4s", self.segment)
        app.router.add_post("/api/services/pleatview/{service}", self.service)
        return app

    async def states(self, request: web.Request) -> web.Response:
        return web.json_response([
            {"entity_id": "camera.front_door", "state": "streaming",
             "attributes": {"friendly_name": "Front door", "access_token": "camera-secret",
                            "entity_picture": "/api/camera_proxy/camera.front_door?token=camera-secret"}},
            {"entity_id": "camera.back_yard", "state": "idle", "attributes": {"friendly_name": "Back yard", "access_token": "x"}},
            {"entity_id": "lock.front_door", "state": "locked", "attributes": {"friendly_name": "Front door lock"}},
            {"entity_id": "todo.shopping", "state": "2", "attributes": {"friendly_name": "Shopping"}},
            {"entity_id": "calendar.family", "state": "off", "attributes": {"friendly_name": "Family"}},
        ])

    async def calendar(self, request: web.Request) -> web.Response:
        return web.json_response([{"summary": "Swimming", "start": {"dateTime": "2026-11-20T16:00:00+13:00"},
                                   "end": {"dateTime": "2026-11-20T17:00:00+13:00"}, "location": "Pool", "uid": "1",
                                   "organizer": "someone@example.com"}])

    async def pictures(self, request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Type": "multipart/x-mixed-replace;boundary=--frameboundary"})
        await response.prepare(request)
        try:
            for _ in range(600):
                await response.write(b"--frameboundary\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                     + str(len(self.still)).encode() + b"\r\n\r\n" + self.still + b"\r\n")
                await asyncio.sleep(0.1)
        except (ConnectionError, RuntimeError):
            pass
        return response

    def newest(self) -> int:
        return min(len(self.segments) - 1, 3 + int(time.monotonic() - self.started))

    async def master(self, request: web.Request) -> web.Response:
        return web.Response(text="#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=500000\nplaylist.m3u8\n", content_type="application/vnd.apple.mpegurl")

    async def playlist(self, request: web.Request) -> web.Response:
        newest = self.newest()
        first = max(0, newest - 3)
        lines = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-TARGETDURATION:1", f"#EXT-X-MEDIA-SEQUENCE:{first}",
                 "#EXT-X-PART-INF:PART-TARGET=0.5", '#EXT-X-MAP:URI="init.mp4"']
        for n in range(first, newest + 1):
            lines += ["#EXTINF:1.000,", f"segment/{n}.m4s"]
        lines.append(f'#EXT-X-PART:DURATION=0.5,URI="segment/{newest + 1}.0.m4s"')
        return web.Response(text="\n".join(lines) + "\n", content_type="application/vnd.apple.mpegurl")

    async def segment(self, request: web.Request) -> web.Response:
        n = int(request.match_info["n"])
        if n > self.newest():
            raise web.HTTPNotFound()
        return web.Response(body=self.segments[n], content_type="video/iso.segment")

    async def websocket(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "auth_required"})
        auth = await ws.receive_json()
        self.seen_tokens.append(auth.get("access_token", ""))
        if auth.get("access_token") != TOKEN:
            await ws.send_json({"type": "auth_invalid"})
            await ws.close()
            return ws
        await ws.send_json({"type": "auth_ok"})
        async for message in ws:
            if message.type != WSMsgType.TEXT:
                break
            command = json.loads(message.data)
            reply = {"id": command["id"], "type": "result", "success": True}
            if command["type"] == "camera/stream" and command["entity_id"] == "camera.front_door":
                reply["result"] = {"url": f"/api/hls/{STREAM_TOKEN}/master_playlist.m3u8"}
            elif command["type"] == "todo/item/list":
                reply["result"] = {"items": [{"summary": "Milk", "status": "needs_action", "uid": "a"},
                                             {"summary": "Bread", "status": "completed", "due": "2026-11-20", "uid": "b"}]}
            else:
                reply = {"id": command["id"], "type": "result", "success": False,
                         "error": {"code": "home_assistant_error", "message": "Camera does not support HLS"}}
            await ws.send_json(reply)
        return ws

    async def service(self, request: web.Request) -> web.Response:
        if not self.services_found:
            return web.json_response({"message": "Service pleatview.list_screens not found."}, status=400)
        data = await request.json()
        if request.match_info["service"] == "list_screens":
            return web.json_response({"changed_states": [{"entity_id": "lock.front_door"}], "service_response": {"screens": [
                {"id": "a1b2", "name": "Reception", "manufacturer": "Google", "model": "Chromecast", "app_version": "1.1.4",
                 "online": True, "connects_out": True, "token": "never-passed-on"}]}})
        self.enrolled.append(data)
        return web.json_response({"changed_states": [], "service_response": {"screen": data["screen"], "enrolled": True}})


class Options(unittest.TestCase):
    def test_nothing_is_shared_until_the_site_says(self) -> None:
        self.assertFalse(Shared.parse({}).anything)
        self.assertFalse(Shared.parse({"SHARED_ENTITIES": "camera.*"}).anything)
        shared = Shared.parse({"SHARE_HOME_ASSISTANT": "True", "SHARED_ENTITIES": ""})
        self.assertEqual(shared.patterns, ("camera.*",))  # cameras by default
        self.assertFalse(shared.screens)
        self.assertTrue(Shared.parse({"SHARE_SCREENS": "true"}).screens)

    def test_only_the_entities_listed(self) -> None:
        shared = Shared.parse({"SHARE_HOME_ASSISTANT": "true", "SHARED_ENTITIES": "camera.front_door, sensor.kitchen_*"})
        self.assertTrue(shared.allows("camera.front_door"))
        self.assertFalse(shared.allows("camera.back_yard"))
        self.assertTrue(shared.allows("sensor.kitchen_temperature"))
        self.assertFalse(shared.allows("sensor.kitchen_temperature", "camera"))
        for bad in ("camera.front_door/../x", "CAMERA.front_door", "camera", "", None, 5):
            self.assertFalse(shared.allows(bad), bad)
        for bad in ("camera", "lock.*;", "../x"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                Shared.parse({"SHARE_HOME_ASSISTANT": "1", "SHARED_ENTITIES": bad})

    def test_the_way_in(self) -> None:
        given = Access.find({"HA_URL": "https://ha.example.com:8443/", "HA_TOKEN": "t"})
        self.assertEqual((given.root, given.websocket), ("https://ha.example.com:8443", "wss://ha.example.com:8443/api/websocket"))
        app = Access.find({"SUPERVISOR_TOKEN": "s"})
        self.assertEqual((app.root, app.websocket, app.token), ("http://supervisor/core", "ws://supervisor/core/websocket", "s"))
        self.assertIsNone(Access.find({}))
        with self.assertRaises(ValueError):
            Access.find({"HA_URL": "ftp://ha", "HA_TOKEN": "t"})

    def test_screens_go_to_the_controllers_own_address(self) -> None:
        self.assertEqual(server.screens_address("controller.example.com", None), ("wss://controller.example.com/api/pleatview/v2", None))
        self.assertEqual(server.screens_address("wss://Controller.example.com:8790/api/pleatview/renderer", "AB:CD" + ":00" * 30),
                         ("wss://controller.example.com:8790/api/pleatview/v2", "abcd" + "00" * 30))

    def test_the_tokens_stay_in_the_renderer(self) -> None:
        with mock.patch.dict(os.environ, {"SUPERVISOR_TOKEN": "s", "HA_TOKEN": "t", "HASSIO_TOKEN": "h"}):
            server.keep_tokens_here()
            self.assertFalse({"SUPERVISOR_TOKEN", "HA_TOKEN", "HASSIO_TOKEN"} & set(os.environ))

    def test_hls_playlists_whole_segments_only(self) -> None:
        segments, init, target = site_ha.media_playlist(
            '#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:7\n#EXT-X-DISCONTINUITY-SEQUENCE:1\n#EXT-X-MAP:URI="init.mp4"\n'
            '#EXTINF:2.0,\nsegment/7.m4s\n#EXT-X-DISCONTINUITY\n#EXTINF:2.0,\nsegment/8.m4s\n'
            '#EXT-X-PART:DURATION=0.5,URI="segment/9.0.m4s"\n#EXT-X-PRELOAD-HINT:TYPE=PART,URI="segment/9.1.m4s"\n')
        self.assertEqual(segments, [(7, 1, "segment/7.m4s"), (8, 2, "segment/8.m4s")])
        self.assertEqual((init, target), ("init.mp4", 2.0))

    def test_the_log_tells_what_changes(self) -> None:
        self.assertEqual(server.describe_call("ha_stream", {"key": "cam-1", "entity_id": "camera.front_door"}),
                         "stream Home Assistant's camera.front_door as cam-1")
        self.assertEqual(server.describe_call("ha_enrol", {"screen": "a1b2", "token": "x"}), "move the screen a1b2 to this controller")
        self.assertIsNone(server.describe_call("ha_picture", {"entity_id": "camera.front_door"}))  # every few seconds: not logged


class TheCalls(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.files = tempfile.TemporaryDirectory()
        cls.media = media(Path(cls.files.name))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.files.cleanup()

    async def asyncSetUp(self) -> None:
        self.ha_fake = FakeHomeAssistant(*self.media)
        self.ha_server = TestServer(self.ha_fake.app())
        await self.ha_server.start_server()
        self.addAsyncCleanup(self.ha_server.close)
        self.root = tempfile.TemporaryDirectory()
        os.chmod(self.root.name, 0o755)  # ffmpeg runs without root, like Chromium
        self.addCleanup(self.root.cleanup)
        self.renderer = server.Renderer(Path(self.root.name), "x264", 2)
        self.addAsyncCleanup(self.renderer.close)
        root = str(self.ha_server.make_url("")).rstrip("/")
        self.ha = HomeAssistant(Access(root, root.replace("http", "ws", 1) + "/api/websocket", TOKEN),
                                Shared(("camera.front_door", "camera.back_yard", "todo.shopping", "calendar.family"), screens=True))
        self.addAsyncCleanup(self.ha.close)
        self.calls = server.controller_calls(self.renderer, lambda: {"name": "r"}, lambda: 40000, self.ha,
                                             ("wss://controller.example.com/api/pleatview/v2", None))
        self.streams = TestServer(server.create_stream_app(self.renderer))
        await self.streams.start_server()
        self.addAsyncCleanup(self.streams.close)

    async def test_only_what_the_site_shares_and_never_its_token(self) -> None:
        config = await self.calls["ha_config"]({})
        self.assertEqual(config["name"], "Harbour office")
        entities = (await self.calls["ha_entities"]({}))["entities"]
        self.assertEqual([e["entity_id"] for e in entities], ["camera.front_door", "camera.back_yard", "todo.shopping", "calendar.family"])
        self.assertEqual(entities[0], {"entity_id": "camera.front_door", "domain": "camera", "name": "Front door", "state": "streaming"})
        answers = json.dumps([config, entities])
        self.assertNotIn("camera-secret", answers)  # attributes stay behind: a camera's carry a token
        self.assertNotIn(TOKEN, answers)
        picture = await self.calls["ha_picture"]({"entity_id": "camera.front_door"})
        self.assertEqual(base64.b64decode(picture["data"]), self.media[2])
        for call, params in (("ha_picture", {"entity_id": "lock.front_door"}), ("ha_picture", {"entity_id": "todo.shopping"}),
                             ("ha_todo", {"entity_id": "camera.front_door"}), ("ha_stream", {"key": "x", "entity_id": "lock.front_door"}),
                             ("ha_picture", {"entity_id": "camera.front_door/../../config"})):
            with self.subTest(call=call, params=params), self.assertRaises(connect.Refused):
                await self.calls[call](params)
        items = (await self.calls["ha_todo"]({"entity_id": "todo.shopping"}))["items"]
        self.assertEqual(items, [{"summary": "Milk", "status": "needs_action", "due": None},
                                 {"summary": "Bread", "status": "completed", "due": "2026-11-20"}])
        events = (await self.calls["ha_calendar"]({"entity_id": "calendar.family", "start": "2026-11-20T00:00:00+13:00",
                                                    "end": "2026-11-27T00:00:00+13:00"}))["events"]
        self.assertEqual([e["summary"] for e in events], ["Swimming"])
        self.assertNotIn("organizer", events[0])
        self.assertTrue(all(seen in (f"Bearer {TOKEN}", TOKEN) for seen in self.ha_fake.seen_tokens))

    async def test_screens_go_to_this_controller_only(self) -> None:
        screens = (await self.calls["ha_screens"]({}))["screens"]
        self.assertEqual(screens, [{"id": "a1b2", "name": "Reception", "manufacturer": "Google", "model": "Chromecast",
                                    "app_version": "1.1.4", "online": True, "connects_out": True}])
        await self.calls["ha_enrol"]({"screen": "a1b2", "token": "one-time-token-0123456789",
                                      "url": "wss://elsewhere.example/api/pleatview/v2", "fingerprint": "00" * 32})
        self.assertEqual(self.ha_fake.enrolled, [{"screen": "a1b2", "url": "wss://controller.example.com/api/pleatview/v2",
                                                  "token": "one-time-token-0123456789"}])
        for bad in ({"screen": "a1b2", "token": "short"}, {"screen": "../x", "token": "one-time-token-0123456789"}):
            with self.subTest(bad=bad), self.assertRaises(connect.Refused):
                await self.calls["ha_enrol"](bad)
        self.ha_fake.services_found = False
        with self.assertRaisesRegex(connect.Refused, "integration is older"):
            await self.calls["ha_screens"]({})

    async def test_screens_stay_unless_the_site_lets_them_go(self) -> None:
        ha = HomeAssistant(self.ha.access, Shared(("camera.*",), screens=False))
        self.addAsyncCleanup(ha.close)
        calls = server.controller_calls(self.renderer, lambda: {}, lambda: 40000, ha, None)
        for call in ("ha_screens", "ha_enrol"):
            with self.subTest(call=call), self.assertRaises(connect.Refused):
                await calls[call]({"screen": "a1b2", "token": "one-time-token-0123456789"})
        self.assertEqual(self.ha_fake.enrolled, [])
        self.assertNotIn("ha_screens", server.controller_calls(self.renderer, lambda: {}, lambda: 40000))  # nothing shared

    async def wait_for_stream(self, secret: str) -> bytes:
        async with aiohttp.ClientSession() as client:
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                playlist = await client.get(self.streams.make_url(f"/stream/{secret}/index.m3u8"))
                text = await playlist.text() if playlist.status == 200 else ""
                names = [line for line in text.splitlines() if line.endswith(".ts")]
                if names:
                    segment = await client.get(self.streams.make_url(f"/stream/{secret}/{names[-1]}"))
                    self.assertEqual(segment.status, 200)
                    return await segment.read()
                await asyncio.sleep(0.5)
        self.fail("no stream")

    def probe(self, data: bytes) -> list[str]:
        path = Path(self.root.name) / "probe.ts"
        path.write_bytes(data)
        out = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
                             capture_output=True, text=True, check=True).stdout
        return [s["codec_name"] for s in json.loads(out)["streams"]]

    async def test_a_cameras_video_streamed_here_without_the_token_on_a_command_line(self) -> None:
        started: list[tuple] = []
        real = asyncio.create_subprocess_exec

        async def spawn(*args, **kwargs):
            started.append((args, kwargs.get("env")))
            return await real(*args, **kwargs)

        with mock.patch.object(asyncio, "create_subprocess_exec", spawn):
            stream = await self.calls["ha_stream"]({"key": "cam-1", "entity_id": "camera.front_door"})
            secret = stream["stream"].split("/")[2]
            data = await self.wait_for_stream(secret)
        self.assertEqual(self.probe(data), ["h264", "aac"])  # Home Assistant's video and sound, copied
        self.assertEqual(self.renderer.relays["h.cam-1"].kind, "hls")
        for args, env in started:
            self.assertNotIn(TOKEN, " ".join(args))
            self.assertNotIn(STREAM_TOKEN, " ".join(args))
            self.assertNotIn(TOKEN, json.dumps(env or {}))
        # The controller fetches it through the connection too; then it's stopped.
        playlist = await self.calls["segment"]({"key": "cam-1", "name": "index.m3u8"})
        self.assertIn(".ts", base64.b64decode(playlist["data"]).decode())
        await self.calls["stop"]({"key": "cam-1"})
        self.assertNotIn("h.cam-1", self.renderer.relays)
        self.assertNotIn(secret, self.renderer.streams)

    async def test_a_camera_without_video_streams_its_pictures(self) -> None:
        stream = await self.calls["ha_stream"]({"key": "cam-2", "entity_id": "camera.back_yard"})
        data = await self.wait_for_stream(stream["stream"].split("/")[2])
        self.assertEqual(self.probe(data), ["h264"])
        self.assertEqual(self.renderer.relays["h.cam-2"].kind, "mjpeg")

    async def test_cameras_at_once(self) -> None:
        with mock.patch.object(site_ha.Relay, "start", new=mock.Mock()), \
                mock.patch.object(site_ha.Relay, "running", new=property(lambda relay: True)):
            for n in range(site_ha.RELAYS_MOST):
                await self.calls["ha_stream"]({"key": f"cam-{n}", "entity_id": "camera.front_door"})
            with self.assertRaisesRegex(connect.Refused, "at most"):
                await self.calls["ha_stream"]({"key": "cam-9", "entity_id": "camera.front_door"})
            # One nobody watched for a while makes room.
            self.renderer.relays["h.cam-0"].last_access -= server.IDLE_SECONDS + 1
            await self.calls["ha_stream"]({"key": "cam-9", "entity_id": "camera.front_door"})
            self.assertNotIn("h.cam-0", self.renderer.relays)


if __name__ == "__main__":
    unittest.main()
