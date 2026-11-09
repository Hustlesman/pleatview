"""Connection and state for one PleatView TV."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import hashlib
import json
import logging
from typing import Any

from homeassistant.components.media_player import async_process_play_media_url
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.network import NoURLAvailableError, get_url
from yarl import URL

from .api import (
    InvalidAuth,
    PleatViewClient,
    PleatViewError,
    RequestRejected,
)
from .const import (
    AUTH_NONE,
    CONF_AUTH_MODE,
    CONF_DASHBOARDS,
    CONF_HA_TOKEN,
    CONF_HA_URL,
    CONF_HOME_DASHBOARD,
    CONF_KEEP_SCREEN_ON,
    CONF_KIOSK,
    CONF_LAUNCH_ON_BOOT,
    CONF_RELOAD_MINUTES,
    CONF_RENDER_URL,
    CONF_REOPEN_MINUTES,
    CONF_RETURN_HOME_MINUTES,
    CONF_ROTATE_SECONDS,
    CONF_ZOOM,
    DEFAULT_OPTIONS,
)
from .render import Renderer, Screen, screen_size

_LOGGER = logging.getLogger(__name__)

RECONNECT_MIN = 1
RECONNECT_MAX = 30
# Older apps switch to any Home Assistant path inside the frontend, which shows the
# default dashboard for a page such as /local/streams.html.
APP_OPENS_LOCAL_PAGES = (1, 0, 12)


def app_version(value: str | None) -> tuple[int, ...]:
    """1.0.12 as (1, 0, 12), ignoring a suffix such as -debug; (0,) if unreadable."""
    try:
        return tuple(int(part) for part in (value or "").split("-")[0].split("."))
    except ValueError:
        return (0,)

type PleatViewConfigEntry = ConfigEntry[PleatViewDevice]


class PleatViewDevice:
    """Keeps an event stream open to the TV and fans state out to entities."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: PleatViewConfigEntry,
        client: PleatViewClient,
        info: dict[str, Any],
        state: dict[str, Any],
    ) -> None:
        """Initialize the device."""
        self.hass = hass
        self.entry = entry
        self.client = client
        self.info = info
        self.state = state
        self.available = True
        self.webpages: dict[str, str] = {}
        self.dashboards = resolve_dashboards(hass, entry.options.get(CONF_DASHBOARDS, ""))
        render_url = (entry.options.get(CONF_RENDER_URL) or "").strip()
        self.renderer = Renderer(hass, render_url, entry.entry_id) if render_url else None
        self._listeners: list[Callable[[], None]] = []
        self._received = False

    @property
    def device_id(self) -> str:
        """Return the TV's stable identifier."""
        return str(self.entry.unique_id)

    @property
    def name(self) -> str:
        """Return the TV's name."""
        return self.entry.title

    @callback
    def async_add_listener(self, update_callback: Callable[[], None]) -> Callable[[], None]:
        """Register a callback for state changes; returns a remover."""
        self._listeners.append(update_callback)

        @callback
        def remove() -> None:
            self._listeners.remove(update_callback)

        return remove

    @property
    def ha_url(self) -> str:
        """The address the TV uses for Home Assistant."""
        return self.entry.options.get(CONF_HA_URL) or default_ha_url(self.hass)

    @property
    def rendering(self) -> bool:
        """Whether the renderer runs this TV's dashboards: set up, and an app that plays them."""
        return self.renderer is not None and "render_stream" in (self.state.get("features") or [])

    def target(self, path: str) -> str:
        """What the TV loads for a dashboard path: the site itself for Webpage dashboards."""
        url = self.webpages.get(path.rstrip("/"), path)
        if url != path and self._on_home_assistant(url):
            version = self.state.get("app_version") or self.info.get("app_version")
            if app_version(version) < APP_OPENS_LOCAL_PAGES:
                return path  # in Home Assistant's frame, which every version shows correctly
        return url

    def _on_home_assistant(self, url: str) -> bool:
        """Whether a Webpage dashboard shows a page Home Assistant serves, such as /local/x.html."""
        if url.startswith("/"):
            return True
        try:
            return URL(url).origin() == URL(self.ha_url).origin()
        except ValueError:
            return False

    async def async_refresh_dashboards(self) -> bool:
        """Re-read Home Assistant's dashboards; returns whether the list changed."""
        self.webpages = await async_webpages(self.hass)
        dashboards = [
            {**dashboard, "path": self.target(dashboard["path"])}
            for dashboard in resolve_dashboards(self.hass, self.entry.options.get(CONF_DASHBOARDS, ""))
        ]
        if dashboards == self.dashboards:
            return False
        self.dashboards = dashboards
        self._notify()
        return True

    async def async_command(self, command: str, **params: Any) -> None:
        """Send a command, translating failures into Home Assistant errors."""
        try:
            await self.client.command(command, **params)
        except InvalidAuth as err:
            self.entry.async_start_reauth(self.hass)
            raise HomeAssistantError(
                f"{self.name} no longer accepts Home Assistant's pairing; pair it again"
            ) from err
        except RequestRejected as err:
            raise HomeAssistantError(f"{self.name} rejected {command}: {err}") from err
        except PleatViewError as err:
            raise HomeAssistantError(f"Could not reach {self.name}: {err}") from err

    def media_url(self, url: str) -> str:
        """An address the TV can open for media, a picture or a camera stream.

        Home Assistant's own paths are signed and go through the same URL as the TV's
        dashboards, which the "Home Assistant URL" option may set.
        """
        url = async_process_play_media_url(self.hass, url, allow_relative_url=True)
        if not url.startswith("/"):
            return url
        base = self.entry.options.get(CONF_HA_URL) or default_ha_url(self.hass)
        if not base:
            raise HomeAssistantError("No URL for Home Assistant is known; set one in the PleatView options")
        return base.rstrip("/") + url

    def desired_config(self) -> dict[str, Any]:
        """Configuration Home Assistant manages on the TV, tagged with a revision."""
        options = {**DEFAULT_OPTIONS, **self.entry.options}
        config: dict[str, Any] = {
            "ha_url": self.ha_url,
            "home_dashboard": self.target(options.get(CONF_HOME_DASHBOARD) or ""),
            "dashboards": self.dashboards,
            "kiosk": options[CONF_KIOSK],
            "zoom": int(options[CONF_ZOOM]),
            "rotate_seconds": int(options[CONF_ROTATE_SECONDS]),
            "return_home_minutes": int(options[CONF_RETURN_HOME_MINUTES]),
            "reopen_minutes": int(options[CONF_REOPEN_MINUTES]),
            "reload_minutes": int(options[CONF_RELOAD_MINUTES]),
            "keep_screen_on": bool(options[CONF_KEEP_SCREEN_ON]),
            "launch_on_boot": bool(options[CONF_LAUNCH_ON_BOOT]),
        }
        if self.entry.data.get(CONF_AUTH_MODE) != AUTH_NONE:
            config["ha_token"] = self.entry.data.get(CONF_HA_TOKEN, "")
        if self.rendering:
            assert self.renderer
            config["render_stream"] = self.ha_url.rstrip("/") + self.renderer.stream_path
        digest = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
        config["rev"] = digest[:16]
        return config

    async def async_sync_config(self, *, force: bool = False) -> None:
        """Push the managed configuration if the TV's copy is out of date."""
        desired = self.desired_config()
        if not desired["ha_url"]:
            _LOGGER.warning(
                "No URL for Home Assistant is known; set one in the PleatView options"
            )
        if force or self.state.get("config_rev") != desired["rev"]:
            await self.client.set_config(desired)

    async def async_run(self) -> None:
        """Keep the event stream open for the lifetime of the config entry."""
        delay = RECONNECT_MIN
        while True:
            self._received = False
            try:
                await self.client.listen(self._handle_state)
            except InvalidAuth:
                self._set_available(False)
                self.entry.async_start_reauth(self.hass)
                return
            except PleatViewError as err:
                _LOGGER.debug("Event stream from %s ended: %s", self.name, err)
            self._set_available(False)
            if self._received:
                delay = RECONNECT_MIN
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX)

    @callback
    def _handle_state(self, state: dict[str, Any]) -> None:
        first = not self._received
        updated = (state.get("app_version"), state.get("features")) != (
            self.state.get("app_version"), self.state.get("features")
        )
        self._received = True
        self.state = state
        self.available = True
        if first and state.get("config_rev") != self.desired_config()["rev"]:
            # The TV restarted with other settings (e.g. app data cleared): re-apply ours.
            self.entry.async_create_background_task(
                self.hass, self._async_resync(), "pleatview config sync"
            )
        if updated:
            # A newer app may open Webpage dashboards it couldn't before (see target()),
            # or play dashboards the renderer runs.
            self.entry.async_create_background_task(
                self.hass, self._async_app_updated(), "pleatview dashboards"
            )
        if self.rendering:
            self.entry.async_create_background_task(self.hass, self.async_render(), "pleatview render")
        self._notify()

    async def _async_app_updated(self) -> None:
        if await self.async_refresh_dashboards() or self.renderer:
            await self._async_resync()

    async def async_render(self) -> None:
        """Has the renderer show what the TV would: its page, at its size and zoom."""
        if not self.renderer or not (url := self.state.get("url")) or not url.startswith(("http://", "https://")):
            return
        options = {**DEFAULT_OPTIONS, **self.entry.options}
        width, height, density = screen_size(self.state)
        token = self.entry.data.get(CONF_HA_TOKEN, "") if self.entry.data.get(CONF_AUTH_MODE) != AUTH_NONE else ""
        await self.renderer.async_show(Screen(
            url=url, width=width, height=height, scale=round(density * int(options[CONF_ZOOM]) / 100, 3),
            kiosk=options[CONF_KIOSK], ha_url=self.ha_url, token=token,
        ))

    async def _async_resync(self) -> None:
        try:
            await self.async_sync_config(force=True)
        except PleatViewError as err:
            _LOGGER.warning("Could not update the configuration of %s: %s", self.name, err)

    @callback
    def _set_available(self, available: bool) -> None:
        if self.available != available:
            self.available = available
            self._notify()

    @callback
    def _notify(self) -> None:
        for update_callback in list(self._listeners):
            update_callback()


def default_ha_url(hass: HomeAssistant) -> str:
    """URL the TV should use for Home Assistant: the internal one when possible."""
    for kwargs in ({"allow_external": False, "allow_cloud": False}, {}):
        try:
            return get_url(hass, **kwargs)
        except NoURLAvailableError:
            continue
    return ""


def parse_dashboards(text: str) -> list[dict[str, str]]:
    """Parse ``Title = /path`` lines (or bare paths) from the options form."""
    dashboards = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        title, sep, path = line.partition("=")
        if not sep:
            title, path = "", line
        title, path = title.strip(), path.strip()
        if not path:
            continue
        if not path.startswith(("/", "http://", "https://")):
            path = f"/{path}"
        dashboards.append({"title": title or path, "path": path})
    return dashboards


# Frontend panels that make sense on a TV. "home" is the overview dashboard since
# 2026; light/climate/security/maintenance are its summary dashboards.
DASHBOARD_PANELS = {
    "lovelace", "home", "energy", "light", "climate", "security", "maintenance", "calendar", "todo",
}
PANEL_ORDER = ["/home", "/lovelace"]


def discover_dashboards(hass: HomeAssistant) -> list[dict[str, str]]:
    """List the dashboards registered with the frontend."""
    panels = hass.data.get("frontend_panels") or {}
    dashboards = []
    for url_path, panel in panels.items():
        component = getattr(panel, "component_name", None)
        if component not in DASHBOARD_PANELS or getattr(panel, "require_admin", False):
            continue
        if url_path == "lovelace":
            title = "Overview"
        else:
            title = getattr(panel, "sidebar_title", None) or url_path
            if component != "lovelace" or title == url_path:
                # Built-in panels use translation keys as titles.
                title = title.replace("-", " ").replace("_", " ").capitalize()
        dashboards.append({"title": title, "path": f"/{url_path}"})

    def order(item: dict[str, str]) -> tuple[int, str]:
        path = item["path"]
        return (PANEL_ORDER.index(path) if path in PANEL_ORDER else len(PANEL_ORDER), item["title"].lower())

    return sorted(dashboards, key=order)


def resolve_dashboards(hass: HomeAssistant, configured: str) -> list[dict[str, str]]:
    """Dashboards from the options, or every dashboard Home Assistant has."""
    return parse_dashboards(configured) or discover_dashboards(hass)


async def async_webpages(hass: HomeAssistant) -> dict[str, str]:
    """Map the paths of Home Assistant's Webpage dashboards to the sites they show.

    Home Assistant shows those sites in a frame, which many sites refuse or where they
    lose their sign-in cookie. The TV opens the site itself instead, and also a page of
    Home Assistant's own, such as /local/streams.html, without the frontend underneath.
    """
    data = hass.data.get("lovelace")
    # A dict in older Home Assistant versions (2025.1), a dataclass in newer ones.
    dashboards = data.get("dashboards", {}) if isinstance(data, dict) else getattr(data, "dashboards", {})
    webpages = {}
    for url_path, dashboard in list(dashboards.items()):
        if not url_path:
            continue
        try:
            config = await dashboard.async_load(False)
        except HomeAssistantError:
            continue  # a new dashboard without a configuration yet
        strategy = config.get("strategy") if isinstance(config, dict) else None
        url = strategy.get("url") if isinstance(strategy, dict) and strategy.get("type") == "iframe" else None
        if not isinstance(url, str):
            continue
        url = url.strip()
        if url.startswith(("http://", "https://")) or (url.startswith("/") and not url.startswith("//")):
            webpages[f"/{url_path}"] = url
    return webpages
