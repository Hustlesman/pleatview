"""Paired screens: pairing, their live state, commands, and following them to new addresses.

Talks protocol v1 (docs/protocol.md) with the same client as the Home Assistant integration
(`tvapi.py` is a copy of custom_components/pleatview/api.py, checked by the tests).
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import math
import time
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from . import schedule
from .mdns import RENDERER_SERVICE, Browser
from .rendering import Rendering, Target
from .store import Store
from .tvapi import CannotConnect, InvalidAuth, PleatViewClient, PleatViewError

_LOGGER = logging.getLogger(__name__)

CLIENT_NAME = "PleatView Console"
RETRY_DELAYS = (2, 5, 10, 30, 60)
REJECTED_RETRY = 300
MOVE_CHECK_EVERY = 60
TEXT_MAX = 2000


class Invalid(ValueError):
    """A request the console won't send, with a reason for the person."""


def _text(value: Any, name: str, *, required: bool = False) -> str | None:
    if value is None or value == "":
        if required:
            raise Invalid(f"{name} is required")
        return None
    if not isinstance(value, str) or len(value) > TEXT_MAX:
        raise Invalid(f"{name} must be text of up to {TEXT_MAX} characters")
    return value


def web_url(value: Any, name: str, schemes: tuple[str, ...] = ("http", "https")) -> str:
    text = _text(value, name, required=True)
    parts = urlsplit(text)
    if parts.scheme not in schemes or not parts.netloc:
        raise Invalid(f"{name} must start with {' or '.join(s + '://' for s in schemes)}")
    return text


def _number(value: Any, name: str, low: float, high: float) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise Invalid(f"{name} must be a number")
    if not low <= value <= high:
        raise Invalid(f"{name} must be between {low:g} and {high:g}")
    return value


def command_params(command: str, body: dict[str, Any]) -> dict[str, Any]:
    """The parameters to send for a command, checked; only commands the web app offers."""
    duration = lambda: _number(body.get("duration"), "duration", 1, 86400)  # noqa: E731
    if command in ("home", "reload", "turn_on", "turn_off", "stop"):
        return {}
    if command == "show_url":
        return {"url": web_url(body.get("url"), "url"), "duration": duration()}
    if command == "show_dashboard":
        return {"path": _text(body.get("path"), "path", required=True), "duration": duration()}
    if command == "play_media":
        loop = body.get("loop")
        if loop is not None and not isinstance(loop, bool):
            raise Invalid("loop must be true or false")
        return {
            "url": web_url(body.get("url"), "url", ("http", "https", "rtsp", "rtsps")),
            "title": _text(body.get("title"), "title"),
            "loop": loop,
            "volume": _number(body.get("volume"), "volume", 0, 1),
        }
    if command == "notify":
        return {
            "message": _text(body.get("message"), "message", required=True),
            "title": _text(body.get("title"), "title"),
            "duration": duration(),
        }
    raise Invalid(f"unknown command {command}")


def check_address(host: str, port: Any) -> tuple[str, int]:
    """A screen typed in by address must be on a local network: a private or link-local IP
    address. That keeps the console from being used to probe anything else."""
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise Invalid("port must be a number from 1 to 65535")
    try:
        address = ipaddress.ip_address(str(host).strip("[]"))
    except ValueError:
        raise Invalid("Enter the screen's IP address, such as 192.168.1.20") from None
    if address.is_loopback or address.is_multicast or address.is_unspecified:
        raise Invalid("That address can't be a screen")
    if not (address.is_private or address.is_link_local):
        raise Invalid("Only screens on a local network can be added by address")
    return str(address), port


def config_rev(config: dict[str, Any]) -> str:
    """The revision the console stamps on the settings it gives a screen (its `rev`)."""
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:16]


class Link:
    """Follows one paired screen's state over its event stream, reconnecting as needed, and
    keeps the console's settings on it (unless Home Assistant manages the screen)."""

    def __init__(self, screens: Screens, tenant_id: str, screen: dict[str, Any], token: str) -> None:
        self.screens = screens
        self.tenant_id = tenant_id
        self.screen_id: int = screen["id"]
        self.device_id: str = screen["device_id"]
        self.client = PleatViewClient(screens.session, screen["host"], screen["port"], token)
        self.state: dict[str, Any] | None = None
        self.online = False
        self.rejected = False
        self.managed_by: str | None = None
        self.seen: float | None = None
        self.next_move_check = 0.0
        self.config: dict[str, Any] = screen.get("config") or {}
        self.applied_rev: str | None = None
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    def view(self) -> dict[str, Any]:
        return {
            "online": self.online,
            "rejected": self.rejected,
            "managed_by": self.managed_by,
            "seen": self.seen,
            "state": self.state,
        }

    async def _run(self) -> None:
        attempt = 0
        while True:
            try:
                await self.client.listen(self._on_state)
                attempt = 0  # the stream ran and ended; reconnect soon
            except InvalidAuth:
                # The screen no longer accepts our token: unpaired on the screen, or reset.
                self.rejected = True
                self._set_offline()
                await asyncio.sleep(REJECTED_RETRY)
                continue
            except PleatViewError as err:
                _LOGGER.debug("Screen %s: %s", self.device_id, err)
            self._set_offline()
            await asyncio.sleep(RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)])
            attempt += 1

    def _on_state(self, state: dict[str, Any]) -> None:
        first = not self.online
        self.state = state
        self.online = True
        self.rejected = False
        self.seen = time.time()
        if first:
            asyncio.create_task(self._read_config())
        self.screens.publish(self.tenant_id, self.screen_id)

    async def _read_config(self) -> None:
        """Whether Home Assistant manages this screen; the console then only sends commands.
        Otherwise the console's settings go back on the screen if anything changed them."""
        try:
            config = await self.client.get_config()
        except PleatViewError:
            return
        self.managed_by = "home_assistant" if config.get("ha_url") else None
        self.applied_rev = config.get("rev")
        screen = self.screens.store.tenant(self.tenant_id).screen(self.screen_id)
        if screen:
            await self.screens.apply(self.tenant_id, screen, self)
        self.screens.publish(self.tenant_id, self.screen_id)

    async def push_config(self) -> None:
        if self.managed_by or not self.config or not self.online:
            return
        rev = config_rev(self.config)
        if self.applied_rev == rev:
            return
        try:
            await self.client.set_config({**self.config, "rev": rev})
            self.applied_rev = rev
        except PleatViewError as err:
            _LOGGER.warning("Screen %s refused the console's settings: %s", self.device_id, err)

    def _set_offline(self) -> None:
        if self.online:
            self.online = False
            self.screens.publish(self.tenant_id, self.screen_id)


class Screens:
    """Every paired screen of every tenant, and the screens found on the network."""

    def __init__(self, store: Store, session: aiohttp.ClientSession, *, discovery: bool = True,
                 screen_url: str = "http://localhost:8791") -> None:
        self.store = store
        self.session = session
        # Where screens load the console's pages and media (the content listener).
        self.screen_url = screen_url.rstrip("/")
        self.rendering = Rendering(session, store.master)
        self.links: dict[tuple[str, int], Link] = {}
        self.browser = Browser(self._on_discovery) if discovery else None
        self._listeners: dict[str, set[asyncio.Queue]] = {}

    async def start(self) -> None:
        for tenant in self.store.tenants():
            for screen in self.store.tenant(tenant["id"]).screens():
                self._link(tenant["id"], screen)
        self._schedules = asyncio.create_task(self.follow_schedules())
        if self.browser:
            try:
                await self.browser.start()
            except OSError as err:
                _LOGGER.warning("Can't look for screens on the network: %s", err)
                self.browser = None

    async def stop(self) -> None:
        if getattr(self, "_schedules", None):
            self._schedules.cancel()
        if self.browser:
            await self.browser.stop()
        await asyncio.gather(*(link.stop() for link in self.links.values()))
        self.links.clear()

    # What screens load

    def page_url(self, tenant_id: str, page: dict[str, Any]) -> str:
        if page["kind"] == "web":
            return page["url"]
        return f"{self.screen_url}/play/{tenant_id}/p/{page['token']}"

    def media_url(self, tenant_id: str, item: dict[str, Any]) -> str | None:
        if item["status"] != "ready" or not item["playable"]:
            return None
        return f"{self.screen_url}/play/{tenant_id}/m/{item['token']}/{item['playable']}"

    def scheduled(self, tenant_id: str, screen: dict[str, Any]) -> dict[str, Any] | None:
        tenant = self.store.tenant(tenant_id)
        entries = tenant.schedules()
        if not entries:
            return None
        site = tenant.site(screen["site_id"]) or {"timezone": "UTC"}
        return schedule.active(entries, screen, tenant.groups_of(screen["id"]), site["timezone"])

    def desired(self, tenant_id: str, screen: dict[str, Any], features: list[str] | None = None) -> dict[str, Any]:
        """The settings the console keeps on a screen: its own, with the home page from the
        schedule that's on, else from the page chosen for it; and, for a screen a renderer
        draws, the stream (only to apps that play one)."""
        config = dict(screen["config"])
        on = self.scheduled(tenant_id, screen)
        page_id = on["page_id"] if on else screen.get("home_page")
        page = self.store.tenant(tenant_id).page(page_id) if page_id else None
        if page:
            config["home_dashboard"] = self.page_url(tenant_id, page)
        playlist: list[dict[str, Any]] = []
        if "home_playlist" in (features or []):
            # Apps that play (and cache) a playlist themselves get the list, not the page.
            if page and page["kind"] == "playlist":
                playlist = self.playlist_items(tenant_id, page)
            config["home_playlist"] = playlist
        if "render_stream" in (features or []):
            target = None if playlist else self.render_target(tenant_id, screen)
            config["render_stream"] = (f"{self.screen_url}/play/{tenant_id}/r/"
                                       f"{self.rendering.secret(tenant_id, screen['id'])}/index.m3u8") if target else ""
        return config

    def playlist_items(self, tenant_id: str, page: dict[str, Any]) -> list[dict[str, Any]]:
        """The playlist's media that are ready, with the addresses screens fetch them at."""
        tenant = self.store.tenant(tenant_id)
        items = []
        for entry in page["items"]:
            item = tenant.media_item(entry.get("media_id", 0))
            url = self.media_url(tenant_id, item) if item else None
            if url:
                items.append({"url": url, "kind": item["kind"], "seconds": entry.get("seconds") or 10})
        return items

    def render_target(self, tenant_id: str, screen: dict[str, Any]) -> Target | None:
        if not screen.get("renderer_id"):
            return None
        tenant = self.store.tenant(tenant_id)
        row = tenant.renderer(screen["renderer_id"])
        if not row:
            return None
        token = self.store.sealer(tenant_id).open(row["token"], f"renderer:{row['device_id']}")
        return Target(f"{row['scheme']}://{row['host']}:{row['port']}", token, row["fingerprint"])

    async def apply(self, tenant_id: str, screen: dict[str, Any], link: Link) -> bool:
        """Puts the console's current settings on a screen it manages, and has its renderer
        show its home page. Whether anything changed."""
        if link.managed_by:
            return False
        features = (link.state or {}).get("features") or []
        desired = self.desired(tenant_id, screen, features)
        target = (self.render_target(tenant_id, screen)
                  if "render_stream" in features and not desired.get("home_playlist") else None)
        if target and desired.get("home_dashboard"):
            await self.rendering.show(tenant_id, screen["id"], target, desired["home_dashboard"], link.state)
        elif not target:
            await self.rendering.stop(tenant_id, screen["id"])
        if desired == link.config and link.applied_rev == config_rev(desired):
            return False
        link.config = desired
        await link.push_config()
        return True

    async def follow_schedules(self) -> None:
        """Every half minute: screens whose schedule changed get their new home page."""
        while True:
            for tenant in self.store.tenants():
                try:
                    db = self.store.tenant(tenant["id"])
                except KeyError:
                    continue
                for screen in db.screens():
                    link = self.links.get((tenant["id"], screen["id"]))
                    if not link or link.managed_by:
                        continue
                    if await self.apply(tenant["id"], screen, link):
                        self.publish(tenant["id"], screen["id"])
            await asyncio.sleep(30)

    async def refresh(self, tenant_id: str, screen_ids: list[int] | None = None) -> None:
        """Puts the current settings on the screens (all of the tenant's when None)."""
        tenant = self.store.tenant(tenant_id)
        for screen in tenant.screens():
            if screen_ids is not None and screen["id"] not in screen_ids:
                continue
            link = self.links.get((tenant_id, screen["id"]))
            if link:
                await self.apply(tenant_id, screen, link)
            self.publish(tenant_id, screen["id"])

    # Reading

    def view(self, tenant_id: str, screen: dict[str, Any]) -> dict[str, Any]:
        link = self.links.get((tenant_id, screen["id"]))
        info = screen["info"]
        return {
            "id": screen["id"],
            "site_id": screen["site_id"],
            "device_id": screen["device_id"],
            "name": screen["name"],
            "host": screen["host"],
            "port": screen["port"],
            "model": " ".join(filter(None, (info.get("manufacturer"), info.get("model")))),
            "app_version": (link.state or {}).get("app_version") if link else None,
            "config": screen["config"],
            "home_page": screen.get("home_page"),
            "renderer_id": screen.get("renderer_id"),
            "plays_stream": "render_stream" in ((link.state or {}).get("features") or []) if link else False,
            "schedule": (self.scheduled(tenant_id, screen) or {}).get("name"),
            "home_without_ha": "home_without_ha" in ((link.state or {}).get("features") or []) if link else False,
            **(link.view() if link else {"online": False, "rejected": False, "managed_by": None,
                                         "seen": None, "state": None}),
        }

    def list(self, tenant_id: str) -> list[dict[str, Any]]:
        return [self.view(tenant_id, s) for s in self.store.tenant(tenant_id).screens()]

    def discovered_renderers(self, tenant_id: str) -> list[dict[str, Any]]:
        """Renderers announced on the network that this tenant hasn't paired."""
        if not self.browser:
            return []
        tenant = self.store.tenant(tenant_id)
        return sorted((r for r in self.browser.found(RENDERER_SERVICE).values()
                       if not tenant.renderer_by_device(r["id"])), key=lambda r: r["name"].lower())

    def discovered(self, tenant_id: str) -> list[dict[str, Any]]:
        """Screens announced on the network that this tenant hasn't paired."""
        if not self.browser:
            return []
        tenant = self.store.tenant(tenant_id)
        return sorted(
            (s for s in self.browser.found().values() if not tenant.screen_by_device(s["id"])),
            key=lambda s: s["name"].lower(),
        )

    # Changing

    async def pair(self, tenant_id: str, site_id: int, host: str, port: int, pin: str) -> int:
        tenant = self.store.tenant(tenant_id)
        if not tenant.site(site_id):
            raise Invalid("No such site")
        if not (isinstance(pin, str) and pin.isdigit() and len(pin) == 6):
            raise Invalid("The PIN is the 6 digits the screen shows")
        client = PleatViewClient(self.session, host, port)
        info = await client.get_info()
        device_id = info.get("id")
        if not isinstance(device_id, str) or not device_id:
            raise Invalid("That address isn't a PleatView screen")
        if tenant.screen_by_device(device_id):
            raise Invalid("That screen is already paired")
        token = await client.pair(pin, CLIENT_NAME)
        keep = {k: info.get(k) for k in ("manufacturer", "model", "api_version") if info.get(k)}
        screen_id = tenant.add_screen(
            site_id=site_id,
            device_id=device_id,
            name=str(info.get("name") or device_id)[:100],
            host=host,
            port=port,
            token=self.store.sealer(tenant_id).seal(token, f"screen:{device_id}"),
            info=keep,
        )
        self._link(tenant_id, tenant.screen(screen_id))
        self.publish(tenant_id, screen_id)
        return screen_id

    async def unpair(self, tenant_id: str, screen_id: int) -> None:
        """Revokes the console's token on the screen when it can be reached, and forgets it."""
        await self.rendering.stop(tenant_id, screen_id)
        link = self.links.pop((tenant_id, screen_id), None)
        if link:
            await link.stop()
            try:
                await link.client.unpair()
            except PleatViewError as err:
                _LOGGER.info("Screen %s not reached to unpair: %s", link.device_id, err)
        self.store.tenant(tenant_id).delete_screen(screen_id)
        self._send(tenant_id, {"type": "removed", "id": screen_id})

    async def configure(self, tenant_id: str, screen_id: int, config: dict[str, Any],
                        home_page: int | None | bool = False) -> None:
        """Keeps these settings (and home page, unless False) on the screen, now and whenever
        it comes back."""
        link = self.links.get((tenant_id, screen_id))
        if link and link.managed_by:
            raise Invalid("Home Assistant manages this screen's settings")
        tenant = self.store.tenant(tenant_id)
        tenant.update_screen(screen_id, config=config)
        if home_page is not False:
            tenant.set_home_page(screen_id, home_page)
        await self.refresh(tenant_id, [screen_id])

    async def command(self, tenant_id: str, screen_id: int, command: str, params: dict[str, Any]) -> None:
        link = self.links.get((tenant_id, screen_id))
        if not link:
            raise Invalid("No such screen")
        await link.client.command(command, **params)

    # Live updates for the web app

    def subscribe(self, tenant_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._listeners.setdefault(tenant_id, set()).add(queue)
        return queue

    def unsubscribe(self, tenant_id: str, queue: asyncio.Queue) -> None:
        self._listeners.get(tenant_id, set()).discard(queue)

    def publish(self, tenant_id: str, screen_id: int) -> None:
        screen = self.store.tenant(tenant_id).screen(screen_id)
        if screen:
            self._send(tenant_id, {"type": "screen", "screen": self.view(tenant_id, screen)})

    def publish_media(self, tenant_id: str, media_id: int) -> None:
        self._send(tenant_id, {"type": "media", "id": media_id})

    def _send(self, tenant_id: str, event: dict[str, Any]) -> None:
        for queue in list(self._listeners.get(tenant_id, ())):
            if queue.full():
                queue.get_nowait()  # a slow page loses the oldest update, not the newest
            queue.put_nowait(event)

    # Inside

    def _link(self, tenant_id: str, screen: dict[str, Any]) -> None:
        token = self.store.sealer(tenant_id).open(screen["token"], f"screen:{screen['device_id']}")
        link = Link(self, tenant_id, {**screen, "config": self.desired(tenant_id, screen)}, token)
        self.links[(tenant_id, screen["id"])] = link
        link.start()

    def _on_discovery(self) -> None:
        for tenant_id in self._listeners:
            self._send(tenant_id, {"type": "discovered"})
        if not self.browser:
            return
        found = self.browser.found()
        now = time.monotonic()
        for (tenant_id, screen_id), link in list(self.links.items()):
            seen = found.get(link.device_id)
            if not seen or (seen["host"], seen["port"]) == (link.client.host, link.client.port):
                continue
            if link.online or now < link.next_move_check:
                continue
            link.next_move_check = now + MOVE_CHECK_EVERY
            asyncio.create_task(self._follow(tenant_id, screen_id, seen["host"], seen["port"]))

    async def _follow(self, tenant_id: str, screen_id: int, host: str, port: int) -> None:
        """Moves to a screen's new address once it proves it issued our token there."""
        old = self.links.get((tenant_id, screen_id))
        if not old:
            return
        candidate = PleatViewClient(self.session, host, port, old.client.token)
        try:
            proven = await candidate.verify()
        except CannotConnect:
            return
        if not proven or self.links.get((tenant_id, screen_id)) is not old:
            return
        tenant = self.store.tenant(tenant_id)
        tenant.update_screen(screen_id, host=host, port=port)
        tenant.log("screen_moved", target=f"screen:{screen_id}", detail={"host": host, "port": port})
        await old.stop()
        self._link(tenant_id, tenant.screen(screen_id))
        self.publish(tenant_id, screen_id)
