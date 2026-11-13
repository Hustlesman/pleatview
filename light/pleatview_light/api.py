"""Protocol v1's HTTP API (docs/protocol.md), on asyncio streams from the standard library.

One request per connection, bodies up to 256 KiB, JSON in and out; the event stream is
Server-Sent Events with a ping every 20 seconds.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
from typing import Any, Awaitable, Callable

from .pairing import Pairing

# Requests name the screen by its address, not another site's name: a web page elsewhere that
# points its own name at the screen (DNS rebinding) is refused (docs/protocol.md).
LOCAL_SUFFIXES = (".local", ".lan", ".home", ".home.arpa", ".internal")


def host_allowed(value: str) -> bool:
    host = value.strip().lower()
    if not host:
        return True  # HTTP/1.0 clients send none; browsers always do
    if host.startswith("["):
        host = host[1:host.find("]")] if "]" in host else host[1:]
    elif host.count(":") == 1:
        host = host.rsplit(":", 1)[0]
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    host = host.rstrip(".")
    return host == "localhost" or "." not in host or host.endswith(LOCAL_SUFFIXES)
from .screen import Screen

_LOGGER = logging.getLogger(__name__)

MAX_BODY = 256 * 1024
REASONS = {200: "OK", 202: "Accepted", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
           405: "Method Not Allowed", 409: "Conflict", 413: "Payload Too Large", 421: "Misdirected Request",
           429: "Too Many Requests"}


class Api:
    def __init__(self, screen: Screen, pairing: Pairing, info: Callable[[], dict[str, Any]]) -> None:
        self.diagnostics: Callable[[], Awaitable[dict[str, Any]]] | None = None
        self.screen = screen
        self.pairing = pairing
        self.info = info
        self.connection: Any = None  # protocol v2's connection out (connect.py), once started

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await asyncio.wait_for(self._handle(reader, writer), timeout=None)
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError):
            pass
        finally:
            writer.close()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
        lines = head.decode("latin-1").split("\r\n")
        method, path, _ = (lines[0].split(" ") + ["", ""])[:3]
        headers = {k.strip().lower(): v.strip() for k, _, v in (line.partition(":") for line in lines[1:] if line)}
        if not host_allowed(headers.get("host", "")):
            return await self._send(writer, 421, {"error": "misdirected"})
        length = int(headers.get("content-length") or 0)
        if length > MAX_BODY:
            return await self._send(writer, 413, {"error": "too_large"})
        raw = await asyncio.wait_for(reader.readexactly(length), 10) if length else b""
        token = re.sub(r"^Bearer\s+", "", headers.get("authorization", ""), flags=re.I) or None
        path = path.split("?", 1)[0]
        if method == "GET" and path == "/api/events":
            if not self.pairing.valid(token):
                return await self._send(writer, 401, {"error": "unauthorized"})
            return await self._events(writer)
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            return await self._send(writer, 400, {"error": "invalid_json"})
        peer = (writer.get_extra_info("peername") or ("",))[0]
        status, reply = await self.route(method, path, body if isinstance(body, dict) else None, token,
                                         local=peer in ("127.0.0.1", "::1"))
        await self._send(writer, status, reply)

    async def route(self, method: str, path: str, body: dict[str, Any] | None,
                    token: str | None, *, trusted: bool = False, local: bool = False) -> tuple[int, dict[str, Any]]:
        """One v1 call. `trusted`: it came over the controller's signed connection (protocol v2)."""
        if body is None:
            return 400, {"error": "body must be a JSON object"}
        if (method, path) == ("GET", "/api/info"):
            if not trusted and not self.pairing.valid(token):
                await self.screen.pin_requested()
            return 200, {**self.info(), "paired": self.pairing.paired}
        if (method, path) == ("POST", "/api/connect") and local and self.connection:
            # Someone on this computer (pleatview-light connect URL): connect out and show a code.
            try:
                self.connection.enrol(str(body.get("url", "")))
            except ValueError as err:
                return 400, {"error": str(err)}
            return 202, {}
        if (method, path) == ("POST", "/api/pair"):
            result = self.pairing.pair(body.get("pin"), body.get("client"))
            if result["result"] == "paired":
                return 200, {"token": result["token"]}
            if result["result"] == "locked_out":
                return 429, {"error": "too_many_attempts", "retry_after": result["retry_after"]}
            return 403, {"error": "invalid_pin"}
        if (method, path) == ("POST", "/api/verify"):
            nonce = body.get("nonce")
            if not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-fA-F]{32,128}", nonce):
                return 400, {"error": "nonce must be 32 to 128 hex digits"}
            return 200, {"proofs": self.pairing.prove(nonce)}
        if not trusted and not self.pairing.valid(token):
            return 401, {"error": "unauthorized"}
        if (method, path) == ("POST", "/api/enrol") and not trusted and self.connection:
            # A controller paired over v1 moves this screen to v2 (docs/protocol-v2.md).
            url, token_v2, pin = body.get("url"), body.get("token"), body.get("fingerprint")
            if not isinstance(url, str) or not isinstance(token_v2, str) or not token_v2 or (
                    pin is not None and not isinstance(pin, str)):
                return 400, {"error": "url and token are required"}
            try:
                self.connection.enrol(url, token_v2, pin)
            except ValueError as err:
                return 400, {"error": str(err)}
            return 202, {}
        if (method, path) == ("POST", "/api/unpair"):
            self.pairing.revoke(token or "")
            return 200, {}
        if (method, path) == ("GET", "/api/state"):
            return 200, self.screen.state
        if (method, path) == ("GET", "/api/diagnostics") and self.diagnostics:
            return 200, await self.diagnostics()
        if path == "/api/config":
            if method == "GET":
                return 200, self.screen.config.public()
            if method == "POST":
                try:
                    config = await self.screen.configure(body)
                except ValueError as err:
                    return 400, {"error": str(err)}
                return 200, config.public()
        if (method, path) == ("POST", "/api/command"):
            try:
                await self.screen.command(body)
            except ValueError as err:
                return 400, {"error": str(err)}
            return 200, {}
        if path == "/api/signin" or path == "/api/signins":
            return 409, {"error": "no_sign_in_pending"}  # Home Assistant renders the pages
        return 404, {"error": "not_found"}

    async def _events(self, writer: asyncio.StreamWriter) -> None:
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-store\r\n"
                     b"Connection: close\r\n\r\n")
        queue = self.screen.subscribe()
        try:
            writer.write(f"event: state\ndata: {json.dumps(self.screen.state)}\n\n".encode())
            await writer.drain()
            while True:
                try:
                    state = await asyncio.wait_for(queue.get(), 20)
                    writer.write(f"event: state\ndata: {json.dumps(state)}\n\n".encode())
                except asyncio.TimeoutError:
                    writer.write(b": ping\n\n")
                await writer.drain()
        finally:
            self.screen.listeners.discard(queue)

    @staticmethod
    async def _send(writer: asyncio.StreamWriter, status: int, data: dict[str, Any]) -> None:
        payload = json.dumps(data).encode()
        writer.write(f"HTTP/1.1 {status} {REASONS.get(status, 'Error')}\r\nContent-Type: application/json\r\n"
                     f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode() + payload)
        await writer.drain()
