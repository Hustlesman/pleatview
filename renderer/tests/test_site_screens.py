"""The site's screens, found through Home Assistant and added to this renderer's controller
(site_screens.py): someone at the screen allows it, the screen goes only to this renderer's own
controller, and the renderer lets go of its pairing afterwards."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer

from pleatview_renderer import connect, server, site_screens
from pleatview_renderer.site_ha import Access, HomeAssistant, Shared
from pleatview_renderer.site_screens import SiteScreens

TOKEN = "the-sites-own-token-0123456789"
ONE_TIME = "one-time-token-from-the-controller-0123"
CONTROLLER = ("wss://controller.example.com/api/pleatview/v2", "ab" * 32)


class FakeScreen:
    """A screen's local API, as far as pairing and enrolling go."""

    def __init__(self, screen_id: str = "found-1", pairing: tuple[str, ...] = ("pin", "approve"),
                 features: tuple[str, ...] = ("protocol_v2",)) -> None:
        self.id, self.pairing, self.features = screen_id, list(pairing), list(features)
        self.tokens: set[str] = set()
        self.revoked: list[str] = []
        self.enrolled: list[dict] = []
        self.answer = "pending"  # how the person at the screen answers the request
        self.clients: list[str] = []

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/api/info", self.info)
        app.router.add_get("/api/state", self.state)
        app.router.add_post("/api/pair", self.pair)
        app.router.add_post("/api/pair/request", self.request)
        app.router.add_get("/api/pair/request/{id}", self.requested)
        app.router.add_post("/api/enrol", self.enrol)
        app.router.add_post("/api/unpair", self.unpair)
        return app

    def bearer(self, request: web.Request) -> str:
        return request.headers.get("Authorization", "").removeprefix("Bearer ")

    async def info(self, request: web.Request) -> web.Response:
        return web.json_response({"id": self.id, "name": "Lobby", "pairing": self.pairing,
                                  **({"features": self.features} if self.bearer(request) in self.tokens else {})})

    async def state(self, request: web.Request) -> web.Response:
        if self.bearer(request) not in self.tokens:
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response({"features": self.features})

    async def pair(self, request: web.Request) -> web.Response:
        body = await request.json()
        if body.get("pin") != "123456":
            return web.json_response({"error": "invalid_pin"}, status=403)
        self.clients.append(body.get("client"))
        self.tokens.add("v1-token-from-the-pin")
        return web.json_response({"token": "v1-token-from-the-pin"})

    async def request(self, request: web.Request) -> web.Response:
        if self.answer == "busy":  # someone else's request waits on the screen
            return web.json_response({"error": "busy", "retry_after": 30}, status=429)
        self.clients.append((await request.json()).get("client"))
        return web.json_response({"request": "req1", "expires_in": 120})

    async def requested(self, request: web.Request) -> web.Response:
        if self.answer == "approved":
            self.tokens.add("v1-token-from-ok")
            return web.json_response({"state": "approved", "token": "v1-token-from-ok"})
        return web.json_response({"state": self.answer})

    async def enrol(self, request: web.Request) -> web.Response:
        if self.bearer(request) not in self.tokens:
            return web.json_response({"error": "unauthorized"}, status=401)
        self.enrolled.append(await request.json())
        return web.json_response({}, status=202)

    async def unpair(self, request: web.Request) -> web.Response:
        token = self.bearer(request)
        self.tokens.discard(token)
        self.revoked.append(token)
        return web.json_response({})


class FakeHomeAssistant:
    """The flows in progress (Home Assistant's discoveries) and pleatview.list_screens."""

    def __init__(self, flows: list[dict], listed: list[dict] | None) -> None:
        self.flows, self.listed = flows, listed

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/api/websocket", self.websocket)
        app.router.add_post("/api/services/{domain}/{service}", self.service)
        return app

    async def websocket(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "auth_required"})
        auth = await ws.receive_json()
        await ws.send_json({"type": "auth_ok" if auth.get("access_token") == TOKEN else "auth_invalid"})
        async for message in ws:
            if message.type != WSMsgType.TEXT:
                break
            command = json.loads(message.data)
            assert command["type"] == "config_entries/flow/progress", command
            await ws.send_json({"id": command["id"], "type": "result", "success": True, "result": self.flows})
        return ws

    async def service(self, request: web.Request) -> web.Response:
        if self.listed is None:
            return web.json_response({"message": "Service pleatview.list_screens not found."}, status=400)
        return web.json_response({"changed_states": [], "service_response": {"screens": self.listed}})


def flow(screen_id: str, name: str, url: str | None, **more) -> dict:
    context = {"source": "zeroconf", "unique_id": screen_id, "title_placeholders": {"name": name}}
    if url:
        context["configuration_url"] = url
    return {"flow_id": f"f-{screen_id}", "handler": "pleatview", "step_id": "pair", "context": context, **more}


class SiteScreensTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.screen = FakeScreen()
        self.screen_server = TestServer(self.screen.app(), host="127.0.0.1")
        await self.screen_server.start_server()
        self.addAsyncCleanup(self.screen_server.close)
        at = f"http://127.0.0.1:{self.screen_server.port}"
        self.ha_fake = FakeHomeAssistant(
            [flow("found-1", "Lobby", at),
             flow("older-1", "Older integration", None),  # no address: integration 1.1.3
             {**flow("renderer-1", "A renderer", at), "step_id": "renderer"},
             {**flow("other", "Someone else's", at), "handler": "hue"},
             flow("../x", "Bad id", at)],
            [{"id": "in-ha-1", "name": "Kitchen", "connects_out": True, "host": "127.0.0.1", "port": self.screen_server.port,
              "token": "never-passed-on"}])
        self.ha_server = TestServer(self.ha_fake.app())
        await self.ha_server.start_server()
        self.addAsyncCleanup(self.ha_server.close)
        root = str(self.ha_server.make_url("")).rstrip("/")
        self.ha = HomeAssistant(Access(root, root.replace("http", "ws", 1) + "/api/websocket", TOKEN), Shared())
        self.addAsyncCleanup(self.ha.close)
        self.site = SiteScreens(self.ha, CONTROLLER, "controller.example.com")
        self.addAsyncCleanup(self.site.close)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.renderer = server.Renderer(Path(folder.name), "x264", 1)
        self.addAsyncCleanup(self.renderer.close)
        self.calls = server.controller_calls(self.renderer, lambda: {}, lambda: 40000, None, None, self.site)
        patcher = mock.patch.object(site_screens, "ASK_EVERY", 0.05)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_the_screens_home_assistant_found_and_has_without_their_addresses(self) -> None:
        found = (await self.calls["site_screens"]({}))["screens"]
        self.assertEqual(found, [
            {"id": "in-ha-1", "name": "Kitchen", "in_home_assistant": True, "connects_out": True},
            {"id": "found-1", "name": "Lobby", "in_home_assistant": False},
        ])
        self.assertNotIn("127.0.0.1", json.dumps(found))
        self.assertNotIn("never-passed-on", json.dumps(found))
        self.ha_fake.listed = None  # no screen in Home Assistant: its integration's services aren't there
        self.site._fresh = -60
        self.assertEqual([s["id"] for s in (await self.calls["site_screens"]({}))["screens"]], ["found-1"])

    async def test_added_with_its_pin_then_let_go(self) -> None:
        with self.assertRaisesRegex(connect.Refused, "Wrong PIN"):
            await self.calls["site_add"]({"screen": "found-1", "how": "pin", "pin": "000000", "token": ONE_TIME})
        self.assertEqual(self.screen.enrolled, [])
        answer = await self.calls["site_add"]({"screen": "found-1", "how": "pin", "pin": "123456", "token": ONE_TIME,
                                               # an address or controller in the call is never used
                                               "url": "wss://elsewhere.example/api/pleatview/v2", "host": "10.9.9.9"})
        self.assertEqual(answer, {"added": True})
        self.assertEqual(self.screen.enrolled, [{"url": CONTROLLER[0], "token": ONE_TIME, "fingerprint": CONTROLLER[1]}])
        self.assertEqual(self.screen.revoked, ["v1-token-from-the-pin"])
        self.assertEqual(self.screen.tokens, set())
        self.assertEqual(self.screen.clients, ["controller.example.com"])  # who asks, as the screen shows it

    async def test_added_once_someone_presses_ok_on_it(self) -> None:
        ticket = (await self.calls["site_add"]({"screen": "found-1", "how": "ok", "token": ONE_TIME}))["ticket"]
        self.assertEqual(await self.calls["site_asked"]({"ticket": ticket}), {"state": "pending"})
        self.screen.answer = "approved"
        for _ in range(100):
            if (await self.calls["site_asked"]({"ticket": ticket}))["state"] != "pending":
                break
            await asyncio.sleep(0.02)
        self.assertEqual(await self.calls["site_asked"]({"ticket": ticket}), {"state": "added"})
        self.assertEqual(self.screen.enrolled, [{"url": CONTROLLER[0], "token": ONE_TIME, "fingerprint": CONTROLLER[1]}])
        self.assertEqual(self.screen.revoked, ["v1-token-from-ok"])
        self.assertEqual(await self.calls["site_asked"]({"ticket": "made-up"}), {"state": "expired"})

    async def test_not_added_when_the_person_at_the_screen_says_no(self) -> None:
        ticket = (await self.calls["site_add"]({"screen": "found-1", "how": "ok", "token": ONE_TIME}))["ticket"]
        self.screen.answer = "denied"
        for _ in range(100):
            if (await self.calls["site_asked"]({"ticket": ticket}))["state"] != "pending":
                break
            await asyncio.sleep(0.02)
        self.assertEqual(await self.calls["site_asked"]({"ticket": ticket}), {"state": "denied"})
        self.assertEqual(self.screen.enrolled, [])

    async def test_only_screens_found_here_and_able_to_connect_out(self) -> None:
        for params, said in (
            ({"screen": "10.0.0.5", "how": "pin", "pin": "123456", "token": ONE_TIME}, "hasn't found that screen"),
            ({"screen": "older-1", "how": "pin", "pin": "123456", "token": ONE_TIME}, "hasn't found that screen"),
            ({"screen": "found-1", "how": "pin", "pin": "12", "token": ONE_TIME}, "6 digits"),
            ({"screen": "found-1", "how": "pin", "pin": "123456", "token": "short"}, "one-time token"),
            ({"screen": "found-1", "how": "wave", "token": ONE_TIME}, "how is ok"),
        ):
            with self.subTest(params=params), self.assertRaisesRegex(connect.Refused, said):
                await self.calls["site_add"](params)
        self.screen.id = "someone-else"  # another screen answers at that address now
        with self.assertRaisesRegex(connect.Refused, "another screen answers"):
            await self.calls["site_add"]({"screen": "found-1", "how": "pin", "pin": "123456", "token": ONE_TIME})
        self.screen.id, self.screen.features = "found-1", []
        with self.assertRaisesRegex(connect.Refused, "can't connect out"):
            await self.calls["site_add"]({"screen": "found-1", "how": "pin", "pin": "123456", "token": ONE_TIME})
        self.assertEqual(self.screen.enrolled, [])
        self.assertEqual(self.screen.tokens, set())  # let go of all the same
        self.screen.answer = "busy"
        with self.assertRaisesRegex(connect.Refused, "asking about another pairing already"):
            await self.calls["site_add"]({"screen": "found-1", "how": "ok", "token": ONE_TIME})
        self.screen.features, self.screen.pairing = ["protocol_v2"], ["pin"]
        with self.assertRaisesRegex(connect.Refused, "PIN only"):
            await self.calls["site_add"]({"screen": "found-1", "how": "ok", "token": ONE_TIME})

    async def test_said_so_when_the_site_doesnt_let_its_screens_be_found(self) -> None:
        calls = server.controller_calls(self.renderer, lambda: {}, lambda: 40000)
        for call in server.SITE_CALLS:
            with self.subTest(call=call), self.assertRaisesRegex(connect.Refused, "Find screens"):
                await calls[call]({})

    def test_the_log_tells_who_was_added_never_the_pin_or_token(self) -> None:
        said = server.describe_call("site_add", {"screen": "found-1", "how": "pin", "pin": "123456", "token": ONE_TIME})
        self.assertIn("found-1", said)
        self.assertNotIn("123456", said)
        self.assertNotIn(ONE_TIME, said)
        self.assertIsNone(server.describe_call("site_screens", {}))


if __name__ == "__main__":
    unittest.main()
