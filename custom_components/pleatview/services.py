"""Services of the integration as a whole, not of one TV: its screens listed, and chosen ones
moved to a controller elsewhere (protocol v2's enrolment with a token,
docs/protocol-v2.md#enrolment). A PleatView Renderer at this site calls them when the site lets
it pass its screens on to the controller it connects out to (docs/renderer-connect-out.md).
Only administrators may call them; each move shows as a notification."""

from __future__ import annotations

import inspect
import logging
import re
from typing import Any

import voluptuous as vol

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError, Unauthorized, UnknownUser
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_register_admin_service

from .api import PleatViewError
from .const import DOMAIN
from .coordinator import PleatViewDevice

_LOGGER = logging.getLogger(__name__)

LIST_SCREENS = "list_screens"
ENROL_WITH = "enrol_with"
# What a screen needs to connect out (docs/protocol.md, features).
CONNECTS_OUT = "protocol_v2"
V2_ADDRESS = re.compile(r"^wss://[A-Za-z0-9.\-\[\]:]{1,255}/api/pleatview/v2$")

ENROL_SCHEMA = vol.Schema(
    {
        vol.Required("screen"): vol.All(cv.string, vol.Length(min=1, max=200)),
        vol.Required("url"): vol.All(cv.string, vol.Match(V2_ADDRESS)),
        vol.Required("token"): vol.All(cv.string, vol.Length(min=16, max=200)),
        vol.Optional("fingerprint"): vol.All(cv.string, vol.Match(r"^[0-9A-Fa-f:]{64,95}$")),
    }
)


def _devices(hass: HomeAssistant) -> list[PleatViewDevice]:
    """The TVs (not renderers) this Home Assistant has set up."""
    return [
        entry.runtime_data
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED and isinstance(getattr(entry, "runtime_data", None), PleatViewDevice)
    ]


def _features(device: PleatViewDevice) -> list[str]:
    return list(device.state.get("features") or device.info.get("features") or [])


def describe(device: PleatViewDevice) -> dict[str, Any]:
    """A screen as list_screens gives it: what a controller needs to choose it, and where it is
    for a renderer at the site (which doesn't pass that on)."""
    return {
        "id": device.device_id,
        "name": device.name,
        "manufacturer": device.info.get("manufacturer"),
        "model": device.info.get("model"),
        "app_version": device.state.get("app_version") or device.info.get("app_version"),
        "online": device.available,
        "connects_out": CONNECTS_OUT in _features(device),
        "host": device.client.host,
        "port": device.client.port,
    }


async def async_list_screens(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    return {"screens": [describe(device) for device in _devices(hass)]}


async def async_enrol_with(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    screen = call.data["screen"]
    device = next((d for d in _devices(hass) if d.device_id == screen), None)
    if device is None:
        raise ServiceValidationError(f"No PleatView screen {screen} is set up here")
    if CONNECTS_OUT not in _features(device):
        raise ServiceValidationError(f"{device.name} can't connect out: it needs PleatView 1.0.19 or later")
    url = call.data["url"]
    try:
        await device.client.enrol(url, call.data["token"], call.data.get("fingerprint"))
    except PleatViewError as err:
        raise HomeAssistantError(f"{device.name} didn't take it: {err}") from err
    host = url.split("/")[2]
    _LOGGER.info("%s connects out to the controller at %s now", device.name, host)
    persistent_notification.async_create(
        hass,
        f"{device.name} now connects out to the controller at {host}, as it was asked to. "
        "Home Assistant keeps its pairing and settings.",
        title="PleatView",
        notification_id=f"{DOMAIN}_enrolled_{device.device_id}",
    )
    return {"screen": device.device_id, "enrolled": True}


def _register_admin(hass: HomeAssistant, service: str, handler: Any, schema: vol.Schema,
                    supports_response: SupportsResponse) -> None:
    """A service for administrators only, with a response. Home Assistant 2025.1's helper for
    those can't give one: there the same check is made here."""
    if "supports_response" in inspect.signature(async_register_admin_service).parameters:
        async_register_admin_service(hass, DOMAIN, service, handler, schema, supports_response=supports_response)
        return

    async def admin_only(call: ServiceCall) -> ServiceResponse:
        if call.context.user_id:
            user = await hass.auth.async_get_user(call.context.user_id)
            if user is None:
                raise UnknownUser(context=call.context)
            if not user.is_admin:
                raise Unauthorized(context=call.context)
        return await handler(call)

    hass.services.async_register(DOMAIN, service, admin_only, schema, supports_response)


def async_setup_services(hass: HomeAssistant) -> None:
    async def list_screens(call: ServiceCall) -> ServiceResponse:
        return await async_list_screens(hass, call)

    async def enrol_with(call: ServiceCall) -> ServiceResponse:
        return await async_enrol_with(hass, call)

    _register_admin(hass, LIST_SCREENS, list_screens, vol.Schema({}), SupportsResponse.ONLY)
    _register_admin(hass, ENROL_WITH, enrol_with, ENROL_SCHEMA, SupportsResponse.OPTIONAL)
