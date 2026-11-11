"""Dashboards rendered on the Home Assistant host, for TVs too weak to run them.

The PleatView Renderer (an add-on or a container) shows a TV's dashboard in Chromium on
a stronger machine and encodes the screen as one H.264 stream. The TV plays that stream
instead of running the page: a streaming stick decodes one video in hardware with ease,
where nine YouTube players or a heavy dashboard overwhelm it. Home Assistant passes the
stream on, so the renderer needs no port of its own on the network.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
import hmac
import logging
import re
import secrets
from typing import TYPE_CHECKING, Any

import aiohttp
from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .renderers import Target, resolve

if TYPE_CHECKING:
    from .coordinator import PleatViewConfigEntry

_LOGGER = logging.getLogger(__name__)

STREAM_FILE = re.compile(r"^(index\.m3u8|s\d{1,9}\.ts)$")
TIMEOUT = aiohttp.ClientTimeout(total=20)
FIND_TIMEOUT = aiohttp.ClientTimeout(total=3)
# Where a renderer usually is: the Home Assistant OS app added from this repository (or a local
# copy of it), or the documented `docker run` on this machine.
CANDIDATES = (
    "http://ce7dee09-pleatview-renderer:8780",
    "http://local-pleatview-renderer:8780",
    "http://127.0.0.1:8780",
)


async def async_find_renderers(hass: HomeAssistant, extra: tuple[str, ...] = ()) -> list[dict[str, str]]:
    """The renderers that answer, as choices for a TV's options: the usual places, and any
    address already in use."""
    session = async_get_clientsession(hass)

    async def probe(url: str) -> dict[str, str] | None:
        try:
            async with session.get(f"{url}/api/info", timeout=FIND_TIMEOUT, allow_redirects=False) as resp:
                if resp.status != 200:
                    return None
                info = await resp.json(content_type=None)
        except Exception:  # noqa: BLE001 - a place that doesn't answer is simply left out
            return None
        if not isinstance(info, dict) or "encoder" not in info:
            return None
        return {"value": url, "label": f"{url} · PleatView Renderer {info.get('version', '')} ({info['encoder']})"}

    urls = list(dict.fromkeys([*CANDIDATES, *(u.strip().rstrip("/") for u in extra if u and u.strip())]))
    return [found for found in await asyncio.gather(*(probe(u) for u in urls)) if found]


@dataclass(frozen=True)
class Screen:
    """What the renderer shows for a TV; the renderer's PUT /api/sessions body."""

    url: str
    width: int = 1920
    height: int = 1080
    scale: float = 2.0
    kiosk: str = "full"
    ha_url: str = ""
    token: str = field(default="", repr=False)


def screen_size(state: dict[str, Any]) -> tuple[int, int, float]:
    """The TV's screen in pixels and its density, from its state; a 1080p TV if it doesn't say."""
    screen = state.get("screen")
    if isinstance(screen, dict):
        try:
            width, height, density = int(screen["width"]), int(screen["height"]), float(screen["density"])
        except (KeyError, TypeError, ValueError):
            pass
        else:
            if width >= 160 and height >= 160 and 0.5 <= density <= 8:
                # The renderer draws at most 1080p: more costs the host a lot and the TV little.
                factor = min(1.0, 1920 / max(width, height), 1080 / min(width, height))
                return round(width * factor), round(height * factor), density * factor
    return 1920, 1080, 2.0


class Renderer:
    """One TV's screen on a PleatView Renderer: a paired one by name, the least busy one
    (Automatic), or one at an address (renderers.resolve)."""

    def __init__(self, hass: HomeAssistant, choice: str, key: str) -> None:
        self.hass = hass
        self.choice = choice
        self.key = key
        self._picked: str | None = None
        # Part of the stream's address, so only the TV it was given to can play it.
        self.secret = secrets.token_urlsafe(18)
        self._shown: Screen | None = None
        self._last: Screen | None = None
        self._lock = asyncio.Lock()

    @property
    def target(self) -> Target | None:
        target = resolve(self.hass, self.choice, self._picked)
        if target and target.entry_id != self._picked:
            self._picked = target.entry_id
            self._shown = None  # another renderer: it has to be told
        return target

    @property
    def base_url(self) -> str:
        return target.base_url if (target := self.target) else ""

    @property
    def stream_path(self) -> str:
        return f"/api/pleatview/render/{self.key}/{self.secret}/index.m3u8"

    async def async_show(self, screen: Screen) -> None:
        """Has the renderer show [screen]; nothing to do when it already does."""
        async with self._lock:
            self._last = screen
            if screen == self._shown:
                return
            if not (target := self.target):
                return
            try:
                async with async_get_clientsession(self.hass).put(
                    f"{target.base_url}/api/sessions/{self.key}", json=asdict(screen), timeout=TIMEOUT, **target.kwargs()
                ) as response:
                    if response.status != 200:
                        _LOGGER.warning("The PleatView Renderer at %s refused %s: %s",
                                        self.base_url, screen.url, (await response.text())[:200])
                        return
            except (aiohttp.ClientError, TimeoutError) as err:
                _LOGGER.warning("Could not reach the PleatView Renderer at %s: %s", self.base_url, err)
                return
            self._shown = screen

    async def async_stop(self) -> None:
        self._shown = None
        if not (target := self.target):
            return
        try:
            async with async_get_clientsession(self.hass).delete(
                f"{target.base_url}/api/sessions/{self.key}", timeout=TIMEOUT, **target.kwargs()
            ):
                pass
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug("Could not stop the screen on the PleatView Renderer: %s", err)

    async def async_fetch(self, name: str) -> web.Response:
        """A file of the stream, from the renderer."""
        if not (target := self.target):
            return web.Response(status=503)
        try:
            async with async_get_clientsession(self.hass).get(
                f"{target.base_url}/hls/{self.key}/{name}", timeout=TIMEOUT, **target.kwargs()
            ) as response:
                if response.status == 404 and self._last:
                    # The renderer restarted and forgot this screen: show it again.
                    self._shown = None
                    self.hass.async_create_background_task(self.async_show(self._last), "pleatview render again")
                if response.status != 200:
                    return web.Response(status=response.status)
                return web.Response(
                    body=await response.read(),
                    content_type=response.content_type,
                    headers={"Cache-Control": response.headers.get("Cache-Control", "no-cache")},
                )
        except (aiohttp.ClientError, TimeoutError):
            return web.Response(status=502)


class RenderStreamView(HomeAssistantView):
    """The rendered stream, for the TV: Home Assistant passes it on from the renderer."""

    url = "/api/pleatview/render/{entry_id}/{secret}/{name}"
    name = "api:pleatview:render"
    # The TV's player sends no Home Assistant token; the secret in the address stands in.
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, entry_id: str, secret: str, name: str) -> web.Response:
        entry: PleatViewConfigEntry | None = self.hass.config_entries.async_get_entry(entry_id)
        device = getattr(entry, "runtime_data", None) if entry else None
        renderer: Renderer | None = getattr(device, "renderer", None)
        if renderer is None or not STREAM_FILE.match(name) or not hmac.compare_digest(secret, renderer.secret):
            return web.Response(status=404)
        return await renderer.async_fetch(name)
