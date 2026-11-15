"""A renderer connecting out to a controller elsewhere (docs/renderer-connect-out.md): one TLS
WebSocket, signed in with the renderer's own key, carrying a fixed list of calls. The site sets
the controller's address in the renderer's options; nothing from the controller changes them.
"""

from __future__ import annotations

import asyncio
import base64
import collections
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

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

_LOGGER = logging.getLogger(__name__)

SUBPROTOCOL = "pleatview.renderer.v1"
PATH = "/api/pleatview/renderer"
LABEL = "pleatview-renderer-v1"
NONCE = re.compile(r"^[0-9a-f]{64}$")
CODE = re.compile(r"^[2-9A-HJ-NP-Z]{4}-?[2-9A-HJ-NP-Z]{4}$")
REQUEST_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
PING_EVERY = 20
SILENCE_LIMIT = 60
FIRST_WAIT, LONGEST_WAIT = 2, 60
LONGEST_RETRY = 3600
BAD_MESSAGE, UNKNOWN_KEY, REVOKED, CODE_EXPIRED, REPLACED = 4400, 4401, 4403, 4408, 4409
# What the controller may send: small requests, a few at once.
LARGEST_MESSAGE = 64 * 1024
MOST_AT_ONCE = 16
MOST_A_SECOND = 30
ANSWER_SECONDS = 30

Call = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class Refused(Exception):
    """A call the renderer won't make; the message goes back to the controller."""


def address(given: str) -> str:
    """The controller's address as signatures name it: wss://host[:port]/api/pleatview/renderer,
    host in lower case, no :443. A bare host name is taken as wss on 443."""
    text = given.strip()
    if "://" not in text:
        text = f"wss://{text}"
    parts = urlsplit(text)
    if parts.scheme.lower() != "wss" or not parts.hostname:
        raise ValueError("the controller's address is a host name, or wss://host[:port]/api/pleatview/renderer")
    if parts.path not in ("", "/", PATH) or parts.query or parts.fragment or parts.username or parts.password:
        raise ValueError(f"the controller's address has no path but {PATH}")
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    return f"wss://{host}{'' if parts.port in (None, 443) else f':{parts.port}'}{PATH}"


def fingerprint(given: str | None) -> bytes | None:
    """A certificate's SHA-256, from hex with or without colons."""
    if not given or not given.strip():
        return None
    text = given.strip().replace(":", "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", text):
        raise ValueError("controller_fingerprint is the SHA-256 of the controller's certificate, in hex")
    return bytes.fromhex(text)


def printable(text: Any, most: int = 120) -> str:
    """Text from the controller as the log may show it: one line, no control characters."""
    return re.sub(r"[^\x20-\x7e]", "?", str(text))[:most]


def signed_text(controller: str, nonce: str) -> str:
    return f"{LABEL}\n{controller}\n{nonce}"


class Closed(Exception):
    def __init__(self, code: int | None, reason: str = "") -> None:
        super().__init__(f"closed with {code}: {reason}")
        self.code, self.reason = code, reason


class Link:
    """The renderer's one connection out, opened again as needed, until the controller removes
    it (then it stays away until its options change or it restarts)."""

    def __init__(self, folder: Path, controller: str, pin: str | None, *, calls: dict[str, Call],
                 info: Callable[[], dict[str, Any]], write: Callable[[Path, str], None],
                 describe: Callable[[str, dict[str, Any]], str | None] = lambda method, params: None,
                 on_removed: Callable[[], Awaitable[None]] | None = None) -> None:
        self.folder = folder
        self.url = address(controller)
        self.pin = fingerprint(pin)
        self.calls = calls
        self.info = info
        self.write = write
        self.describe = describe
        self.on_removed = on_removed
        self.key = self._load_key()
        der = self.key.public_key().public_bytes(serialization.Encoding.DER,
                                                 serialization.PublicFormat.SubjectPublicKeyInfo)
        self.public = base64.b64encode(der).decode()
        self.key_id = hashlib.sha256(der).hexdigest()
        self.state = self._load_state()
        self.online = False
        self.removed = False
        self.code: str | None = None
        self.retry_after: int | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._task: asyncio.Task | None = None

    # Keys and what the renderer keeps

    def _load_key(self) -> ec.EllipticCurvePrivateKey:
        path = self.folder / "renderer-key.pem"
        try:
            key = serialization.load_pem_private_key(path.read_bytes(), password=None)
            if isinstance(key, ec.EllipticCurvePrivateKey) and isinstance(key.curve, ec.SECP256R1):
                return key
        except (OSError, ValueError):
            pass
        key = ec.generate_private_key(ec.SECP256R1())
        self.write(path, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()).decode())
        return key

    def _load_state(self) -> dict[str, Any]:
        try:
            state = json.loads((self.folder / "controller.json").read_text())
        except (OSError, ValueError):
            state = {}
        if not isinstance(state, dict) or state.get("url") != self.url:
            # Another controller (or none before): enrol from the start; the old one is forgotten.
            state = {"url": self.url, "enrolled": False}
            self.write(self.folder / "controller.json", json.dumps(state))
        return state

    def _save(self, **changes: Any) -> None:
        self.state = {**self.state, **changes}
        self.write(self.folder / "controller.json", json.dumps(self.state))

    def sign(self, nonce: str) -> str:
        signature = self.key.sign(signed_text(self.url, nonce).encode(), ec.ECDSA(hashes.SHA256()))
        return base64.b64encode(signature).decode()

    def _tls(self) -> ssl.SSLContext | aiohttp.Fingerprint:
        if self.pin:
            return aiohttp.Fingerprint(self.pin)  # the controller's own certificate, pinned
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        return context

    # The connection

    def start(self) -> None:
        self._task = asyncio.create_task(self.run(), name="controller link")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - stopping, whatever it was doing
                pass

    async def run(self) -> None:
        wait = FIRST_WAIT
        async with aiohttp.ClientSession() as session:
            while not self.removed:
                try:
                    await self._connect(session)
                    wait = FIRST_WAIT
                except Closed as closed:
                    if closed.code in (UNKNOWN_KEY, REVOKED):
                        _LOGGER.warning("The controller at %s removed this renderer (%s). It stays away until its "
                                        "options change or it restarts.", self.url, closed.code)
                        self._save(enrolled=False)
                        self.removed = True
                        if self.on_removed:
                            await self.on_removed()  # what it showed stops with it
                        break
                    if closed.code == REPLACED:
                        _LOGGER.warning("This renderer's key connected to the controller again from elsewhere; staying away")
                        self.removed = True
                        break
                    if closed.code == CODE_EXPIRED:
                        wait = FIRST_WAIT
                    else:
                        _LOGGER.info("The controller closed the connection (%s %s)", closed.code, closed.reason)
                except aiohttp.WSServerHandshakeError as err:
                    if err.status == 503:
                        retry = (err.headers or {}).get("Retry-After", "")
                        self.retry_after = int(retry) if retry.isdigit() else None
                    _LOGGER.info("The controller at %s didn't take the connection: %s", self.url, err.status)
                except (aiohttp.ClientError, OSError, ssl.SSLError, asyncio.TimeoutError, ValueError) as err:
                    _LOGGER.info("Can't reach the controller at %s: %s", self.url, err or type(err).__name__)
                except Exception:  # noqa: BLE001 - the connection must keep coming back, whatever happened
                    _LOGGER.exception("The connection to the controller failed")
                finally:
                    self.online = False
                    self._ws = None
                if self.removed:
                    break
                if self.retry_after:
                    pause = random.uniform(1, min(self.retry_after, LONGEST_RETRY))
                    self.retry_after = None
                else:
                    pause = wait * random.uniform(0.5, 1)
                await asyncio.sleep(pause)
                wait = min(wait * 2, LONGEST_WAIT)

    async def _connect(self, session: aiohttp.ClientSession) -> None:
        ws = await asyncio.wait_for(session.ws_connect(self.url, protocols=(SUBPROTOCOL,), ssl=self._tls(), heartbeat=None,
                                                       autoping=True, max_msg_size=LARGEST_MESSAGE), 20)
        self._ws = ws
        try:
            if ws.protocol != SUBPROTOCOL:
                raise ValueError(f"the controller doesn't speak {SUBPROTOCOL}")
            challenge = await self._receive(ws, 15)
            nonce = challenge.get("nonce")
            if challenge.get("type") != "challenge" or not isinstance(nonce, str) or not NONCE.match(nonce):
                raise ValueError("expected a challenge")
            hello = {"signature": self.sign(nonce), "info": self.info()}
            if self.state.get("enrolled"):
                await ws.send_json({"type": "auth", "key": self.key_id, **hello})
            else:
                await ws.send_json({"type": "enrol", "key": self.public, **hello})
            while True:  # a code first, when enrolling; then welcome
                message = await self._receive(ws, SILENCE_LIMIT)
                kind = message.get("type")
                if kind == "code":
                    code = str(message.get("code", ""))
                    if CODE.match(code) and code != self.code:
                        self.code = code
                        _LOGGER.warning("To add this renderer, type the code %s into the controller at %s",
                                        code, urlsplit(self.url).hostname)
                elif kind == "ping":
                    await ws.send_json({"type": "pong"})
                elif kind == "welcome":
                    break
            if not self.state.get("enrolled"):
                self._save(enrolled=True)
                _LOGGER.info("Added to the controller at %s", self.url)
            self.code = None
            self.online = True
            _LOGGER.info("Connected to the controller at %s", self.url)
            await self._serve(ws)
        finally:
            await ws.close()

    async def _receive(self, ws: aiohttp.ClientWebSocketResponse, timeout: float) -> dict[str, Any]:
        message = await ws.receive(timeout=timeout)
        if message.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
            code = message.data if message.type == aiohttp.WSMsgType.CLOSE and isinstance(message.data, int) else ws.close_code
            raise Closed(code, printable(message.extra or ""))
        if message.type == aiohttp.WSMsgType.ERROR:
            raise Closed(ws.close_code or BAD_MESSAGE, "error")
        if message.type != aiohttp.WSMsgType.TEXT:
            await ws.close(code=BAD_MESSAGE, message=b"text frames only")
            raise Closed(BAD_MESSAGE, "text frames only")
        try:
            data = json.loads(message.data)
        except ValueError:
            await ws.close(code=BAD_MESSAGE, message=b"not JSON")
            raise Closed(BAD_MESSAGE, "not JSON") from None
        if not isinstance(data, dict):
            await ws.close(code=BAD_MESSAGE, message=b"not a JSON object")
            raise Closed(BAD_MESSAGE, "not a JSON object")
        return data

    async def _serve(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        heard = [time.monotonic()]
        recent: collections.deque[float] = collections.deque()
        running: set[asyncio.Task] = set()

        async def keep_alive() -> None:
            while True:
                await asyncio.sleep(PING_EVERY)
                if time.monotonic() - heard[0] > SILENCE_LIMIT:
                    await ws.close(code=1001, message=b"silent")
                    return
                await ws.send_json({"type": "ping"})

        pinger = asyncio.create_task(keep_alive())
        try:
            while True:
                message = await self._receive(ws, SILENCE_LIMIT + PING_EVERY)
                heard[0] = now = time.monotonic()
                kind = message.get("type")
                while recent and now - recent[0] > 1:
                    recent.popleft()
                recent.append(now)  # every message counts, pings too
                if kind == "request" or len(recent) > MOST_A_SECOND:
                    if len(recent) > MOST_A_SECOND or len(running) >= MOST_AT_ONCE:
                        _LOGGER.warning("The controller sent requests faster than this renderer takes them; closing")
                        await ws.close(code=BAD_MESSAGE, message=b"too many requests")
                        raise Closed(BAD_MESSAGE, "too many requests")
                if kind == "request":
                    task = asyncio.create_task(self._answer(ws, message))
                    running.add(task)
                    task.add_done_callback(running.discard)
                elif kind == "ping":
                    await ws.send_json({"type": "pong"})
                elif kind == "retry":
                    after = message.get("after")
                    if isinstance(after, int) and not isinstance(after, bool) and 1 <= after <= LONGEST_RETRY:
                        self.retry_after = after
        finally:
            pinger.cancel()
            for task in running:
                task.cancel()

    async def _answer(self, ws: aiohttp.ClientWebSocketResponse, request: dict[str, Any]) -> None:
        ident = request.get("id")
        if not ((isinstance(ident, int) and not isinstance(ident, bool) and 0 <= ident < 2**53)
                or (isinstance(ident, str) and REQUEST_ID.match(ident))):
            return  # nothing to answer to
        method = request.get("method")
        params = request.get("params", {})
        call = self.calls.get(method) if isinstance(method, str) else None
        if call is None:
            reply: dict[str, Any] = {"error": "unknown method"}
            _LOGGER.warning("The controller asked for %r, which this renderer doesn't do", str(method)[:40])
        elif not isinstance(params, dict):
            reply = {"error": "params is a JSON object"}
        else:
            told = self.describe(method, params)
            try:
                reply = {"result": await asyncio.wait_for(call(params), ANSWER_SECONDS)}
                if told:
                    _LOGGER.info("The controller: %s", told)
            except Refused as refused:
                reply = {"error": str(refused)}
                if told:
                    _LOGGER.info("The controller: %s (refused: %s)", told, refused)
            except asyncio.TimeoutError:
                reply = {"error": "it took too long"}
            except Exception:  # noqa: BLE001 - one call failing mustn't end the connection
                _LOGGER.exception("The controller's %s failed", method)
                reply = {"error": "it failed"}
        try:
            await ws.send_json({"type": "response", "id": ident, **reply})
        except (ConnectionError, RuntimeError):
            pass
