"""Console tests. Run inside the image (CI does): python3 -m unittest discover -v tests"""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import sqlite3
import stat
import struct
import subprocess
import sys
import tempfile
import time
import unittest

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pleatview_console import auth, keys, mdns, rendering, schedule, totp  # noqa: E402
from pleatview_console import store as store_module  # noqa: E402
from pleatview_console.screens import Invalid, Screens, check_address, command_params  # noqa: E402
from pleatview_console.content import create_content_app  # noqa: E402
from pleatview_console.server import LIBRARY, MEDIA_MAX, create_app  # noqa: E402
from pleatview_console.store import Store  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
HEADERS = {"X-PleatView": "1"}
PASSWORD = "correct horse battery"


class FakeTv:
    """A PleatView screen's protocol v1 API, enough for pairing, state and commands."""

    def __init__(self) -> None:
        self.pin = "123456"
        self.tokens: set[str] = set()
        self.commands: list[dict] = []
        self.config = {"ha_url": ""}
        self.state = {"power": "on", "view": "dashboard", "foreground": True, "url": "http://x/", "app_version": "1.0.17",
                      "features": ["home_without_ha"]}
        app = web.Application()
        app.router.add_get("/api/info", self.info)
        app.router.add_post("/api/pair", self.pair)
        app.router.add_post("/api/unpair", self.unpair)
        app.router.add_get("/api/events", self.events)
        app.router.add_get("/api/config", self.get_config)
        app.router.add_post("/api/config", self.post_config)
        self.config_posts: list[dict] = []
        app.router.add_post("/api/command", self.command)
        self.server = TestServer(app, host="127.0.0.1")

    def authed(self, request: web.Request) -> str:
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if token not in self.tokens:
            raise web.HTTPUnauthorized()
        return token

    async def info(self, request):
        return web.json_response({"id": "tv-1", "name": "Lobby TV", "manufacturer": "Acme", "model": "Box",
                                  "app_version": "1.0.16", "api_version": 1, "paired": bool(self.tokens)})

    async def pair(self, request):
        data = await request.json()
        if data.get("pin") != self.pin:
            return web.json_response({"error": "invalid_pin"}, status=403)
        token = f"token-{len(self.tokens)}"
        self.tokens.add(token)
        return web.json_response({"token": token})

    async def unpair(self, request):
        self.tokens.discard(self.authed(request))
        return web.json_response({})

    async def get_config(self, request):
        self.authed(request)
        return web.json_response(self.config)

    async def post_config(self, request):
        self.authed(request)
        update = await request.json()
        self.config_posts.append(update)
        self.config.update(update)
        return web.json_response(self.config)

    async def command(self, request):
        self.authed(request)
        self.commands.append(await request.json())
        return web.json_response({})

    async def events(self, request):
        self.authed(request)
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await response.write(f"event: state\ndata: {json.dumps(self.state)}\n\n".encode())
        try:
            while True:
                await asyncio.sleep(0.2)
                await response.write(b": ping\n\n")
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return response


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.store = Store(self.data, keys.load_master_key(self.data / "master.key"))
        self.session = aiohttp.ClientSession()
        self.screens = Screens(self.store, self.session, discovery=False)
        await self.screens.start()

    async def asyncTearDown(self) -> None:
        await self.screens.stop()
        await self.session.close()
        self.store.close()
        self.tmp.cleanup()


class TvClientCopy(unittest.TestCase):
    def test_same_client_as_the_integration(self) -> None:
        console = REPO / "console" / "pleatview_console" / "tvapi.py"
        integration = REPO / "custom_components" / "pleatview" / "api.py"
        self.assertEqual(console.read_text(), integration.read_text(), "copy api.py to tvapi.py again")


class Passwords(unittest.TestCase):
    def test_hash_and_check(self) -> None:
        stored = auth.hash_password(PASSWORD)
        self.assertTrue(stored.startswith("scrypt$15$8$3$"))
        self.assertTrue(auth.check_password(PASSWORD, stored))
        self.assertFalse(auth.check_password(PASSWORD + "!", stored))
        self.assertFalse(auth.check_password(PASSWORD, "garbage"))
        self.assertNotEqual(stored, auth.hash_password(PASSWORD))  # salted

    def test_new_password_rules(self) -> None:
        self.assertIsNotNone(auth.check_new_password("short"))
        self.assertIsNone(auth.check_new_password(PASSWORD))

    def test_throttle(self) -> None:
        now = [0.0]
        throttle = auth.Throttle(clock=lambda: now[0])
        for _ in range(4):
            throttle.failed("user:a", "address:1")
        self.assertEqual(throttle.wait("user:a"), 0)
        throttle.failed("user:a", "address:1")
        self.assertGreater(throttle.wait("user:a"), 55)
        self.assertGreater(throttle.wait("user:other", "address:1"), 55)  # same address
        throttle.failed("user:a")
        self.assertGreater(throttle.wait("user:a"), 115)  # doubles
        now[0] += 200
        self.assertEqual(throttle.wait("user:a"), 0)
        throttle.succeeded("user:a")
        throttle.failed("user:a")
        self.assertEqual(throttle.wait("user:a"), 0)


class Keys(unittest.TestCase):
    def test_master_key_is_private_and_stable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "master.key"
            first = keys.load_master_key(path)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(first, keys.load_master_key(path))
            self.assertEqual(len(first), 32)

    def test_sealed_secrets_stay_in_their_place(self) -> None:
        master = os.urandom(32)
        wrapped = keys.new_tenant_key(master, "aaaa")
        sealer = keys.Sealer(master, "aaaa", wrapped)
        sealed = sealer.seal("secret", "screen:tv-1")
        self.assertNotIn("secret", sealed)
        self.assertEqual(sealer.open(sealed, "screen:tv-1"), "secret")
        with self.assertRaises(Exception):
            sealer.open(sealed, "screen:tv-2")  # copied to another screen
        with self.assertRaises(Exception):
            keys.Sealer(master, "bbbb", wrapped)  # another tenant's id
        with self.assertRaises(Exception):
            keys.Sealer(os.urandom(32), "aaaa", wrapped)  # without the master key

    def test_certificate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cert, key = Path(tmp) / "cert.pem", Path(tmp) / "key.pem"
            keys.ensure_certificate(cert, key)
            self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
            self.assertRegex(keys.fingerprint(cert), r"^([0-9A-F]{2}:){31}[0-9A-F]{2}$")


class Storage(unittest.TestCase):
    def test_tenants_are_separate_databases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp), os.urandom(32))
            a, b = store.add_tenant("A"), store.add_tenant("B")
            store.tenant(a).add_site("Only in A")
            self.assertEqual(store.tenant(b).sites(), [])
            self.assertTrue((Path(tmp) / "tenants" / a / "tenant.db").exists())
            store.delete_tenant(a)
            self.assertFalse((Path(tmp) / "tenants" / a).exists())
            with self.assertRaises(KeyError):
                store.tenant(a)
            with self.assertRaises(KeyError):
                store.tenant("../../etc")
            store.close()

    def test_sessions_hashed_and_ended_by_password_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp), os.urandom(32))
            user = store.add_user("ann", "Ann", "x")
            token = store.add_session(user)
            self.assertEqual(store.session_user(token)["username"], "ann")
            raw = Path(tmp, "console.db").read_bytes() + Path(tmp, "console.db-wal").read_bytes()
            self.assertNotIn(token.encode(), raw)
            store.set_password(user, "y")
            self.assertIsNone(store.session_user(token))
            store.close()


def _name(name: str) -> bytes:
    return b"".join(bytes([len(p)]) + p.encode() for p in name.rstrip(".").split(".")) + b"\0"


def _record(name: bytes, rtype: int, ttl: int, data: bytes) -> bytes:
    return name + struct.pack("!HHIH", rtype, 0x8001, ttl, len(data)) + data


class Mdns(unittest.TestCase):
    def response(self) -> bytes:
        instance = _name("Lobby TV._pleatview._tcp.local.")
        txt = b"".join(bytes([len(s)]) + s for s in (b"id=tv-1", b"name=Lobby TV", b"api=1", b"version=1.0.16"))
        # The SRV target uses compression: a pointer to "local" inside the first name.
        header = struct.pack("!6H", 0, 0x8400, 0, 4, 0, 0)
        service = _name("_pleatview._tcp.local.")
        ptr = _record(service, mdns.PTR, 4500, instance)
        local_offset = 12 + len(service) - len(b"\x05local\x00")
        target = b"\x07lobbytv" + struct.pack("!H", 0xC000 | local_offset)
        srv = _record(instance, mdns.SRV, 120, struct.pack("!HHH", 0, 0, 8765) + target)
        return header + ptr + srv + _record(instance, mdns.TXT, 4500, txt) + _record(
            b"\x07lobbytv" + struct.pack("!H", 0xC000 | local_offset), mdns.A, 120, socket.inet_aton("192.168.1.20"))

    def test_finds_a_screen(self) -> None:
        browser = mdns.Browser()
        browser.datagram_received(self.response(), ("192.168.1.20", 5353))
        found = browser.found()
        self.assertEqual(found["tv-1"], {"id": "tv-1", "name": "Lobby TV", "host": "192.168.1.20",
                                         "port": 8765, "version": "1.0.16", "api": "1", "scheme": "http"})
        self.assertEqual(browser.found(mdns.RENDERER_SERVICE), {})

    def test_ignores_garbage_and_loops(self) -> None:
        self.assertEqual(mdns.parse(b"\x00" * 5), [])
        looped = struct.pack("!6H", 0, 0x8400, 0, 1, 0, 0) + struct.pack("!H", 0xC00C)
        self.assertEqual(mdns.parse(looped), [])

    def test_query(self) -> None:
        packet = mdns.query((mdns.SERVICE, mdns.PTR))
        self.assertEqual(packet[:12], struct.pack("!6H", 0, 0, 1, 0, 0, 0))
        self.assertIn(b"\x0a_pleatview\x04_tcp\x05local\x00", packet)


class Validation(unittest.TestCase):
    def test_commands(self) -> None:
        self.assertEqual(command_params("show_url", {"url": "https://a.example/"})["url"], "https://a.example/")
        self.assertEqual(command_params("home", {"evil": 1}), {})
        for command, data in [
            ("show_url", {"url": "javascript:alert(1)"}),
            ("show_url", {"url": "https://a/", "duration": -1}),
            ("play_media", {"url": "file:///etc/passwd"}),
            ("notify", {}),
            ("forget_sign_ins", {}),
            ("reboot", {}),
        ]:
            with self.assertRaises(Invalid, msg=command):
                command_params(command, data)
        self.assertEqual(command_params("play_media", {"url": "rtsp://cam/1"})["url"], "rtsp://cam/1")

    def test_addresses(self) -> None:
        self.assertEqual(check_address("192.168.1.20", 8765), ("192.168.1.20", 8765))
        self.assertEqual(check_address("fe80::1", 8765), ("fe80::1", 8765))
        for host in ("8.8.8.8", "127.0.0.1", "0.0.0.0", "screen.example.com", "224.0.0.251"):
            with self.assertRaises(Invalid, msg=host):
                check_address(host, 8765)
        with self.assertRaises(Invalid):
            check_address("192.168.1.20", 70000)


class ApiBase(Base):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.client = TestClient(TestServer(create_app(self.store, self.screens, https=False)))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await super().asyncTearDown()

    async def setup_console(self) -> dict:
        response = await self.client.post("/api/setup", headers=HEADERS, json={
            "username": "ann", "name": "Ann", "password": PASSWORD, "tenant": "Acme", "site": "HQ"})
        self.assertEqual(response.status, 200, await response.text())
        return await response.json()


class Api(ApiBase):
    async def test_first_start_then_sign_in(self) -> None:
        status = await (await self.client.get("/api/status")).json()
        self.assertTrue(status["setup"])
        me = await self.setup_console()
        self.assertTrue(me["user"]["admin"])
        self.assertEqual(me["tenants"][0]["role"], "owner")
        again = await self.client.post("/api/setup", headers=HEADERS, json={
            "username": "eve", "name": "Eve", "password": PASSWORD, "tenant": "X", "site": "Y"})
        self.assertEqual(again.status, 409)
        await self.client.post("/api/logout", headers=HEADERS)
        self.assertEqual((await self.client.get(f"/api/t/{me['tenants'][0]['id']}")).status, 401)
        wrong = await self.client.post("/api/login", headers=HEADERS, json={"username": "ann", "password": "nope"})
        self.assertEqual(wrong.status, 401)
        right = await self.client.post("/api/login", headers=HEADERS, json={"username": "ANN", "password": PASSWORD})
        self.assertEqual(right.status, 200)
        cookie = right.headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)

    async def test_changes_need_the_header_and_our_origin(self) -> None:
        response = await self.client.post("/api/setup", json={})
        self.assertEqual(response.status, 403)
        response = await self.client.post("/api/setup", headers={**HEADERS, "Origin": "https://evil.example"}, json={})
        self.assertEqual(response.status, 403)

    async def test_security_headers_and_web_app(self) -> None:
        for path in ("/", "/app.js", "/sw.js", "/manifest.webmanifest", "/icon-192.png"):
            response = await self.client.get(path)
            self.assertEqual(response.status, 200, path)
            self.assertIn("script-src 'self'", response.headers["Content-Security-Policy"])
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        for path in ("/../server.py", "/%2e%2e/server.py", "/nope.js", "/store.py"):
            self.assertEqual((await self.client.get(path)).status, 404, path)
        manifest = await (await self.client.get("/manifest.webmanifest")).json()
        self.assertEqual(manifest["display"], "standalone")

    async def test_tenants_are_silos(self) -> None:
        me = await self.setup_console()
        own = me["tenants"][0]["id"]
        other = self.store.add_tenant("Someone else")  # the admin isn't a member of it
        self.assertEqual((await self.client.get(f"/api/t/{other}")).status, 404)
        self.assertEqual((await self.client.get(f"/api/t/{other}/screens")).status, 404)
        self.assertEqual((await self.client.get("/api/t/0000000000000000")).status, 404)
        self.assertEqual((await self.client.get(f"/api/t/{own}")).status, 200)

    async def test_roles(self) -> None:
        me = await self.setup_console()
        tenant = me["tenants"][0]["id"]
        viewer = self.store.add_user("vic", "Vic", auth.hash_password(PASSWORD))
        self.store.set_member(viewer, tenant, "viewer")
        await self.client.post("/api/logout", headers=HEADERS)
        await self.client.post("/api/login", headers=HEADERS, json={"username": "vic", "password": PASSWORD})
        self.assertEqual((await self.client.get(f"/api/t/{tenant}/screens")).status, 200)
        response = await self.client.post(f"/api/t/{tenant}/sites", headers=HEADERS, json={"name": "New"})
        self.assertEqual(response.status, 403)
        response = await self.client.post(f"/api/t/{tenant}/screens", headers=HEADERS,
                                          json={"host": "192.168.1.20", "pin": "123456", "site_id": 1})
        self.assertEqual(response.status, 403)
        response = await self.client.post("/api/tenants", headers=HEADERS, json={"name": "Mine"})
        self.assertEqual(response.status, 403)

    async def test_typed_addresses_stay_local(self) -> None:
        me = await self.setup_console()
        tenant = me["tenants"][0]["id"]
        for host in ("127.0.0.1", "8.8.8.8", "localhost"):
            response = await self.client.post(f"/api/t/{tenant}/screens", headers=HEADERS,
                                              json={"host": host, "pin": "123456", "site_id": 1})
            self.assertEqual(response.status, 400, host)

    async def test_password_change(self) -> None:
        await self.setup_console()
        for _ in range(5):
            response = await self.client.post("/api/password", headers=HEADERS,
                                              json={"current": "wrong", "password": PASSWORD + "2"})
            self.assertEqual(response.status, 403)
        response = await self.client.post("/api/password", headers=HEADERS,
                                          json={"current": PASSWORD, "password": PASSWORD + "2"})
        self.assertEqual(response.status, 429)  # as slow as guessing at the sign-in page
        self.app_throttle().succeeded("user:ann")
        response = await self.client.post("/api/password", headers=HEADERS,
                                          json={"current": PASSWORD, "password": "short"})
        self.assertEqual(response.status, 400)
        response = await self.client.post("/api/password", headers=HEADERS,
                                          json={"current": PASSWORD, "password": PASSWORD + "2"})
        self.assertEqual(response.status, 200)
        status = await (await self.client.get("/api/status")).json()
        self.assertEqual(status["user"]["username"], "ann")  # still signed in here

    def app_throttle(self):
        from pleatview_console.server import THROTTLE
        return self.client.server.app[THROTTLE]

    async def test_sign_in_slows_down(self) -> None:
        await self.setup_console()
        for _ in range(5):
            await self.client.post("/api/login", headers=HEADERS, json={"username": "ann", "password": "wrong"})
        response = await self.client.post("/api/login", headers=HEADERS, json={"username": "ann", "password": PASSWORD})
        self.assertEqual(response.status, 429)


class TwoFactorAndMembers(ApiBase):
    async def login(self, username: str, password: str, code: str | None = None):
        data = {"username": username, "password": password}
        if code is not None:
            data["code"] = code
        await self.client.post("/api/logout", headers=HEADERS)
        return await self.client.post("/api/login", headers=HEADERS, json=data)

    async def enable_two_factor(self) -> tuple[str, list[str]]:
        response = await self.client.post("/api/2fa/setup", headers=HEADERS, json={"password": PASSWORD})
        self.assertEqual(response.status, 200)
        secret = (await response.json())["secret"]
        step = int(time.time() // 30)
        response = await self.client.post("/api/2fa/enable", headers=HEADERS, json={"code": totp.code_at(secret, step)})
        self.assertEqual(response.status, 200, await response.text())
        return secret, (await response.json())["recovery_codes"]

    async def test_two_factor_sign_in(self) -> None:
        await self.setup_console()
        secret, codes = await self.enable_two_factor()
        self.assertEqual(len(codes), 10)
        response = await self.login("ann", PASSWORD)
        self.assertEqual(response.status, 401)
        self.assertTrue((await response.json())["two_factor"])
        self.assertEqual((await self.login("ann", PASSWORD, "000000")).status, 401)
        step = int(time.time() // 30) + 1  # the next step, so it's newer than the one used to enable
        self.assertEqual((await self.login("ann", PASSWORD, totp.code_at(secret, step))).status, 200)
        self.assertEqual((await self.login("ann", PASSWORD, totp.code_at(secret, step))).status, 401)  # used once
        self.assertEqual((await self.login("ann", PASSWORD, codes[0].upper())).status, 200)
        self.assertEqual((await self.login("ann", PASSWORD, codes[0])).status, 401)  # recovery codes too
        self.assertEqual((await self.login("ann", "wrong", codes[1])).status, 401)

    async def test_console_can_require_two_factor(self) -> None:
        me = await self.setup_console()
        tenant = me["tenants"][0]["id"]
        response = await self.client.post("/api/settings", headers=HEADERS, json={"require_2fa": True})
        self.assertEqual(response.status, 409)  # not before the administrator has it
        await self.enable_two_factor()
        response = await self.client.post("/api/settings", headers=HEADERS, json={"require_2fa": True})
        self.assertEqual(response.status, 200)
        added = await (await self.client.post(f"/api/t/{tenant}/members", headers=HEADERS, json={
            "username": "bob", "name": "Bob", "role": "operator"})).json()
        await self.login("bob", added["password"])
        await self.client.post("/api/password", headers=HEADERS, json={"current": added["password"], "password": PASSWORD})
        response = await self.client.get(f"/api/t/{tenant}/screens")
        self.assertEqual(response.status, 403)
        self.assertEqual((await response.json())["action"], "two_factor")
        self.assertEqual((await self.client.post("/api/2fa/disable", headers=HEADERS, json={"password": PASSWORD})).status, 409)

    async def test_members(self) -> None:
        me = await self.setup_console()
        tenant = me["tenants"][0]["id"]
        response = await self.client.post(f"/api/t/{tenant}/members", headers=HEADERS, json={
            "username": "bob", "name": "Bob", "role": "viewer"})
        self.assertEqual(response.status, 200)
        bob = await response.json()
        self.assertGreaterEqual(len(bob["password"]), 12)
        taken = await self.client.post(f"/api/t/{tenant}/members", headers=HEADERS, json={
            "username": "bob", "name": "Bob", "role": "viewer"})
        self.assertEqual(taken.status, 409)  # an administrator adds existing accounts; this one is a member
        members = (await (await self.client.get(f"/api/t/{tenant}/members")).json())["members"]
        self.assertEqual({m["username"]: m["managed"] for m in members}, {"ann": False, "bob": True})
        ann_id = next(m["id"] for m in members if m["username"] == "ann")
        last_owner = await self.client.patch(f"/api/t/{tenant}/members/{ann_id}", headers=HEADERS, json={"role": "viewer"})
        self.assertEqual(last_owner.status, 409)

        # Bob's first sign-in: his own password before anything else.
        await self.login("bob", bob["password"])
        blocked = await self.client.get(f"/api/t/{tenant}/screens")
        self.assertEqual((await blocked.json())["action"], "change_password")
        same = await self.client.post("/api/password", headers=HEADERS, json={"current": bob["password"], "password": bob["password"]})
        self.assertEqual(same.status, 400)
        changed = await self.client.post("/api/password", headers=HEADERS, json={"current": bob["password"], "password": PASSWORD})
        self.assertEqual(changed.status, 200)
        self.assertEqual((await self.client.get(f"/api/t/{tenant}/screens")).status, 200)
        self.assertEqual((await self.client.get(f"/api/t/{tenant}/members")).status, 403)  # viewers don't manage

        # Ann resets Bob's password, then removes him; his account goes with it.
        await self.login("ann", PASSWORD)
        reset = await self.client.post(f"/api/t/{tenant}/members/{bob['id']}/reset", headers=HEADERS, json={"reset": "password"})
        new_password = (await reset.json())["password"]
        self.assertEqual((await self.login("bob", PASSWORD)).status, 401)
        self.assertEqual((await self.login("bob", new_password)).status, 200)
        await self.login("ann", PASSWORD)
        removed = await self.client.delete(f"/api/t/{tenant}/members/{bob['id']}", headers=HEADERS)
        self.assertEqual(removed.status, 200)
        self.assertIsNone(self.store.user_by_name("bob"))

    async def test_owners_cant_reset_shared_accounts(self) -> None:
        me = await self.setup_console()
        first = me["tenants"][0]["id"]
        second = (await (await self.client.post("/api/tenants", headers=HEADERS, json={"name": "Second"})).json())["id"]
        carol = await (await self.client.post(f"/api/t/{first}/members", headers=HEADERS, json={
            "username": "carol", "name": "Carol", "role": "owner"})).json()
        dave = await (await self.client.post(f"/api/t/{first}/members", headers=HEADERS, json={
            "username": "dave", "name": "Dave", "role": "viewer"})).json()
        both = await self.client.post(f"/api/t/{second}/members", headers=HEADERS, json={"username": "dave", "role": "viewer"})
        self.assertEqual(both.status, 200)  # an administrator adds an existing account
        await self.login("carol", carol["password"])
        await self.client.post("/api/password", headers=HEADERS, json={"current": carol["password"], "password": PASSWORD})
        response = await self.client.post(f"/api/t/{first}/members/{dave['id']}/reset", headers=HEADERS, json={"reset": "password"})
        self.assertEqual(response.status, 403)
        self.assertEqual((await self.client.get(f"/api/t/{second}")).status, 404)  # Carol can't see the other tenant


class Totp(unittest.TestCase):
    def test_rfc_6238_vector(self) -> None:
        secret = base64.b32encode(b"12345678901234567890").decode()
        self.assertEqual(totp.code_at(secret, 59 // 30), "287082")  # RFC 6238 appendix B, 6 digits
        self.assertEqual(totp.code_at(secret, 1111111109 // 30), "081804")
        self.assertEqual(totp.verify(secret, "081 804", 0, now=1111111109), 1111111109 // 30)
        self.assertIsNone(totp.verify(secret, "081804", 1111111109 // 30, now=1111111109))

    def test_migration_from_the_first_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = sqlite3.connect(Path(tmp) / "console.db")
            db.executescript(f"{store_module.CONSOLE_V1} PRAGMA user_version = 1;")
            db.execute("INSERT INTO users (username, name, password, created) VALUES ('old', 'Old', 'x', 0)")
            db.commit()
            db.close()
            store = Store(Path(tmp), os.urandom(32))
            self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], len(store_module.CONSOLE_SCHEMA))
            self.assertEqual(store.user_by_name("old")["must_change"], 0)
            store.close()


class Pairing(Base):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.tv = FakeTv()
        await self.tv.server.start_server()
        self.tenant = self.store.add_tenant("Acme")
        self.site = self.store.tenant(self.tenant).add_site("HQ")

    async def asyncTearDown(self) -> None:
        await super().asyncTearDown()
        await self.tv.server.close()

    async def wait_online(self, screen_id: int) -> dict:
        for _ in range(50):
            view = next(s for s in self.screens.list(self.tenant) if s["id"] == screen_id)
            if view["online"] and view["managed_by"] is None and view["state"]:
                return view
            await asyncio.sleep(0.05)
        self.fail("the screen never came online")

    async def test_pair_follow_command_unpair(self) -> None:
        port = self.tv.server.port
        with self.assertRaises(Exception):
            await self.screens.pair(self.tenant, self.site, "127.0.0.1", port, "000000")
        screen_id = await self.screens.pair(self.tenant, self.site, "127.0.0.1", port, "123456")
        stored = self.store.tenant(self.tenant).screen(screen_id)
        self.assertNotIn("token-0", stored["token"])  # sealed with the tenant's key
        view = await self.wait_online(screen_id)
        self.assertEqual(view["name"], "Lobby TV")
        self.assertEqual(view["model"], "Acme Box")
        self.assertEqual(view["app_version"], "1.0.17")
        self.assertTrue(view["home_without_ha"])
        await self.screens.command(self.tenant, screen_id, "show_url",
                                   command_params("show_url", {"url": "https://example.com/"}))
        self.assertEqual(self.tv.commands, [{"command": "show_url", "url": "https://example.com/"}])
        with self.assertRaises(Invalid):
            await self.screens.pair(self.tenant, self.site, "127.0.0.1", port, "123456")  # already paired
        await self.screens.unpair(self.tenant, screen_id)
        self.assertEqual(self.tv.tokens, set())
        self.assertEqual(self.store.tenant(self.tenant).screens(), [])

    async def test_settings_stay_on_the_screen(self) -> None:
        screen_id = await self.screens.pair(self.tenant, self.site, "127.0.0.1", self.tv.server.port, "123456")
        await self.wait_online(screen_id)
        await asyncio.sleep(0.1)
        await self.screens.configure(self.tenant, screen_id, {"home_dashboard": "https://example.com/board", "zoom": 80})
        self.assertEqual(self.tv.config["home_dashboard"], "https://example.com/board")
        self.assertEqual(len(self.tv.config["rev"]), 16)
        sent = len(self.tv.config_posts)
        await self.restart_screens()
        await self.wait_online(screen_id)
        await asyncio.sleep(0.2)
        self.assertEqual(len(self.tv.config_posts), sent)  # unchanged: nothing sent again
        self.tv.config = {"ha_url": ""}  # the screen was reset
        await self.restart_screens()
        await self.wait_online(screen_id)
        for _ in range(20):
            if self.tv.config.get("home_dashboard"):
                break
            await asyncio.sleep(0.05)
        self.assertEqual(self.tv.config["home_dashboard"], "https://example.com/board")

    async def test_home_assistant_keeps_its_screens(self) -> None:
        self.tv.config = {"ha_url": "http://ha.local:8123"}
        screen_id = await self.screens.pair(self.tenant, self.site, "127.0.0.1", self.tv.server.port, "123456")
        for _ in range(50):
            if self.screens.list(self.tenant)[0]["managed_by"]:
                break
            await asyncio.sleep(0.05)
        with self.assertRaises(Invalid):
            await self.screens.configure(self.tenant, screen_id, {"home_dashboard": "https://example.com/"})
        self.assertEqual(self.tv.config_posts, [])

    async def restart_screens(self) -> None:
        await self.screens.stop()
        self.screens = Screens(self.store, self.session, discovery=False)
        await self.screens.start()

    async def test_links_come_back_after_a_restart(self) -> None:
        screen_id = await self.screens.pair(self.tenant, self.site, "127.0.0.1", self.tv.server.port, "123456")
        await self.screens.stop()
        self.screens = Screens(self.store, self.session, discovery=False)
        await self.screens.start()
        await self.wait_online(screen_id)



def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-y", *args], check=True)


def probe_codecs(path: Path) -> list[str]:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(path)],
                         check=True, capture_output=True, text=True).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


class MediaLibrary(ApiBase):
    """Uploads in other formats come out playable; playlists and media reach screens."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.files = Path(self.tmp.name) / "samples"
        self.files.mkdir()
        me = await self.setup_console()
        self.tenant_id = me["tenants"][0]["id"]
        self.content = TestClient(TestServer(create_content_app(self.store)))
        await self.content.start_server()
        self.library = self.client.server.app[LIBRARY]

    async def asyncTearDown(self) -> None:
        await self.content.close()
        await super().asyncTearDown()

    async def upload(self, path: Path) -> int:
        response = await self.client.post(f"/api/t/{self.tenant_id}/media?name={path.name}", headers=HEADERS,
                                          data=path.read_bytes())
        self.assertEqual(response.status, 200, await response.text())
        media_id = (await response.json())["id"]
        await self.library.convert(self.tenant_id, media_id)
        return media_id

    def item(self, media_id: int) -> dict:
        return self.store.tenant(self.tenant_id).media_item(media_id)

    async def test_formats(self) -> None:
        webm = self.files / "clip.webm"
        ffmpeg("-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=1", "-f", "lavfi", "-i", "sine=duration=1",
               "-c:v", "libvpx-vp9", "-c:a", "libopus", "-shortest", str(webm))
        mp4 = self.files / "ready.mp4"
        ffmpeg("-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(mp4))
        flac = self.files / "tone.flac"
        ffmpeg("-f", "lavfi", "-i", "sine=duration=1", str(flac))
        png = self.files / "still.png"
        ffmpeg("-f", "lavfi", "-i", "testsrc2=size=64x48", "-frames:v", "1", str(png))
        evil = self.files / "evil.m3u8"
        evil.write_text("#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\nfile:///etc/passwd\n#EXT-X-ENDLIST\n")

        converted = self.item(await self.upload(webm))
        self.assertEqual((converted["status"], converted["kind"], converted["playable"]), ("ready", "video", "play.mp4"))
        folder = self.store.data / "tenants" / self.tenant_id / "media" / str(converted["id"])
        self.assertEqual(sorted(probe_codecs(folder / "play.mp4")), ["aac", "h264"])
        self.assertTrue((folder / "thumb.jpg").is_file())
        self.assertEqual(converted["summary"]["video"]["codec"], "vp9")

        copied = self.item(await self.upload(mp4))
        self.assertEqual(copied["playable"], "play.mp4")
        self.assertEqual(probe_codecs(self.store.data / "tenants" / self.tenant_id / "media" / str(copied["id"]) / "play.mp4"), ["h264"])
        self.assertEqual(self.item(await self.upload(flac))["playable"], "play.m4a")
        self.assertEqual(self.item(await self.upload(png))[("playable")], "play.png")
        failed = self.item(await self.upload(evil))
        self.assertEqual(failed["status"], "failed")
        self.assertIsNone(failed["playable"])

    async def test_pages_groups_and_what_screens_load(self) -> None:
        png = self.files / "still.png"
        ffmpeg("-f", "lavfi", "-i", "testsrc2=size=64x48", "-frames:v", "1", str(png))
        media_id = await self.upload(png)
        base = f"/api/t/{self.tenant_id}"
        bad = await self.client.post(base + "/pages", headers=HEADERS, json={"name": "X", "kind": "playlist", "items": [{"media_id": 999}]})
        self.assertEqual(bad.status, 400)
        response = await self.client.post(base + "/pages", headers=HEADERS, json={
            "name": "Lobby loop", "kind": "playlist", "items": [{"media_id": media_id, "seconds": 5}]})
        page_id = (await response.json())["id"]
        pages = (await (await self.client.get(base + "/pages")).json())["pages"]
        address = pages[0]["address"]
        path = address.split("8791", 1)[1] if "8791" in address else address.split("://", 1)[1].split("/", 1)[1]
        path = "/" + path.lstrip("/")
        player = await self.content.get(path)
        self.assertEqual(player.status, 200)
        self.assertIn("player.js", await player.text())
        self.assertIn("default-src 'none'", player.headers["Content-Security-Policy"])
        items = (await (await self.content.get(path + "/items")).json())["items"]
        self.assertEqual([(i["kind"], i["seconds"]) for i in items], [("image", 5)])
        picture = await self.content.get(items[0]["url"])
        self.assertEqual(picture.status, 200)
        self.assertEqual(picture.headers["Content-Type"], "image/png")
        for wrong in (path.replace(path.rsplit("/", 1)[1], "x" * 24), items[0]["url"].replace("play.png", "original"),
                      f"/play/{self.tenant_id}/m/{'y' * 24}/play.png", "/play/player.js"):
            self.assertEqual((await self.content.get(wrong)).status, 200 if wrong.endswith("player.js") else 404, wrong)

        web_page = await (await self.client.post(base + "/pages", headers=HEADERS, json={
            "name": "Menu", "kind": "web", "url": "https://example.com/menu"})).json()
        self.assertEqual((await self.client.post(base + "/pages", headers=HEADERS, json={
            "name": "Bad", "kind": "web", "url": "javascript:alert(1)"})).status, 400)
        group = await (await self.client.post(base + "/groups", headers=HEADERS, json={"name": "Lobby", "screens": [42]})).json()
        self.assertNotIn("id", group)  # not a screen of this organisation
        group = await (await self.client.post(base + "/groups", headers=HEADERS, json={"name": "Lobby", "screens": []})).json()
        result = await (await self.client.post(f"{base}/groups/{group['id']}/action", headers=HEADERS,
                                               json={"command": "home"})).json()
        self.assertEqual(result, {"done": 0, "failed": []})
        self.assertEqual((await self.client.delete(f"{base}/pages/{page_id}", headers=HEADERS)).status, 200)
        self.assertEqual((await self.client.delete(f"{base}/media/{media_id}", headers=HEADERS)).status, 200)
        self.assertFalse((self.store.data / "tenants" / self.tenant_id / "media" / str(media_id)).exists())
        self.assertEqual(web_page["id"] > 0, True)

    async def test_upload_limit(self) -> None:
        self.client.server.app[MEDIA_MAX] = 10
        response = await self.client.post(f"/api/t/{self.tenant_id}/media?name=big", headers=HEADERS, data=b"x" * 100)
        self.assertEqual(response.status, 413)
        self.assertEqual(self.store.tenant(self.tenant_id).media(), [])


class HomePages(Pairing):
    async def test_a_page_as_home(self) -> None:
        screen_id = await self.screens.pair(self.tenant, self.site, "127.0.0.1", self.tv.server.port, "123456")
        await self.wait_online(screen_id)
        await asyncio.sleep(0.1)
        tenant = self.store.tenant(self.tenant)
        page_id = tenant.save_page(None, name="Menu", kind="web", url="https://example.com/menu", items=[])
        await self.screens.configure(self.tenant, screen_id, {"zoom": 90}, page_id)
        self.assertEqual(self.tv.config["home_dashboard"], "https://example.com/menu")
        tenant.save_page(page_id, name="Menu", kind="web", url="https://example.com/menu2", items=[])
        await self.screens.refresh(self.tenant, tenant.screens_with_page(page_id))
        self.assertEqual(self.tv.config["home_dashboard"], "https://example.com/menu2")
        playlist = tenant.save_page(None, name="Loop", kind="playlist", url=None, items=[])
        await self.screens.configure(self.tenant, screen_id, {}, playlist)
        token = tenant.page(playlist)["token"]
        self.assertEqual(self.tv.config["home_dashboard"], f"http://localhost:8791/play/{self.tenant}/p/{token}")




class Schedules(unittest.TestCase):
    def entry(self, **kw):
        return {"id": 1, "target": "site:1", "page_id": 1, "days": "12345", "start": "08:00", "end": "17:00", **kw}

    def test_times(self) -> None:
        tue_9 = datetime(2026, 11, 10, 9, 0)  # a Tuesday
        self.assertTrue(schedule.is_on(self.entry(), tue_9))
        self.assertFalse(schedule.is_on(self.entry(), tue_9.replace(hour=17)))
        self.assertFalse(schedule.is_on(self.entry(days="67"), tue_9))
        night = self.entry(start="22:00", end="06:00", days="1")  # Monday night into Tuesday
        self.assertTrue(schedule.is_on(night, datetime(2026, 11, 9, 23, 0)))
        self.assertTrue(schedule.is_on(night, datetime(2026, 11, 10, 5, 59)))
        self.assertFalse(schedule.is_on(night, datetime(2026, 11, 10, 22, 30)))
        self.assertTrue(schedule.is_on(self.entry(start="00:00", end="00:00", days="2"), tue_9))

    def test_the_most_specific_wins_in_the_sites_time_zone(self) -> None:
        screen = {"id": 5, "site_id": 1}
        entries = [self.entry(id=1, page_id=10), self.entry(id=2, target="group:3", page_id=20),
                   self.entry(id=3, target="screen:5", page_id=30, start="12:00", end="13:00")]
        # 20:30 UTC on Monday is 09:30 on Tuesday in Auckland.
        now = datetime(2026, 11, 9, 20, 30, tzinfo=timezone.utc)
        self.assertEqual(schedule.active(entries, screen, [3], "Pacific/Auckland", now)["page_id"], 20)
        self.assertEqual(schedule.active(entries, screen, [], "Pacific/Auckland", now)["page_id"], 10)
        self.assertIsNone(schedule.active(entries, screen, [3], "UTC", now))  # Monday 20:30 there
        self.assertFalse(schedule.valid_zone("Mars/Olympus"))


class Organisations(ApiBase):
    async def test_schedules_rename_export_and_delete(self) -> None:
        me = await self.setup_console()
        tenant_id = me["tenants"][0]["id"]
        base = f"/api/t/{tenant_id}"
        site = self.store.tenant(tenant_id).sites()[0]
        page = await (await self.client.post(base + "/pages", headers=HEADERS, json={
            "name": "Menu", "kind": "web", "url": "https://example.com/"})).json()
        bad = await self.client.post(base + "/schedules", headers=HEADERS, json={
            "name": "x", "target": "site:999", "page_id": page["id"], "days": "1", "start": "08:00", "end": "09:00"})
        self.assertEqual(bad.status, 400)
        good = await self.client.post(base + "/schedules", headers=HEADERS, json={
            "name": "Breakfast", "target": f"site:{site['id']}", "page_id": page["id"], "days": "71", "start": "06:00", "end": "10:30"})
        self.assertEqual(good.status, 200, await good.text())
        self.assertEqual((await (await self.client.get(base + "/schedules")).json())["schedules"][0]["days"], "17")
        zone = await self.client.patch(f"{base}/sites/{site['id']}", headers=HEADERS, json={"name": "HQ", "timezone": "Europe/Amsterdam"})
        self.assertEqual(zone.status, 200)
        self.assertEqual((await self.client.patch(f"{base}/sites/{site['id']}", headers=HEADERS,
                                                  json={"name": "HQ", "timezone": "Nowhere/Land"})).status, 400)
        renamed = await (await self.client.patch(base, headers=HEADERS, json={"name": "Acme Ltd"})).json()
        self.assertEqual(renamed["tenants"][0]["name"], "Acme Ltd")
        export = await self.client.get(base + "/export")
        data = await export.json()
        self.assertEqual(data["organisation"]["name"], "Acme Ltd")
        self.assertEqual(data["sites"][0]["timezone"], "Europe/Amsterdam")
        self.assertEqual(len(data["schedules"]), 1)
        self.assertNotIn("token", json.dumps(data["screens"]))

        other = (await (await self.client.post("/api/tenants", headers=HEADERS, json={"name": "Other"})).json())["id"]
        added = await (await self.client.post(f"/api/t/{other}/members", headers=HEADERS, json={
            "username": "olga", "name": "Olga", "role": "owner"})).json()
        listed = (await (await self.client.get("/api/tenants")).json())["tenants"]
        self.assertEqual(sorted(t["name"] for t in listed), ["Acme Ltd", "Other"])
        refused = await self.client.delete(f"/api/tenants/{other}", headers=HEADERS, json={"confirm": "other"})
        self.assertEqual(refused.status, 400)
        deleted = await self.client.delete(f"/api/tenants/{other}", headers=HEADERS, json={"confirm": "Other"})
        self.assertEqual(deleted.status, 200)
        self.assertFalse((self.store.data / "tenants" / other).exists())
        self.assertIsNone(self.store.user(added["id"]))  # Olga had nothing else to open




class FakeRenderer:
    """A paired PleatView Renderer's API: pairing, sessions and the stream's files."""

    def __init__(self) -> None:
        self.sessions: dict[str, dict] = {}
        app = web.Application()
        app.router.add_get("/api/info", self.info)
        app.router.add_post("/api/pair", self.pair)
        app.router.add_put("/api/sessions/{key}", self.put)
        app.router.add_get("/hls/{key}/{name}", self.hls)
        self.server = TestServer(app, host="127.0.0.1")

    def authed(self, request) -> None:
        if request.headers.get("Authorization") != "Bearer r-token":
            raise web.HTTPUnauthorized()

    async def info(self, request):
        return web.json_response({"id": "rack-a", "name": "Rack A", "encoder": "x264", "max_screens": 4,
                                  "paired": True, "sessions": self.sessions})

    async def pair(self, request):
        if (await request.json()).get("pin") != "654321":
            return web.json_response({"error": "invalid_pin"}, status=403)
        return web.json_response({"token": "r-token"})

    async def put(self, request):
        self.authed(request)
        self.sessions[request.match_info["key"]] = await request.json()
        return web.json_response({"playlist": "x"})

    async def hls(self, request):
        self.authed(request)
        if request.match_info["key"] not in self.sessions:
            return web.Response(status=404)
        return web.Response(text="#EXTM3U", content_type="application/vnd.apple.mpegurl")


class HomePlaylists(Pairing):
    async def test_apps_that_cache_get_the_list(self) -> None:
        self.tv.state["features"] = ["render_stream", "home_without_ha", "home_playlist"]
        tenant = self.store.tenant(self.tenant)
        media_id, token = tenant.add_media("Clip", 10)
        tenant.update_media(media_id, status="ready", kind="video", playable="play.mp4")
        waiting, _ = tenant.add_media("Not yet", 10)
        page_id = tenant.save_page(None, name="Loop", kind="playlist", url=None,
                                   items=[{"media_id": media_id}, {"media_id": waiting, "seconds": 5}])
        screen_id = await self.screens.pair(self.tenant, self.site, "127.0.0.1", self.tv.server.port, "123456")
        await self.wait_online(screen_id)
        await asyncio.sleep(0.1)
        await self.screens.configure(self.tenant, screen_id, {}, page_id)
        self.assertEqual(self.tv.config["home_playlist"], [
            {"url": f"http://localhost:8791/play/{self.tenant}/m/{token}/play.mp4", "kind": "video", "seconds": 10}])
        self.assertTrue(self.tv.config["home_dashboard"].endswith(tenant.page(page_id)["token"]))  # the page, for older apps
        web = tenant.save_page(None, name="Menu", kind="web", url="https://example.com/", items=[])
        await self.screens.configure(self.tenant, screen_id, {}, web)
        self.assertEqual(self.tv.config["home_playlist"], [])


class ConsoleRenderers(Pairing):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.tv.state["features"] = ["render_stream", "home_without_ha"]
        self.renderer = FakeRenderer()
        await self.renderer.server.start_server()

    async def asyncTearDown(self) -> None:
        await super().asyncTearDown()
        await self.renderer.server.close()

    async def test_a_renderer_draws_a_light_screen(self) -> None:
        url = f"http://127.0.0.1:{self.renderer.server.port}"
        with self.assertRaises(ValueError):
            await rendering.pair(self.session, url, "000000", None)
        token = await rendering.pair(self.session, url, "654321", None)
        tenant = self.store.tenant(self.tenant)
        renderer_id = tenant.add_renderer(name="Rack A", device_id="rack-a", host="127.0.0.1", port=self.renderer.server.port,
                                          scheme="http", token=self.store.sealer(self.tenant).seal(token, "renderer:rack-a"),
                                          fingerprint=None)
        screen_id = await self.screens.pair(self.tenant, self.site, "127.0.0.1", self.tv.server.port, "123456")
        await self.wait_online(screen_id)
        await asyncio.sleep(0.1)
        page_id = tenant.save_page(None, name="Menu", kind="web", url="https://example.com/menu", items=[])
        tenant.set_screen_renderer(screen_id, renderer_id)
        await self.screens.configure(self.tenant, screen_id, {}, page_id)
        key = f"console-{self.tenant}-{screen_id}"
        self.assertEqual(self.renderer.sessions[key]["url"], "https://example.com/menu")
        stream = self.tv.config["render_stream"]
        secret = self.screens.rendering.secret(self.tenant, screen_id)
        self.assertEqual(stream, f"http://localhost:8791/play/{self.tenant}/r/{secret}/index.m3u8")
        content = TestClient(TestServer(create_content_app(self.store, self.screens.rendering)))
        await content.start_server()
        try:
            playlist = await content.get(f"/play/{self.tenant}/r/{secret}/index.m3u8")
            self.assertEqual((playlist.status, await playlist.text()), (200, "#EXTM3U"))
            self.assertEqual((await content.get(f"/play/{self.tenant}/r/{'0' * 32}/index.m3u8")).status, 404)
            self.assertEqual((await content.get(f"/play/{self.tenant}/r/{secret}/../x")).status, 404)
        finally:
            await content.close()
        tenant.set_screen_renderer(screen_id, None)
        await self.screens.refresh(self.tenant, [screen_id])
        self.assertEqual(self.tv.config["render_stream"], "")


if __name__ == "__main__":
    unittest.main()
