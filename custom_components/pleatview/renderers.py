"""PleatView Renderers as entries of their own: paired with a PIN, named by the user (the
entry's title is the renderer's alias), watched for load, and chosen per TV.

A TV's *Render on Home Assistant* option holds one of:
- ``renderer:<entry id>``: that paired renderer;
- ``auto``: the least busy paired renderer, kept until it goes away;
- an address (``http://…``): an unpaired renderer, as before pairing existed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import hashlib
import logging
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import CONF_FINGERPRINT, CONF_KIND, CONF_SCHEME, CONF_TOKEN, DOMAIN, KIND_RENDERER, RENDER_AUTO, RENDERER_PREFIX

_LOGGER = logging.getLogger(__name__)

TIMEOUT = aiohttp.ClientTimeout(total=10)
POLL = timedelta(seconds=30)


@dataclass(frozen=True)
class Target:
    """Where a renderer is, and how to sign in to it."""

    base_url: str
    token: str | None = None
    fingerprint: str | None = None  # SHA-256 of its certificate, pinned when pairing (HTTPS)
    entry_id: str | None = None

    def kwargs(self) -> dict[str, Any]:
        """Request arguments: the token, and the pinned certificate instead of a CA."""
        out: dict[str, Any] = {}
        if self.token:
            out["headers"] = {"Authorization": f"Bearer {self.token}"}
        if self.fingerprint:
            out["ssl"] = aiohttp.Fingerprint(bytes.fromhex(self.fingerprint))
        return out


def base_url(data: dict[str, Any]) -> str:
    return f"{data.get(CONF_SCHEME, 'http')}://{data['host']}:{data['port']}"


def is_renderer(entry: ConfigEntry) -> bool:
    return entry.data.get(CONF_KIND) == KIND_RENDERER


def target_of(entry: ConfigEntry) -> Target:
    return Target(base_url(entry.data), entry.data.get(CONF_TOKEN), entry.data.get(CONF_FINGERPRINT), entry.entry_id)


async def async_fetch_certificate(hass: HomeAssistant, url: str) -> str:
    """The SHA-256 fingerprint of an HTTPS renderer's certificate, to pin when pairing."""
    async with async_get_clientsession(hass).get(f"{url}/api/info", ssl=False, timeout=TIMEOUT) as response:
        ssl_object = response.connection.transport.get_extra_info("ssl_object") if response.connection else None
        der = ssl_object.getpeercert(binary_form=True) if ssl_object else None
    if not der:
        raise aiohttp.ClientError("no certificate")
    return hashlib.sha256(der).hexdigest()


async def async_pair(hass: HomeAssistant, url: str, pin: str, fingerprint: str | None) -> str:
    """Exchanges the PIN in the renderer's log for a token. Raises ValueError with a reason."""
    target = Target(url, fingerprint=fingerprint)
    async with async_get_clientsession(hass).post(
        f"{url}/api/pair", json={"pin": pin, "client": "Home Assistant"}, timeout=TIMEOUT, **target.kwargs()
    ) as response:
        data = await response.json(content_type=None)
    if response.status == 200 and isinstance(data, dict) and data.get("token"):
        return str(data["token"])
    if response.status == 429:
        raise ValueError("too_many_attempts")
    raise ValueError("invalid_pin")


class RendererCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls a paired renderer's /api/info: its screens, and how busy its machine is."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=entry.title, update_interval=POLL)
        self.target = target_of(entry)

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            async with async_get_clientsession(self.hass).get(
                f"{self.target.base_url}/api/info", timeout=TIMEOUT, **self.target.kwargs()
            ) as response:
                if response.status == 401:
                    raise ConfigEntryAuthFailed("The renderer no longer accepts our token")
                if response.status != 200:
                    raise UpdateFailed(f"HTTP {response.status}")
                info = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise UpdateFailed(str(err) or type(err).__name__) from err
        if not isinstance(info, dict):
            raise UpdateFailed("not a renderer")
        return info

    @property
    def busy(self) -> float:
        """Screens in use out of its maximum, then its processor load: lower is less busy."""
        data = self.data or {}
        sessions = len(data.get("sessions") or {})
        maximum = max(int(data.get("max_screens") or 1), 1)
        return sessions / maximum + 0.1 * float((data.get("load") or {}).get("cpu") or 0)


def renderer_entries(hass: HomeAssistant) -> list[ConfigEntry]:
    return [e for e in hass.config_entries.async_entries(DOMAIN) if is_renderer(e)]


def choices(hass: HomeAssistant) -> list[dict[str, str]]:
    """The paired renderers, by name, for a TV's options."""
    out = []
    for entry in renderer_entries(hass):
        coordinator: RendererCoordinator | None = getattr(entry, "runtime_data", None)
        data = (coordinator.data if isinstance(coordinator, RendererCoordinator) else None) or {}
        detail = f"{len(data.get('sessions') or {})}/{data.get('max_screens', '?')} screens" if data else "not reached"
        out.append({"value": f"{RENDERER_PREFIX}{entry.entry_id}", "label": f"{entry.title} ({detail})"})
    return out


def resolve(hass: HomeAssistant, value: str, sticky: str | None = None) -> Target | None:
    """Where to render for a TV's option value. `sticky` is the renderer Automatic picked last."""
    value = (value or "").strip()
    if not value:
        return None
    if value.startswith(RENDERER_PREFIX):
        entry = hass.config_entries.async_get_entry(value[len(RENDERER_PREFIX):])
        return target_of(entry) if entry and is_renderer(entry) else None
    if value == RENDER_AUTO:
        loaded = [e for e in renderer_entries(hass) if e.state is ConfigEntryState.LOADED
                  and isinstance(getattr(e, "runtime_data", None), RendererCoordinator)
                  and e.runtime_data.last_update_success]
        if not loaded:
            return None
        for entry in loaded:
            if entry.entry_id == sticky:
                return target_of(entry)
        return target_of(min(loaded, key=lambda e: e.runtime_data.busy))
    return Target(value.rstrip("/"))


async def async_unpair(hass: HomeAssistant, entry: ConfigEntry) -> None:
    target = target_of(entry)
    try:
        async with async_get_clientsession(hass).post(
            f"{target.base_url}/api/unpair", json={}, timeout=TIMEOUT, **target.kwargs()
        ):
            pass
    except (aiohttp.ClientError, TimeoutError):
        _LOGGER.debug("The renderer %s wasn't reached to unpair", entry.title)
