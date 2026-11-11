"""An in-process stand-in for the PleatView TV app's HTTP API."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from typing import Any

from aiohttp import web

PIN = "246810"


class FakeTV:
    """Implements docs/protocol.md closely enough to exercise the integration."""

    def __init__(self) -> None:
        self.port = 0
        self.pin = PIN
        self.tokens: set[str] = set()
        self.info: dict[str, Any] = {
            "id": "tv-1",
            "name": "Living Room TV",
            "manufacturer": "Google",
            "model": "Chromecast",
            "app_version": "0.1.0",
            "api_version": 1,
        }
        self.state: dict[str, Any] = {
            "foreground": True,
            "power": "on",
            "view": "dashboard",
            "url": "http://ha.test:8123/lovelace/0",
            "dashboard": "/lovelace/0",
            "ha_connected": True,
            "auth_error": False,
            "last_error": None,
            "overlay": None,
            "media": None,
            "volume": 1.0,
            "muted": False,
            "config_rev": "",
            "overlay_permission": False,
            "app_version": "0.1.0",
        }
        self.config: dict[str, Any] = {}
        self.commands: list[dict[str, Any]] = []
        self.authorizations: list[str] = []  # every Authorization header received
        self.verify_supported = True
        self.streams: list[web.StreamResponse] = []
        self._closing = asyncio.Event()

    def issue_token(self) -> str:
        token = f"token-{len(self.tokens) + 1}"
        self.tokens.add(token)
        return token

    def app(self) -> web.Application:
        app = web.Application(middlewares=[self._auth])
        app.router.add_get("/api/info", self._info)
        app.router.add_post("/api/pair", self._pair)
        app.router.add_post("/api/pair/request", self._pair_request)
        app.router.add_get("/api/pair/request/{request}", self._pair_state)
        app.router.add_post("/api/verify", self._verify)
        app.router.add_get("/api/state", self._state)
        app.router.add_get("/api/events", self._events)
        app.router.add_post("/api/command", self._command)
        app.router.add_get("/api/config", self._get_config)
        app.router.add_post("/api/config", self._set_config)
        app.router.add_post("/api/unpair", self._unpair)
        return app

    # Pairing approved on the TV: `approve` decides what the person at the TV does.
    approve: str | None = None  # "allow", "deny", or None (nobody presses anything)

    async def _pair_request(self, request: web.Request) -> web.Response:
        self.approval_requests = getattr(self, "approval_requests", 0) + 1
        return web.json_response({"request": "req-1", "expires_in": 3}, status=202)

    async def _pair_state(self, request: web.Request) -> web.Response:
        if request.match_info["request"] != "req-1":
            return web.json_response({"error": "no_such_request"}, status=404)
        if self.approve == "allow":
            return web.json_response({"state": "approved", "token": self.issue_token()})
        if self.approve == "deny":
            return web.json_response({"state": "denied"})
        return web.json_response({"state": "pending"})

    async def push(self, **changes: Any) -> None:
        """Change state and send it to every open event stream."""
        self.state.update(changes)
        for stream in list(self.streams):
            await stream.write(_event(self.state))

    async def wait_for_stream(self, timeout: float = 5) -> None:
        async with asyncio.timeout(timeout):
            while not self.streams:
                await asyncio.sleep(0.01)

    def close_streams(self) -> None:
        self._closing.set()

    @web.middleware
    async def _auth(self, request: web.Request, handler):
        if "Authorization" in request.headers:
            self.authorizations.append(request.headers["Authorization"])
        if request.path not in ("/api/info", "/api/pair", "/api/verify") and not request.path.startswith("/api/pair/request"):
            header = request.headers.get("Authorization", "")
            if header.removeprefix("Bearer ") not in self.tokens:
                return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    async def _info(self, request: web.Request) -> web.Response:
        return web.json_response({**self.info, "paired": bool(self.tokens)})

    async def _pair(self, request: web.Request) -> web.Response:
        body = await request.json()
        if body.get("pin", "").replace(" ", "") != self.pin:
            return web.json_response({"error": "invalid_pin"}, status=403)
        return web.json_response({"token": self.issue_token()})

    async def _verify(self, request: web.Request) -> web.Response:
        if not self.verify_supported:
            return web.json_response({"error": "not found"}, status=404)
        nonce = (await request.json())["nonce"].encode()
        proofs = [
            hmac.new(hashlib.sha256(token.encode()).hexdigest().encode(), nonce, hashlib.sha256).hexdigest()
            for token in self.tokens
        ]
        return web.json_response({"proofs": proofs})

    async def _state(self, request: web.Request) -> web.Response:
        return web.json_response(self.state)

    async def _events(self, request: web.Request) -> web.StreamResponse:
        stream = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await stream.prepare(request)
        await stream.write(b"retry: 3000\n\n")
        await stream.write(_event(self.state))
        self.streams.append(stream)
        try:
            await self._closing.wait()
        finally:
            self.streams.remove(stream)
        return stream

    async def _command(self, request: web.Request) -> web.Response:
        body = await request.json()
        if body.get("command") == "explode":
            return web.json_response({"error": "unknown command 'explode'"}, status=400)
        self.commands.append(body)
        return web.json_response({"ok": True})

    async def _get_config(self, request: web.Request) -> web.Response:
        return web.json_response(self.config)

    async def _set_config(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.config.update(body)
        self.state["config_rev"] = body.get("rev", "")
        return web.json_response(self.config)

    async def _unpair(self, request: web.Request) -> web.Response:
        self.tokens.discard(request.headers["Authorization"].removeprefix("Bearer "))
        return web.json_response({"ok": True})


def _event(state: dict[str, Any]) -> bytes:
    return f"event: state\ndata: {json.dumps(state)}\n\n".encode()
