"""The site's PleatView screens, found for the controller this renderer connects out to and added
there as they would be on its own network (docs/renderer-connect-out.md#screens-at-the-site). On
unless the site turns its Find screens option off.

The app can't see the site's network itself (it's on Home Assistant's own), so it asks Home
Assistant: the screens its PleatView integration found on the network and hasn't added, each with
the address it was found at, and the ones it has added. Adding one takes someone at the screen,
as on the controller's own network: they press OK there, or read its PIN to whoever types it
into the controller. This renderer then pairs with the screen, hands it the controller's address
for screens with the one-time token the controller made for it, and lets go of its pairing. The
screen connects out to the controller by itself.

The controller names a screen by its id, never by an address: the addresses are the ones found
here. And a screen only ever goes to this renderer's own controller.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from yarl import URL

from .site_ha import SCREEN_ID, HomeAssistant, Unavailable, printable

_LOGGER = logging.getLogger(__name__)

TIMEOUT = aiohttp.ClientTimeout(total=10)
LARGEST_ANSWER = 64 * 1024
FRESH_FOR = 10  # seconds the screens found are answered again without asking Home Assistant
APPROVAL_WAIT = 130  # a screen asks the person in front of it for 120 seconds
ASK_EVERY = 1.5
TOKEN = re.compile(r"^[A-Za-z0-9_\-]{16,200}$")
PIN = re.compile(r"^[0-9]{6}$")
CONNECTS_OUT = "protocol_v2"
HOW = ("ok", "pin")


@dataclass(frozen=True)
class Found:
    """A screen at the site: where it was found, and whether Home Assistant has it."""

    id: str
    name: str
    host: str
    port: int
    in_home_assistant: bool = False
    connects_out: bool | None = None  # known for the ones Home Assistant has

    def describe(self) -> dict[str, Any]:
        """What the controller is told: no address."""
        return {"id": self.id, "name": self.name, "in_home_assistant": self.in_home_assistant,
                **({"connects_out": self.connects_out} if self.connects_out is not None else {})}


def _port(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 < value < 65536 else None


def found_in_flows(flows: Any) -> list[Found]:
    """Screens Home Assistant's PleatView integration found on the network and hasn't added (its
    discoveries, waiting in Settings → Devices & services), with the address each was found at:
    the flow's configuration_url, from integration 1.1.4 on."""
    out = []
    for flow in flows if isinstance(flows, list) else []:
        context = flow.get("context") if isinstance(flow, dict) else None
        if flow.get("handler") != "pleatview" or not isinstance(context, dict) or context.get("source") != "zeroconf" \
                or flow.get("step_id") == "renderer":
            continue
        screen_id, url = context.get("unique_id"), context.get("configuration_url")
        if not isinstance(screen_id, str) or not SCREEN_ID.fullmatch(screen_id) or not isinstance(url, str):
            continue
        try:
            parts = urlsplit(url)
            port = _port(parts.port or 80)
        except ValueError:
            continue
        if parts.scheme != "http" or not parts.hostname or port is None:
            continue
        names = context.get("title_placeholders") if isinstance(context.get("title_placeholders"), dict) else {}
        out.append(Found(screen_id, printable(names.get("name") or screen_id, 100), parts.hostname, port))
    return out


def found_in_home_assistant(listed: Any) -> list[Found]:
    """The screens Home Assistant has added, as its pleatview.list_screens gives them (with their
    addresses from integration 1.1.4 on)."""
    screens = listed.get("screens") if isinstance(listed, dict) else None
    out = []
    for screen in screens[:500] if isinstance(screens, list) else []:
        if not isinstance(screen, dict) or not isinstance(screen.get("id"), str) or not SCREEN_ID.fullmatch(screen["id"]):
            continue
        host, port = screen.get("host"), _port(screen.get("port"))
        known = isinstance(host, str) and re.fullmatch(r"[A-Za-z0-9.:\-%]{1,253}", host) and port
        out.append(Found(screen["id"], printable(screen.get("name") or screen["id"], 100), host if known else "",
                         port if known else 0, True, screen.get("connects_out") is True))
    return out


class Unreachable(Unavailable):
    """The screen didn't answer: asked again while it may still."""


class ScreenClient:
    """A screen's local API (docs/protocol.md), as far as adding it to the controller takes."""

    def __init__(self, http: aiohttp.ClientSession, found: Found) -> None:
        self.http, self.found, self.token = http, found, ""
        self.base = URL.build(scheme="http", host=found.host, port=found.port)

    async def _request(self, method: str, path: str, body: dict[str, Any] | None = None, *, auth: bool = True,
                       busy: str = "Too many wrong PINs. The screen shows a new one in a minute.") -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.token}"} if auth and self.token else {}
        try:
            async with self.http.request(method, self.base.with_path(path), json=body, headers=headers,
                                         timeout=TIMEOUT, allow_redirects=False) as response:
                raw = bytearray()
                async for chunk in response.content.iter_chunked(16 * 1024):
                    raw += chunk
                    if len(raw) > LARGEST_ANSWER:
                        raise Unavailable(f"{self.found.name} answered with too much")
                status = response.status
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            raise Unreachable(f"{self.found.name} can't be reached at the address it was found at") from None
        try:
            answer = json.loads(raw) if raw else {}
        except ValueError:
            answer = {}
        answer = answer if isinstance(answer, dict) else {}
        if status == 403 and answer.get("error") == "invalid_pin":
            raise Unavailable("Wrong PIN. Check the PIN on the screen; it changes after each pairing.")
        if status == 429:
            raise Unavailable(busy)
        if status == 401:
            raise Unavailable(f"{self.found.name} didn't take this renderer's pairing")
        if status == 404:
            raise Unavailable(f"{self.found.name}'s app is too old for this: update it")
        if status >= 400:
            said = printable(answer.get("error") or "", 100)
            raise Unavailable(f"{self.found.name} answered HTTP {status}" + (f": {said}" if said else ""))
        return answer

    async def info(self) -> dict[str, Any]:
        return await self._request("GET", "/api/info")

    async def state(self) -> dict[str, Any]:
        return await self._request("GET", "/api/state")

    async def pair(self, pin: str, client: str) -> str:
        token = (await self._request("POST", "/api/pair", {"pin": pin, "client": client}, auth=False)).get("token")
        if not isinstance(token, str) or not token:
            raise Unavailable(f"{self.found.name} didn't pair")
        return token

    async def request_approval(self, client: str) -> str:
        # One request waits at a time: Home Assistant asks a screen it has just found, for two minutes.
        busy = f"{self.found.name} is asking about another pairing already: try again in two minutes, or use its PIN"
        request = (await self._request("POST", "/api/pair/request", {"client": client}, auth=False, busy=busy)).get("request")
        if not isinstance(request, str) or not re.fullmatch(r"[A-Za-z0-9_\-]{1,100}", request):
            raise Unavailable(f"{self.found.name} didn't take the request")
        return request

    async def approval_state(self, request: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/pair/request/{request}", auth=False)

    async def enrol(self, url: str, token: str, fingerprint: str | None) -> None:
        await self._request("POST", "/api/enrol", {"url": url, "token": token, **({"fingerprint": fingerprint} if fingerprint else {})})

    async def unpair(self) -> None:
        await self._request("POST", "/api/unpair", {})


class SiteScreens:
    """The site's screens: found through Home Assistant, added to this renderer's controller."""

    def __init__(self, ha: HomeAssistant, screens_to: tuple[str, str | None], controller_host: str) -> None:
        self.ha, self.screens_to = ha, screens_to
        # Who asks, as the screen shows it when it asks the person in front of it to allow pairing.
        self.client = printable(controller_host, 40)
        self.known: dict[str, Found] = {}
        self.asking: dict[str, dict[str, Any]] = {}
        self._fresh = -FRESH_FOR
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._http: aiohttp.ClientSession | None = None

    @property
    def http(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(trust_env=False)
        return self._http

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._http is not None:
            await self._http.close()

    async def found(self) -> list[Found]:
        async with self._lock:
            if time.monotonic() - self._fresh >= FRESH_FOR:
                flows = await self.ha._ws({"type": "config_entries/flow/progress"})
                try:
                    listed = await self.ha._service("pleatview", "list_screens", {})
                except Unavailable:
                    listed = None  # no screen in Home Assistant: then its integration isn't set up, nor its services
                found = {f.id: f for f in found_in_flows(flows)}
                found.update({f.id: f for f in found_in_home_assistant(listed)})
                self.known, self._fresh = found, time.monotonic()
        return sorted(self.known.values(), key=lambda f: (f.name.lower(), f.id))

    async def add(self, screen: Any, how: Any, pin: Any, token: Any) -> dict[str, Any]:
        """Pairs with a screen found here and passes it on: at once with its PIN, or once
        someone presses OK on it (a ticket to ask about, with asked())."""
        if not isinstance(screen, str) or not SCREEN_ID.fullmatch(screen):
            raise Unavailable("screen is a screen's id, as site_screens gives it")
        if how not in HOW:
            raise Unavailable("how is ok (someone presses OK on the screen) or pin (the PIN it shows)")
        if how == "pin" and not (isinstance(pin, str) and PIN.fullmatch(pin)):
            raise Unavailable("The PIN is the 6 digits the screen shows")
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            raise Unavailable("token is the one-time token the controller made for the screen")
        found = self.known.get(screen)
        if found is None:
            self._fresh = -FRESH_FOR
            found = next((f for f in await self.found() if f.id == screen), None)
        if found is None:
            raise Unavailable("this renderer hasn't found that screen at its site")
        if not found.host:
            raise Unavailable("Home Assistant's PleatView integration is older than this renderer: update it")
        client = ScreenClient(self.http, found)
        info = await client.info()
        if info.get("id") != found.id:
            self._fresh = -FRESH_FOR
            raise Unavailable(f"another screen answers at {found.name}'s address now")
        if how == "pin":
            await self._pass_on(client, await client.pair(pin, self.client), token)
            return {"added": True}
        if "approve" not in (info.get("pairing") or []):
            raise Unavailable(f"{found.name} pairs with its PIN only: it's on the screen, or in its menu")
        request = await client.request_approval(self.client)
        now = time.monotonic()
        for old in [t for t, e in self.asking.items() if e["until"] + 600 < now]:
            del self.asking[old]
        ticket = secrets.token_urlsafe(16)
        self.asking[ticket] = {"state": "pending", "screen": found.id, "until": now + APPROVAL_WAIT}
        task = asyncio.ensure_future(self._wait(ticket, client, request, token))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        _LOGGER.info("Asked %s to allow pairing, to pass it on to the controller", found.name)
        return {"ticket": ticket}

    def asked(self, ticket: Any) -> dict[str, Any]:
        """pending, added, denied, expired, or failed (with what went wrong)."""
        entry = self.asking.get(ticket) if isinstance(ticket, str) else None
        if entry is None:
            return {"state": "expired"}
        return {k: entry[k] for k in ("state", "error") if k in entry}

    async def _wait(self, ticket: str, client: ScreenClient, request: str, token: str) -> None:
        entry = self.asking[ticket]
        try:
            while time.monotonic() < entry["until"]:
                await asyncio.sleep(ASK_EVERY)
                try:
                    answer = await client.approval_state(request)
                except Unreachable:
                    continue  # asked again in a moment
                state, paired = answer.get("state"), answer.get("token")
                if state == "approved" and isinstance(paired, str) and paired:
                    await self._pass_on(client, paired, token)
                    entry["state"] = "added"
                    return
                if state in ("denied", "expired"):
                    entry["state"] = state
                    return
            entry["state"] = "expired"
        except Unavailable as err:
            entry.update(state="failed", error=str(err))
        except Exception:  # noqa: BLE001 - said to the controller, and logged
            _LOGGER.exception("Passing %s on failed", client.found.name)
            entry.update(state="failed", error="the renderer couldn't pass it on: see its log")

    async def _pass_on(self, client: ScreenClient, paired: str, token: str) -> None:
        """With the pairing just made: the screen gets the controller's address for screens and
        the token, and this renderer lets go of the pairing."""
        client.token = paired
        try:
            info, state = await client.info(), await client.state()
            if CONNECTS_OUT not in (info.get("features") or state.get("features") or []):
                raise Unavailable(f"{client.found.name} can't connect out: it needs PleatView 1.0.19 or later")
            await client.enrol(self.screens_to[0], token, self.screens_to[1])
            _LOGGER.info("Passed %s on to the controller: it connects out to it now", client.found.name)
        finally:
            try:
                await client.unpair()
            except Unavailable as err:
                _LOGGER.warning("Couldn't let go of the pairing with %s: %s", client.found.name, err)
            client.token = ""
