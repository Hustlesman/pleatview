"""Protocol v2 (docs/protocol-v2.md): PleatView Light connects out to a controller over TLS,
signs in with its own key and answers v1's calls over that one connection. The standard
library and python3-cryptography only."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import random
import re
import ssl
import time
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from . import websocket
from .websocket import Busy, Closed

_LOGGER = logging.getLogger(__name__)

SUBPROTOCOL = "pleatview.v2"
PATH = "/api/pleatview/v2"
NONCE = re.compile(r"^[0-9a-f]{64}$")
PIN = re.compile(r"^[0-9a-f]{64}$")
PING_EVERY = 20
SILENCE_LIMIT = 60
FIRST_WAIT, LONGEST_WAIT = 2, 60
UNKNOWN_KEY, REVOKED, CODE_EXPIRED, REPLACED = 4401, 4403, 4408, 4409

# v2's methods and the v1 calls they stand for.
METHODS = {
    "info": ("GET", "/api/info"),
    "state": ("GET", "/api/state"),
    "command": ("POST", "/api/command"),
    "config": ("GET", "/api/config"),
    "set_config": ("POST", "/api/config"),
    "signins": ("GET", "/api/signins"),
    "forget_signins": ("DELETE", "/api/signins"),
    "signin": ("POST", "/api/signin"),
    "diagnostics": ("GET", "/api/diagnostics"),
}
MOVE_TRIES = 3
LONGEST_RETRY = 3600


def address(given: str) -> str:
    """A controller's address as signatures name it: wss, host in lower case, no :443."""
    parts = urlsplit(given.strip())
    if parts.scheme.lower() != "wss" or not parts.hostname:
        raise ValueError("a controller's address is wss://host[:port]/api/pleatview/v2")
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    return f"wss://{host}{'' if parts.port in (None, 443) else f':{parts.port}'}{PATH}"


def signed_text(controller: str, nonce: str) -> str:
    return f"pleatview-v2\n{controller}\n{nonce}"


class Connection:
    """The screen's one connection out, kept open and opened again as needed."""

    def __init__(self, folder: Path, *, info: Callable[[], dict[str, Any]],
                 route: Callable[..., Awaitable[tuple[int, dict[str, Any]]]],
                 state: Callable[[], dict[str, Any]], subscribe: Callable[[], asyncio.Queue],
                 unsubscribe: Callable[[asyncio.Queue], None], show_code: Callable[[str | None, str], Awaitable[None]],
                 write: Callable[[Path, str], None]) -> None:
        self.folder = folder
        self.info = info
        self.route = route
        self.state = state
        self.subscribe = subscribe
        self.unsubscribe = unsubscribe
        self.show_code = show_code
        self.write = write
        self.key = self._load_key()
        der = self.key.public_key().public_bytes(serialization.Encoding.DER,
                                                 serialization.PublicFormat.SubjectPublicKeyInfo)
        self.public = base64.b64encode(der).decode()
        self.key_id = hashlib.sha256(der).hexdigest()
        try:
            self.controller: dict[str, Any] = json.loads((folder / "controller.json").read_text())
        except (OSError, ValueError):
            self.controller = {}
        self.token: str | None = None  # one-time, from a controller paired over v1
        self.online = False
        self.retry_after: int | None = None  # the controller's own wait, from `retry` or a 503
        self.moving: dict[str, Any] | None = None  # a new address, until it lets this screen in
        self.move_tries = 0
        self._wake = asyncio.Event()
        self._ws: websocket.WebSocket | None = None
        self._task: asyncio.Task | None = None

    # Keys

    def _load_key(self) -> ec.EllipticCurvePrivateKey:
        path = self.folder / "v2-key.pem"
        try:
            key = serialization.load_pem_private_key(path.read_bytes(), password=None)
            if isinstance(key, ec.EllipticCurvePrivateKey):
                return key
        except (OSError, ValueError):
            pass
        key = ec.generate_private_key(ec.SECP256R1())
        self.write(path, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()).decode())
        return key

    def sign(self, controller: str, nonce: str) -> str:
        signature = self.key.sign(signed_text(controller, nonce).encode(), ec.ECDSA(hashes.SHA256()))
        return base64.b64encode(signature).decode()

    # What controllers ask for

    def enrol(self, url: str, token: str | None = None, pin: str | None = None) -> None:
        """Connect to this controller: with a token from one paired over v1, or to show a code."""
        if pin is not None and not PIN.match(pin.lower()):
            raise ValueError("fingerprint is the hex of the certificate's SHA-256")
        self._save({"url": address(url), "pin": pin.lower() if pin else None, "enrolled": False})
        self.token = token
        self._restart()

    def forget(self) -> None:
        self._save({})
        self.token = None
        asyncio.ensure_future(self.show_code(None, ""))

    def _save(self, controller: dict[str, Any]) -> None:
        self.controller = controller
        self.write(self.folder / "controller.json", json.dumps(controller))

    def _restart(self) -> None:
        if self._ws:
            asyncio.ensure_future(self._ws.close(1000))
        self._wake.set()

    # The connection

    def start(self) -> None:
        self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        if self._ws:
            await self._ws.close(1001)

    def _move_failed(self) -> None:
        self.move_tries += 1
        self._moved(False)

    def _moved(self, ok: bool) -> None:
        """A move ends: kept once the new address let this screen in, given up after three tries."""
        if ok:
            _LOGGER.info("The controller moved to %s", self.moving["url"])
            self._save({**self.controller, "url": self.moving["url"], "pin": self.moving.get("pin")})
        elif self.move_tries >= MOVE_TRIES:
            _LOGGER.warning("The controller's new address %s didn't let this screen in; staying at %s",
                            self.moving["url"], self.controller.get("url"))
        else:
            return
        self.moving = None

    async def run(self) -> None:
        wait = FIRST_WAIT
        while True:
            self._wake.clear()
            if not self.controller.get("url"):
                await self._wake.wait()
                continue
            moving = self.moving is not None
            try:
                await self._connect()
                wait = FIRST_WAIT  # it ran and ended: come back soon
            except Closed as closed:
                if moving and self.moving is not None:
                    self._move_failed()  # whatever the new address said, the old one stands
                    wait = FIRST_WAIT
                elif closed.code in (UNKNOWN_KEY, REVOKED):
                    _LOGGER.info("The controller removed this screen (%s)", closed.code)
                    self.forget()
                    continue
                elif closed.code == CODE_EXPIRED:
                    wait = FIRST_WAIT  # ask for a new code
                elif closed.code == REPLACED:
                    _LOGGER.info("This screen connected again elsewhere; staying away")
                    await self._wake.wait()
                    continue
            except Busy as busy:
                self.retry_after = busy.retry_after
            except (OSError, ssl.SSLError, asyncio.TimeoutError, asyncio.IncompleteReadError, ValueError) as err:
                _LOGGER.info("Can't reach the controller at %s: %s", (self.moving or self.controller).get("url"), err)
                if moving and self.moving is not None:
                    self._move_failed()
            except Exception:  # noqa: BLE001 - the connection must keep coming back, whatever happened
                _LOGGER.exception("The connection to the controller failed")
            finally:
                self.online = False
            if self.moving is not None and not moving:
                pause = 0.0  # just told to move: go now
            elif self.retry_after:
                pause = random.uniform(1, min(self.retry_after, LONGEST_RETRY))
                self.retry_after = None
            else:
                pause = wait * random.uniform(0.5, 1)
            try:
                await asyncio.wait_for(self._wake.wait(), pause)
            except asyncio.TimeoutError:
                pass
            wait = min(wait * 2, LONGEST_WAIT)

    async def _connect(self) -> None:
        target = self.moving or self.controller
        url = target["url"]
        ws = await websocket.connect(url, subprotocol=SUBPROTOCOL, pin=target.get("pin"))
        self._ws = ws
        try:
            challenge = await asyncio.wait_for(ws.receive_json(), 15)
            nonce = challenge.get("nonce")
            if challenge.get("type") != "challenge" or not isinstance(nonce, str) or not NONCE.match(nonce):
                raise ValueError("expected a challenge")
            hello = {"signature": self.sign(url, nonce), "info": self.info()}
            if self.controller.get("enrolled"):
                await ws.send_json({"type": "auth", "key": self.key_id, **hello})
            else:
                await ws.send_json({"type": "enrol", "key": self.public, **hello,
                                    **({"token": self.token} if self.token else {})})
            while True:  # a code first, perhaps, then welcome
                message = await ws.receive_json()
                kind = message.get("type")
                if kind == "code":
                    await self.show_code(str(message.get("code", "")), urlsplit(url).hostname or "")
                elif kind == "ping":
                    await ws.send_json({"type": "pong"})
                elif kind == "welcome":
                    break
            if self.moving:
                self._moved(True)
            if not self.controller.get("enrolled"):
                self._save({**self.controller, "enrolled": True})
                self.token = None
                await self.show_code(None, "")
                _LOGGER.info("Enrolled with the controller at %s", url)
            self.online = True
            await self._serve(ws)
        finally:
            self._ws = None
            await ws.close()

    async def _serve(self, ws: websocket.WebSocket) -> None:
        heard = [time.monotonic()]
        changes = self.subscribe()

        async def send_state() -> None:
            await ws.send_json({"type": "state", "state": self.state()})
            while True:
                await ws.send_json({"type": "state", "state": await changes.get()})

        async def keep_alive() -> None:
            while True:
                await asyncio.sleep(PING_EVERY)
                if time.monotonic() - heard[0] > SILENCE_LIMIT:
                    await ws.close(1001)
                    return
                await ws.send_json({"type": "ping"})

        helpers = [asyncio.create_task(send_state()), asyncio.create_task(keep_alive())]
        try:
            while True:
                message = await ws.receive_json()
                heard[0] = time.monotonic()
                kind = message.get("type")
                if kind == "request":
                    asyncio.create_task(self._answer(ws, message))
                elif kind == "ping":
                    await ws.send_json({"type": "pong"})
                elif kind == "retry":
                    after = message.get("after")
                    if isinstance(after, int) and not isinstance(after, bool) and 1 <= after <= LONGEST_RETRY:
                        self.retry_after = after
                elif kind == "move":
                    self._move(message)
        finally:
            for helper in helpers:
                helper.cancel()
            self.unsubscribe(changes)

    def _move(self, message: dict[str, Any]) -> None:
        """The controller has a new address: try it, keeping the old one until it lets us in."""
        pin = message.get("fingerprint")
        try:
            url = address(str(message.get("url", "")))
        except ValueError:
            _LOGGER.warning("Ignoring a move to an address that isn't wss://")
            return
        if pin is not None and (not isinstance(pin, str) or not PIN.match(pin.lower())):
            return
        if url == self.controller.get("url"):
            return
        self.moving = {"url": url, "pin": pin.lower() if pin else None}
        self.move_tries = 0
        _LOGGER.info("The controller asks this screen to use %s", url)
        if self._ws:
            asyncio.ensure_future(self._ws.close(1000))

    async def _answer(self, ws: websocket.WebSocket, request: dict[str, Any]) -> None:
        call = METHODS.get(str(request.get("method")))
        params = request.get("params") if isinstance(request.get("params"), dict) else {}
        if not call:
            reply: dict[str, Any] = {"error": f"unknown method {request.get('method')}"}
        else:
            status, body = await self.route(*call, params, None, trusted=True)
            reply = {"result": body} if status < 300 else {"error": str(body.get("error", status))}
        try:
            await ws.send_json({"type": "response", "id": request.get("id"), **reply})
        except (ConnectionError, RuntimeError):
            pass
