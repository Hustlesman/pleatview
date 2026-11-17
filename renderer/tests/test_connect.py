"""Connecting out to a controller elsewhere (docs/renderer-connect-out.md): the filter that keeps
its pages off the site's network, the signed connection over TLS, the calls it may make, and
its sessions kept apart from the local API's. Run inside the renderer's image."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import ipaddress
import json
import secrets
import ssl
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import aiohttp
from aiohttp import WSMsgType, web
from aiohttp.test_utils import AioHTTPTestCase, TestServer
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from pleatview_renderer import connect, filter as page_filter, server, session
from pleatview_renderer.filter import Filter, Places, public


class TheFilterRules(unittest.IsolatedAsyncioTestCase):
    def test_only_the_public_internet(self) -> None:
        for text in ("8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"):
            self.assertTrue(public(ipaddress.ip_address(text)), text)
        for text in ("127.0.0.1", "10.1.2.3", "172.30.32.2", "192.168.1.1", "169.254.169.254", "100.64.0.1", "0.0.0.0",
                     "224.0.0.251", "255.255.255.255", "240.0.0.1", "198.18.0.1", "192.0.0.8", "::1", "::", "fe80::1",
                     "fc00::1", "fd12::1", "ff02::fb", "::ffff:127.0.0.1", "::ffff:8.8.8.8", "64:ff9b::a00:1",
                     "2002:c0a8:101::1", "2001:0:4136:e378::1", "2001:db8::1", "::127.0.0.1"):
            self.assertFalse(public(ipaddress.ip_address(text)), text)

    def test_the_sites_places(self) -> None:
        places = Places.parse("homeassistant.local:8123, 192.168.1.40/32,[fd00::5]:80")
        self.assertTrue(places.named("HomeAssistant.local.", 8123))
        self.assertFalse(places.named("homeassistant.local", 80))
        self.assertTrue(places.holds(ipaddress.ip_address("192.168.1.40"), 9999))
        self.assertTrue(places.holds(ipaddress.ip_address("fd00::5"), 80))
        self.assertFalse(places.holds(ipaddress.ip_address("192.168.1.41"), 80))
        self.assertFalse(Places.parse(""))
        for bad in ("homeassistant.local", "x:0", "x:99999", "300.1.1.1/8"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                Places.parse(bad)

    async def test_names_are_looked_up_and_each_answer_checked(self) -> None:
        answers = {"evil.example": ["127.0.0.1"], "mixed.example": ["10.0.0.1", "1.1.1.1"],
                   "homeassistant.local": ["192.168.1.10"], "gone.example": OSError("no such name")}

        async def resolve(host: str, port: int) -> list:
            found = answers[host]
            if isinstance(found, Exception):
                raise found
            return [ipaddress.ip_address(a) for a in found]
        rules = Filter(Places.parse("homeassistant.local:8123,192.168.1.40/32"), resolve=resolve)
        self.assertEqual(await rules.destinations("evil.example", 443), [])  # rebound to this machine
        self.assertEqual(await rules.destinations("mixed.example", 443), [ipaddress.ip_address("1.1.1.1")])
        self.assertEqual(await rules.destinations("homeassistant.local", 8123), [ipaddress.ip_address("192.168.1.10")])
        self.assertEqual(await rules.destinations("homeassistant.local", 22), [])
        self.assertEqual(await rules.destinations("gone.example", 80), [])
        self.assertEqual(await rules.destinations("localhost", 8780), [])
        self.assertEqual(await rules.destinations("supervisor", 80), [])
        self.assertEqual(await rules.destinations("192.168.1.40", 5000), [ipaddress.ip_address("192.168.1.40")])
        self.assertEqual(await rules.destinations("192.168.1.41", 80), [])
        self.assertEqual(await rules.destinations("[::1]", 8780), [])


async def socks(port: int, host: str, target_port: int, command: int = 1) -> tuple[int, asyncio.StreamReader, asyncio.StreamWriter]:
    """A SOCKS5 CONNECT through the filter, as the browser does; the reply code and the tunnel."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(bytes([5, 1, 0]))
    assert await reader.readexactly(2) == bytes([5, 0])
    literal = page_filter.ip(host)
    if literal is None:
        address = bytes([3, len(host)]) + host.encode()
    else:
        address = (bytes([1]) if literal.version == 4 else bytes([4])) + literal.packed
    writer.write(bytes([5, command, 0]) + address + struct.pack("!H", target_port))
    reply = await reader.readexactly(10)
    return reply[1], reader, writer


class TheFilterProxy(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.write(await reader.read(100))
            await writer.drain()
            writer.close()
        self.echo = await asyncio.start_server(echo, "127.0.0.1", 0)
        self.echo_port = self.echo.sockets[0].getsockname()[1]
        # Only the echo server, as if the site had listed it.
        self.filter = Filter(Places([f"127.0.0.1:{self.echo_port}"]))
        self.port = await self.filter.start()

    async def asyncTearDown(self) -> None:
        await self.filter.stop()
        self.echo.close()
        await self.echo.wait_closed()

    async def test_a_tunnel_where_it_may_go_and_none_elsewhere(self) -> None:
        code, reader, writer = await socks(self.port, "127.0.0.1", self.echo_port)
        self.assertEqual(code, 0)
        writer.write(b"hello")
        self.assertEqual(await asyncio.wait_for(reader.read(100), 5), b"hello")
        writer.close()
        for host, port in (("127.0.0.1", self.echo_port + 1), ("localhost", self.echo_port), ("169.254.169.254", 80),
                           ("10.0.0.1", 80), ("::1", self.echo_port)):
            code, _, writer = await socks(self.port, host, port)
            self.assertEqual(code, page_filter.NOT_ALLOWED, f"{host}:{port}")
            writer.close()
        code, _, writer = await socks(self.port, "127.0.0.1", self.echo_port, command=2)  # BIND
        self.assertEqual(code, page_filter.NOT_SUPPORTED)
        writer.close()

    async def test_nothing_but_socks5(self) -> None:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write(b"GET http://127.0.0.1/ HTTP/1.1\r\nHost: x\r\n\r\n")
        self.assertIn(await asyncio.wait_for(reader.read(10), 5), (b"", bytes([5, 0xFF])))
        writer.close()


class TheBrowser(unittest.TestCase):
    def test_a_controllers_pages_go_through_the_filter(self) -> None:
        screen = server.parse_screen({"url": "https://example.com/"})
        args = session.chromium_args(screen, Path("/tmp/p"), 9301, proxy=40000)
        self.assertIn("--proxy-server=socks5://127.0.0.1:40000", args)
        self.assertIn("--proxy-bypass-list=<-loopback>", args)
        self.assertIn("--disable-background-networking", args)
        self.assertIn("--force-webrtc-ip-handling-policy=disable_non_proxied_udp", args)
        self.assertFalse(any(a.startswith("--proxy") for a in session.chromium_args(screen, Path("/tmp/p"), 9301)))


class Addresses(unittest.TestCase):
    def test_the_controllers_address(self) -> None:
        self.assertEqual(connect.address("Controller.Example.com"), "wss://controller.example.com/api/pleatview/renderer")
        self.assertEqual(connect.address("wss://c.example:443/api/pleatview/renderer"), "wss://c.example/api/pleatview/renderer")
        self.assertEqual(connect.address("wss://c.example:8790"), "wss://c.example:8790/api/pleatview/renderer")
        self.assertEqual(connect.address("wss://[2001:db8::1]:8790/"), "wss://[2001:db8::1]:8790/api/pleatview/renderer")
        for bad in ("ws://c.example", "https://c.example", "wss://c.example/other", "wss://u:p@c.example", "wss://c.example/?x=1", ""):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                connect.address(bad)
        self.assertEqual(connect.fingerprint("AB:" * 31 + "AB"), bytes([0xAB] * 32))
        self.assertIsNone(connect.fingerprint(" "))
        with self.assertRaises(ValueError):
            connect.fingerprint("abc")


class FakeController:
    """A controller over TLS with its own certificate: challenges, checks signatures, hands out a
    code, welcomes, asks, and removes."""

    def __init__(self, folder: Path) -> None:
        self.cert, self.key = folder / "c.pem", folder / "k.pem"
        server.make_certificate(self.cert, self.key)
        self.pin = server.certificate_fingerprint(self.cert)
        self.keys: dict[str, ec.EllipticCurvePublicKey] = {}
        self.seen: list[dict] = []
        self.welcomed = asyncio.Event()
        self.script: list = []  # what to do after welcome: dicts to send, or ("close", code)
        self.answers: list[dict] = []
        self.done = asyncio.Event()
        self.url = ""

    async def handle(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(protocols=(connect.SUBPROTOCOL,))
        await ws.prepare(request)
        nonce = secrets.token_hex(32)
        await ws.send_json({"type": "challenge", "nonce": nonce})
        hello = await ws.receive_json()
        self.seen.append(hello)
        text = connect.signed_text(self.url, nonce).encode()
        if hello["type"] == "enrol":
            public_key = serialization.load_der_public_key(base64.b64decode(hello["key"]))
            public_key.verify(base64.b64decode(hello["signature"]), text, ec.ECDSA(hashes.SHA256()))
            der = base64.b64decode(hello["key"])
            import hashlib
            self.keys[hashlib.sha256(der).hexdigest()] = public_key
            await ws.send_json({"type": "code", "code": "K7RM-W2XD"})
        else:
            public_key = self.keys.get(hello["key"])
            if public_key is None:
                await ws.close(code=connect.UNKNOWN_KEY)
                return ws
            public_key.verify(base64.b64decode(hello["signature"]), text, ec.ECDSA(hashes.SHA256()))
        await ws.send_json({"type": "welcome"})
        self.welcomed.set()
        for step in self.script:
            if isinstance(step, tuple):
                await ws.close(code=step[1])
                self.done.set()
                return ws
            await ws.send_json(step)
            if step.get("type") == "request":
                message = await ws.receive()
                while message.type == WSMsgType.TEXT and json.loads(message.data).get("type") != "response":
                    message = await ws.receive()
                if message.type == WSMsgType.TEXT:
                    self.answers.append(json.loads(message.data))
        self.done.set()
        async for _ in ws:
            pass
        return ws


class TheConnection(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        folder = Path(self.tmp.name)
        self.controller = FakeController(folder)
        app = web.Application()
        self.handler = self.controller.handle

        async def route(request: web.Request) -> web.WebSocketResponse:
            return await self.handler(request)
        app.router.add_get(connect.PATH, route)
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.load_cert_chain(self.controller.cert, self.controller.key)
        self.http = TestServer(app, host="127.0.0.1")
        await self.http.start_server(ssl=context)
        self.controller.url = connect.address(f"wss://127.0.0.1:{self.http.port}")
        self.state = folder / "state"
        self.state.mkdir()
        self.shown: list[dict] = []

        async def show(params: dict) -> dict:
            if params.get("url", "").startswith("ftp:"):
                raise connect.Refused("url must be an http or https address")
            self.shown.append(params)
            return {"stream": "/stream/x/index.m3u8"}

        async def info(params: dict) -> dict:
            return {"name": "Site renderer"}
        self.calls = {"show": show, "info": info}
        self.patches = [mock.patch.object(connect, "FIRST_WAIT", 0.05)]
        for patch in self.patches:
            patch.start()

    async def asyncTearDown(self) -> None:
        for patch in self.patches:
            patch.stop()
        await self.http.close()
        self.tmp.cleanup()

    def link(self, pin: str | None = "own") -> connect.Link:
        return connect.Link(self.state, f"wss://127.0.0.1:{self.http.port}",
                            self.controller.pin if pin == "own" else pin, calls=self.calls,
                            info=lambda: {"name": "Site renderer"}, write=server.write_private)

    async def test_enrols_with_a_code_signs_in_after_and_answers_only_its_calls(self) -> None:
        self.controller.script = [
            {"type": "request", "id": 1, "method": "show", "params": {"key": "tv", "url": "https://example.com/"}},
            {"type": "request", "id": 2, "method": "show", "params": {"key": "tv", "url": "ftp://x"}},
            {"type": "request", "id": "x3", "method": "set_options", "params": {"site_pages": "0.0.0.0/0"}},
            {"type": "request", "id": 4, "method": "info", "params": []},
        ]
        link = self.link()
        with self.assertLogs("pleatview_renderer.connect", "WARNING") as logged:
            link.start()
            await asyncio.wait_for(self.controller.done.wait(), 10)
        self.assertIn("K7RM-W2XD", "\n".join(logged.output))  # the code, for the site to pass on
        self.assertEqual(self.controller.seen[0]["type"], "enrol")
        self.assertEqual(self.controller.answers, [
            {"type": "response", "id": 1, "result": {"stream": "/stream/x/index.m3u8"}},
            {"type": "response", "id": 2, "error": "url must be an http or https address"},
            {"type": "response", "id": "x3", "error": "unknown method"},
            {"type": "response", "id": 4, "error": "params is a JSON object"},
        ])
        self.assertTrue(json.loads((self.state / "controller.json").read_text())["enrolled"])
        self.assertEqual((self.state / "renderer-key.pem").stat().st_mode & 0o777, 0o600)
        await link.stop()
        # Next time it signs in with its key.
        self.controller.done.clear()
        self.controller.script = []
        link = self.link()
        link.start()
        await asyncio.wait_for(self.controller.done.wait(), 10)
        self.assertEqual(self.controller.seen[-1]["type"], "auth")
        self.assertEqual(self.controller.seen[-1]["key"], link.key_id)
        await link.stop()

    async def test_removed_it_stays_away(self) -> None:
        self.controller.script = [("close", connect.REVOKED)]
        link = self.link()
        stopped = []

        async def removed() -> None:
            stopped.append(True)
        link.on_removed = removed
        with self.assertLogs("pleatview_renderer.connect", "WARNING") as logged:
            await asyncio.wait_for(link.run(), 10)
        self.assertTrue(link.removed)
        self.assertEqual(stopped, [True])  # what it showed for the controller stops
        self.assertIn("removed this renderer", "\n".join(logged.output))
        self.assertFalse(json.loads((self.state / "controller.json").read_text())["enrolled"])

    async def test_never_with_a_certificate_it_doesnt_trust(self) -> None:
        for pin in (None, "00" * 32):  # not the system's, nor the one pinned
            link = self.link(pin)
            with self.assertLogs("pleatview_renderer.connect", "INFO"):
                link.start()
                await asyncio.sleep(0.5)
            await link.stop()
            self.assertEqual(self.controller.seen, [], pin)

    async def test_another_controller_means_enrolling_again(self) -> None:
        (self.state / "controller.json").write_text(json.dumps({"url": "wss://old.example/api/pleatview/renderer", "enrolled": True}))
        link = self.link()
        self.assertEqual(link.state, {"url": self.controller.url, "enrolled": False})

    async def test_too_many_requests_close_it(self) -> None:
        closed: list[int | None] = []

        async def flood(request: web.Request) -> web.WebSocketResponse:
            ws = web.WebSocketResponse(protocols=(connect.SUBPROTOCOL,))
            await ws.prepare(request)
            await ws.send_json({"type": "challenge", "nonce": secrets.token_hex(32)})
            await ws.receive_json()
            await ws.send_json({"type": "welcome"})
            for n in range(connect.MOST_A_SECOND + 5):
                await ws.send_json({"type": "request", "id": n, "method": "info", "params": {}})
            message = await ws.receive()
            while message.type == WSMsgType.TEXT:
                message = await ws.receive()
            closed.append(ws.close_code)
            return ws
        self.handler = flood
        link = self.link()
        link._save(enrolled=True)
        with self.assertLogs("pleatview_renderer.connect", "WARNING") as logged:
            task = asyncio.create_task(link.run())
            for _ in range(100):
                if closed:
                    break
                await asyncio.sleep(0.05)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.assertEqual(closed[:1], [connect.BAD_MESSAGE])
        self.assertIn("faster than this renderer takes them", "\n".join(logged.output))


class TheCalls(AioHTTPTestCase):
    async def get_application(self):
        self.root = tempfile.TemporaryDirectory()
        patches = [mock.patch.object(session.Session, name, new=mock.AsyncMock()) for name in ("stop", "update", "overlay")]
        patches.append(mock.patch.object(session.Session, "start", new=mock.Mock()))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.root.cleanup)
        self.renderer = server.Renderer(Path(self.root.name), "x264", 2)
        self.calls = server.controller_calls(self.renderer, lambda: {"name": "r"}, lambda: 40000)
        self.streams = TestServer(server.create_stream_app(self.renderer))
        await self.streams.start_server()
        self.addAsyncCleanup(self.streams.close)
        return server.create_app(Path(self.root.name), "x264", renderer=self.renderer)

    async def test_shown_apart_from_the_local_apis_and_streamed_at_a_secret(self) -> None:
        shown = await self.calls["show"]({"key": "tv-1", "url": "https://example.com/", "width": 1080, "height": 1920})
        secret = shown["stream"].split("/")[2]
        self.assertRegex(secret, r"^[0-9a-f]{32}$")
        session_ = self.renderer.sessions["c.tv-1"]
        self.assertEqual((session_.proxy, session_.screen.width), (40000, 1080))  # through the filter
        for bad in ({"key": "tv-1", "url": "https://x/", "ha_url": "http://ha:8123", "token": "t"},
                    {"key": "../x", "url": "https://x/"}, {"key": "tv-2", "url": "file:///data/renderer-key.pem"}):
            with self.subTest(bad=bad), self.assertRaises(connect.Refused):
                await self.calls["show"](bad)
        await self.calls["show"]({"key": "tv-2", "url": "https://example.com/"})
        with self.assertRaises(connect.Refused):
            await self.calls["show"]({"key": "tv-3", "url": "https://example.com/"})  # no room
        # The local API neither sees nor touches them.
        info = await (await self.client.get("/api/info")).json()
        self.assertEqual(info["sessions"], {})
        self.assertEqual((await self.client.delete("/api/sessions/c.tv-1")).status, 204)
        self.assertIn("c.tv-1", self.renderer.sessions)
        folder = self.renderer.sessions["c.tv-1"].folder
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "s3.ts").write_bytes(b"\x47" * 188)
        self.assertEqual((await self.client.get("/hls/c.tv-1/s3.ts")).status, 404)
        # The site's screens fetch it at its secret address, and nothing else there.
        async with aiohttp.ClientSession() as client:
            ok = await client.get(self.streams.make_url(f"/stream/{secret}/s3.ts"))
            self.assertEqual((ok.status, ok.headers["X-Content-Type-Options"]), (200, "nosniff"))
            self.assertEqual(await ok.read(), b"\x47" * 188)
            for path in (f"/stream/{'0' * 32}/s3.ts", f"/stream/{secret}/../renderer-key.pem", "/api/info", "/hls/tv-1/s3.ts"):
                self.assertEqual((await client.get(self.streams.make_url(path))).status, 404, path)
        segment = await self.calls["segment"]({"key": "tv-1", "name": "s3.ts"})
        self.assertEqual(base64.b64decode(segment["data"]), b"\x47" * 188)
        with self.assertRaises(connect.Refused):
            await self.calls["segment"]({"key": "tv-1", "name": "../../renderer-key.pem"})
        await self.calls["stop"]({"key": "tv-1"})
        self.assertNotIn("c.tv-1", self.renderer.sessions)
        self.assertNotIn(secret, self.renderer.streams)

    def test_a_controllers_screen_keeps_its_sound(self) -> None:
        from pleatview_renderer import sound
        folders = self.renderer.folders
        fifo = folders.profile(folders.name("c.tv-1")) / "sound"
        self.assertTrue(sound.alsa_device(fifo))  # a path the renderer named, which ALSA takes

    def test_the_log_tells_what_changes(self) -> None:
        self.assertEqual(server.describe_call("show", {"key": "tv", "url": "https://x/"}), "show tv: https://x/ (1920×1080)")
        self.assertEqual(server.describe_call("stop", {"key": "tv"}), "stop tv")
        self.assertIsNone(server.describe_call("segment", {"key": "tv"}))
        told = server.describe_call("show", {"key": "tv", "url": "https://x/\n2026-11-16 INFO Added to the controller"})
        self.assertNotIn("\n", told)  # nothing from the controller starts a line of its own


if __name__ == "__main__":
    unittest.main()
