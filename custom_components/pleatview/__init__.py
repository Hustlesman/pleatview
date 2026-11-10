"""PleatView: Home Assistant dashboards, media and notifications on Android/Google TV."""

from __future__ import annotations

from contextlib import suppress
from datetime import timedelta
import inspect
import logging

from homeassistant.auth.const import GROUP_ID_READ_ONLY, GROUP_ID_USER
from homeassistant.auth.models import TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN
from homeassistant.const import (
    CONF_HOST,
    CONF_PORT,
    EVENT_PANELS_UPDATED,
    MAJOR_VERSION,
    MINOR_VERSION,
    Platform,
    __version__ as HA_VERSION,
)
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import InvalidAuth, PleatViewClient, PleatViewError
from .const import (
    AUTH_MANAGED,
    AUTH_NONE,
    CONF_ALLOW_CONTROL,
    CONF_AUTH_MODE,
    CONF_HA_TOKEN,
    CONF_TOKEN,
    CONF_USER_ID,
    DOMAIN,
    ISSUE_OLD_HA,
    SAFE_HA_VERSION,
)
from .coordinator import PleatViewConfigEntry, PleatViewDevice
from .render import RenderStreamView
from .wall import WallView

_LOGGER = logging.getLogger(__name__)

LOVELACE_UPDATED = "lovelace_updated"  # fired when a dashboard's configuration is saved

PLATFORMS = [Platform.BINARY_SENSOR, Platform.MEDIA_PLAYER, Platform.NOTIFY, Platform.SENSOR]
VIEWS = f"{DOMAIN}_views"  # the stream and wall pages, registered with the first TV
TOKEN_LIFETIME = timedelta(days=3650)


def _client(hass: HomeAssistant, entry: PleatViewConfigEntry) -> PleatViewClient:
    return PleatViewClient(
        async_get_clientsession(hass),
        entry.data[CONF_HOST],
        entry.data[CONF_PORT],
        entry.data[CONF_TOKEN],
    )


async def async_setup_entry(hass: HomeAssistant, entry: PleatViewConfigEntry) -> bool:
    """Connect to the TV, hand it its configuration and start listening."""
    _async_check_ha_version(hass)
    client = _client(hass, entry)
    try:
        info = await client.get_info()
        state = await client.get_state()
    except InvalidAuth as err:
        raise ConfigEntryAuthFailed("The TV no longer accepts the pairing token") from err
    except PleatViewError as err:
        raise ConfigEntryNotReady(f"Cannot reach {client.base_url}: {err}") from err
    if info.get("id") != entry.unique_id:
        raise ConfigEntryNotReady(f"A different PleatView device answered at {client.base_url}")

    await _async_ensure_credentials(hass, entry)
    if not hass.data.get(VIEWS):
        hass.http.register_view(RenderStreamView(hass))
        hass.http.register_view(WallView())
        hass.data[VIEWS] = True

    device = PleatViewDevice(hass, entry, client, info, state)
    entry.runtime_data = device
    await device.async_refresh_dashboards()
    try:
        await device.async_sync_config()
    except PleatViewError as err:
        _LOGGER.warning("Could not configure %s: %s", entry.title, err)

    async def _async_dashboards_changed(_: Event) -> None:
        if await device.async_refresh_dashboards():
            try:
                await device.async_sync_config()
            except PleatViewError as err:
                _LOGGER.debug("Could not send the new dashboards to %s: %s", entry.title, err)

    # A dashboard was added, renamed or removed, or a dashboard's configuration saved.
    for event_type in (EVENT_PANELS_UPDATED, LOVELACE_UPDATED):
        entry.async_on_unload(hass.bus.async_listen(event_type, _async_dashboards_changed))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_create_background_task(hass, device.async_run(), f"pleatview {entry.title}")
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


def _async_check_ha_version(hass: HomeAssistant) -> None:
    """Ask for a Home Assistant update when dashboard cards have known security bugs."""
    if (MAJOR_VERSION, MINOR_VERSION) >= SAFE_HA_VERSION:
        ir.async_delete_issue(hass, DOMAIN, ISSUE_OLD_HA)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        ISSUE_OLD_HA,
        is_fixable=False,
        is_persistent=False,
        severity=ir.IssueSeverity.WARNING,
        learn_more_url="https://github.com/Hustlesman/pleatview/blob/main/SECURITY.md#home-assistants-version",
        translation_key=ISSUE_OLD_HA,
        translation_placeholders={"version": HA_VERSION, "safe_version": ".".join(map(str, SAFE_HA_VERSION))},
    )


async def _async_options_updated(hass: HomeAssistant, entry: PleatViewConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: PleatViewConfigEntry) -> bool:
    """Unload a config entry; the event stream task is cancelled with it."""
    if renderer := entry.runtime_data.renderer:
        await renderer.async_stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: PleatViewConfigEntry) -> None:
    """Reset the TV, revoke our pairing and delete the user created for it."""
    client = _client(hass, entry)
    with suppress(PleatViewError):
        if entry.data.get(CONF_AUTH_MODE) != AUTH_NONE:
            await client.set_config({"ha_url": "", "ha_token": ""})
        await client.unpair()
    if (user_id := entry.data.get(CONF_USER_ID)) and (user := await hass.auth.async_get_user(user_id)):
        await hass.auth.async_remove_user(user)


async def _async_ensure_credentials(hass: HomeAssistant, entry: PleatViewConfigEntry) -> None:
    """In managed mode, make sure a dedicated user and a valid token exist for the TV.

    The user is local-only, so the token is useless outside the home network.
    """
    if entry.data.get(CONF_AUTH_MODE) != AUTH_MANAGED:
        return
    user = None
    if user_id := entry.data.get(CONF_USER_ID):
        user = await hass.auth.async_get_user(user_id)
    token = entry.data.get(CONF_HA_TOKEN)
    if user is not None and token and hass.auth.async_validate_access_token(token) is not None:
        return

    if user is None:
        group = GROUP_ID_USER if entry.data.get(CONF_ALLOW_CONTROL, True) else GROUP_ID_READ_ONLY
        user = await hass.auth.async_create_user(
            f"PleatView {entry.title}", group_ids=[group], local_only=True
        )
    client_name = f"PleatView ({entry.title})"
    # Long-lived token names must be unique per user; drop a stale one first.
    for stale in [t for t in user.refresh_tokens.values() if t.client_name == client_name]:
        result = hass.auth.async_remove_refresh_token(stale)
        if inspect.isawaitable(result):
            await result
    refresh_token = await hass.auth.async_create_refresh_token(
        user,
        client_name=client_name,
        token_type=TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN,
        access_token_expiration=TOKEN_LIFETIME,
    )
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_USER_ID: user.id,
            CONF_HA_TOKEN: hass.auth.async_create_access_token(refresh_token),
        },
    )
