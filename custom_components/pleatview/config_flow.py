"""Config flow for PleatView."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import suppress
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

try:
    from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
except ImportError:  # Home Assistant 2025.1
    from homeassistant.components.zeroconf import ZeroconfServiceInfo

from .api import InvalidPin, PleatViewClient, PleatViewError, TooManyAttempts
from .const import (
    AUTH_MANAGED,
    AUTH_NONE,
    AUTH_TOKEN,
    CONF_ALLOW_CONTROL,
    CONF_AUTH_MODE,
    CONF_DASHBOARDS,
    CONF_HA_TOKEN,
    CONF_HA_URL,
    CONF_HOME_DASHBOARD,
    CONF_KEEP_SCREEN_ON,
    CONF_KIOSK,
    CONF_LAUNCH_ON_BOOT,
    CONF_RELOAD_MINUTES,
    CONF_REOPEN_MINUTES,
    CONF_RETURN_HOME_MINUTES,
    CONF_ROTATE_SECONDS,
    CONF_TOKEN,
    CONF_USER_ID,
    CONF_ZOOM,
    DEFAULT_OPTIONS,
    DEFAULT_PORT,
    DOMAIN,
    KIOSK_MODES,
)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
    }
)
PIN_SCHEMA = vol.Schema({vol.Required("pin"): str})

_LOGGER = logging.getLogger(__name__)


class PleatViewConfigFlow(ConfigFlow, domain=DOMAIN):
    """Discover or add a TV, pair with its PIN, then choose how it signs in."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._host = ""
        self._port = DEFAULT_PORT
        self._info: dict[str, Any] = {}
        self._token: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return PleatViewOptionsFlow()

    def _client(self) -> PleatViewClient:
        return PleatViewClient(async_get_clientsession(self.hass), self._host, self._port, self._token)

    @property
    def _name(self) -> str:
        return self._info.get("name") or "PleatView"

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Add a TV by address."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._host = user_input[CONF_HOST].strip()
            self._port = user_input[CONF_PORT]
            try:
                self._info = await self._client().get_info()
            except PleatViewError:
                errors["base"] = "cannot_connect"
            else:
                # An address typed in wins over an open discovery of the same TV, which may be
                # stuck on an address Home Assistant can't reach. Creating the entry closes it.
                await self.async_set_unique_id(self._info["id"], raise_on_progress=False)
                self._abort_if_unique_id_configured(updates={CONF_HOST: self._host, CONF_PORT: self._port})
                return await self.async_step_pair()
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_zeroconf(self, discovery_info: ZeroconfServiceInfo) -> ConfigFlowResult:
        """Handle a TV advertising _pleatview._tcp."""
        device_id = discovery_info.properties.get("id")
        if not device_id:
            return self.async_abort(reason="invalid_discovery_info")
        self._host = discovery_info.host
        self._port = discovery_info.port or DEFAULT_PORT
        await self.async_set_unique_id(device_id)
        for entry in self._async_current_entries(include_ignore=False):
            if entry.unique_id == device_id:
                await self._async_follow_moved_tv(entry)
        try:
            self._info = await self._client().get_info()
        except PleatViewError:
            self._abort_if_unique_id_configured()
            return self.async_abort(reason="cannot_connect")
        self._abort_if_unique_id_configured()
        self.context["title_placeholders"] = {"name": self._name}
        # Entering the PIN is the confirmation: no separate "Set up this TV?" step.
        return await self.async_step_pair()

    async def _async_follow_moved_tv(self, entry: ConfigEntry) -> None:
        """Move a paired TV to the address it announced, once it proves it's that TV; then abort.

        Anyone on the network can announce a TV's id, which isn't secret. Moving the entry
        sends that address the pairing token and Home Assistant's own token for the TV. TVs
        also announce addresses Home Assistant can't reach, such as a VPN's or an emulator's.
        """
        if (entry.data[CONF_HOST], entry.data[CONF_PORT]) != (self._host, self._port):
            client = PleatViewClient(
                async_get_clientsession(self.hass), self._host, self._port, entry.data[CONF_TOKEN]
            )
            try:
                proven = await client.verify()
            except PleatViewError:
                proven = None  # nothing answers there
            if proven:
                self._abort_if_unique_id_configured(updates={CONF_HOST: self._host, CONF_PORT: self._port})
            if proven is False:
                _LOGGER.warning(
                    "%s:%s announced itself as %s but couldn't prove it is that TV, so Home Assistant "
                    "stays with %s:%s. PleatView 1.0.4 and later prove it; with an older app, update it "
                    "or add the TV again with its new address",
                    self._host, self._port, entry.title, entry.data[CONF_HOST], entry.data[CONF_PORT],
                )
        self._abort_if_unique_id_configured()

    async def async_step_pair(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the PIN shown on the TV."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                self._token = await self._client().pair(user_input["pin"], "Home Assistant")
            except InvalidPin:
                errors["base"] = "invalid_pin"
            except TooManyAttempts:
                errors["base"] = "too_many_attempts"
            except PleatViewError:
                errors["base"] = "cannot_connect"
            else:
                if self.source == SOURCE_REAUTH:
                    return self.async_update_reload_and_abort(
                        self._get_reauth_entry(),
                        data_updates={CONF_HOST: self._host, CONF_PORT: self._port, CONF_TOKEN: self._token},
                    )
                # Pairing is the only question: the TV signs in with a dedicated user.
                # Other sign-in methods are under Reconfigure.
                return self.async_create_entry(
                    title=self._name,
                    data={
                        CONF_HOST: self._host,
                        CONF_PORT: self._port,
                        CONF_TOKEN: self._token,
                        CONF_AUTH_MODE: AUTH_MANAGED,
                        CONF_ALLOW_CONTROL: True,
                    },
                )
        return self.async_show_form(
            step_id="pair",
            data_schema=PIN_SCHEMA,
            description_placeholders={"name": self._name},
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Change how the TV signs in to Home Assistant."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            mode = user_input[CONF_AUTH_MODE]
            token = (user_input.get(CONF_HA_TOKEN) or "").strip()
            if mode == AUTH_TOKEN and not token and entry.data.get(CONF_AUTH_MODE) == AUTH_TOKEN:
                token = entry.data.get(CONF_HA_TOKEN, "")  # unchanged
            if mode == AUTH_TOKEN and self.hass.auth.async_validate_access_token(token) is None:
                errors[CONF_HA_TOKEN] = "invalid_ha_token"
            else:
                return await self._async_apply_sign_in(entry, mode, user_input[CONF_ALLOW_CONTROL], token)
        schema = vol.Schema(
            {
                vol.Required(CONF_AUTH_MODE, default=entry.data.get(CONF_AUTH_MODE, AUTH_MANAGED)): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[AUTH_MANAGED, AUTH_TOKEN, AUTH_NONE],
                        translation_key="auth_mode",
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
                vol.Required(CONF_ALLOW_CONTROL, default=entry.data.get(CONF_ALLOW_CONTROL, True)): selector.BooleanSelector(),
                vol.Optional(CONF_HA_TOKEN): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
            }
        )
        return self.async_show_form(step_id="reconfigure", data_schema=schema, errors=errors)

    async def _async_apply_sign_in(
        self, entry: ConfigEntry, mode: str, allow_control: bool, token: str
    ) -> ConfigFlowResult:
        data = {**entry.data, CONF_AUTH_MODE: mode, CONF_ALLOW_CONTROL: allow_control}
        was_managed = entry.data.get(CONF_AUTH_MODE) == AUTH_MANAGED
        if was_managed and (mode != AUTH_MANAGED or allow_control != entry.data.get(CONF_ALLOW_CONTROL, True)):
            # Remove the old user; setup creates a new one with the right permissions if still wanted.
            if (user_id := data.pop(CONF_USER_ID, None)) and (user := await self.hass.auth.async_get_user(user_id)):
                await self.hass.auth.async_remove_user(user)
            data.pop(CONF_HA_TOKEN, None)
        if mode == AUTH_TOKEN:
            data[CONF_HA_TOKEN] = token
        elif mode == AUTH_NONE:
            data.pop(CONF_HA_TOKEN, None)
            # Forget the token Home Assistant provided, so the TV shows the login page again.
            client = PleatViewClient(
                async_get_clientsession(self.hass), entry.data[CONF_HOST], entry.data[CONF_PORT], entry.data[CONF_TOKEN]
            )
            with suppress(PleatViewError):
                await client.set_config({"ha_token": ""})
        return self.async_update_reload_and_abort(entry, data=data)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """The TV forgot us (app data cleared or pairing reset): pair again."""
        self._host = entry_data[CONF_HOST]
        self._port = entry_data[CONF_PORT]
        try:
            self._info = await self._client().get_info()
        except PleatViewError:
            return self.async_abort(reason="cannot_connect")
        return await self.async_step_pair()


class PleatViewOptionsFlow(OptionsFlow):
    """Dashboard and display settings, pushed to the TV."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage the options."""
        errors: dict[str, str] = {}
        if user_input is not None:
            url = (user_input.get(CONF_HA_URL) or "").strip()
            if url and not url.startswith(("http://", "https://")):
                errors[CONF_HA_URL] = "invalid_url"
            else:
                return self.async_create_entry(data=user_input)

        options = {**DEFAULT_OPTIONS, **self.config_entry.options, **(user_input or {})}

        def number(maximum: int, unit: str, step: int = 1) -> selector.NumberSelector:
            return selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0, max=maximum, step=step, unit_of_measurement=unit,
                    mode=selector.NumberSelectorMode.BOX,
                )
            )

        schema = vol.Schema(
            {
                vol.Optional(CONF_HA_URL, description={"suggested_value": options.get(CONF_HA_URL)}): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.URL)
                ),
                vol.Optional(
                    CONF_HOME_DASHBOARD, description={"suggested_value": options.get(CONF_HOME_DASHBOARD)}
                ): selector.TextSelector(),
                vol.Optional(
                    CONF_DASHBOARDS, description={"suggested_value": options.get(CONF_DASHBOARDS)}
                ): selector.TextSelector(selector.TextSelectorConfig(multiline=True)),
                vol.Required(CONF_KIOSK, default=options[CONF_KIOSK]): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=KIOSK_MODES, translation_key="kiosk", mode=selector.SelectSelectorMode.DROPDOWN
                    )
                ),
                vol.Required(CONF_ZOOM, default=options[CONF_ZOOM]): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=25, max=200, step=5, unit_of_measurement="%", mode=selector.NumberSelectorMode.BOX
                    )
                ),
                vol.Required(CONF_ROTATE_SECONDS, default=options[CONF_ROTATE_SECONDS]): number(3600, "s"),
                vol.Required(CONF_RETURN_HOME_MINUTES, default=options[CONF_RETURN_HOME_MINUTES]): number(1440, "min"),
                vol.Required(CONF_REOPEN_MINUTES, default=options[CONF_REOPEN_MINUTES]): number(1440, "min"),
                vol.Required(CONF_RELOAD_MINUTES, default=options[CONF_RELOAD_MINUTES]): number(10080, "min"),
                vol.Required(CONF_KEEP_SCREEN_ON, default=options[CONF_KEEP_SCREEN_ON]): selector.BooleanSelector(),
                vol.Required(CONF_LAUNCH_ON_BOOT, default=options[CONF_LAUNCH_ON_BOOT]): selector.BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
