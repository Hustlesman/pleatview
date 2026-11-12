"""Config flow for PleatView."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import suppress
import logging
from typing import Any

import aiohttp
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

from . import renderers
from .api import InvalidAuth, InvalidPin, NotFound, PleatViewClient, PleatViewError, TooManyAttempts
from .render import async_find_renderers
from .const import (
    AUTH_MANAGED,
    AUTH_NONE,
    AUTH_TOKEN,
    CONF_ALLOW_CONTROL,
    CONF_AUTH_MODE,
    CONF_DASHBOARDS,
    CONF_FINGERPRINT,
    CONF_HA_TOKEN,
    CONF_HA_URL,
    CONF_HOME_DASHBOARD,
    CONF_KEEP_SCREEN_ON,
    CONF_KIND,
    CONF_KIOSK,
    CONF_LAUNCH_ON_BOOT,
    CONF_RELOAD_MINUTES,
    CONF_RENDER_URL,
    CONF_REOPEN_MINUTES,
    CONF_RETURN_HOME_MINUTES,
    CONF_ROTATE_SECONDS,
    CONF_SCHEME,
    CONF_TOKEN,
    CONF_USER_ID,
    CONF_ZOOM,
    DEFAULT_OPTIONS,
    DEFAULT_PORT,
    DOMAIN,
    KIND_RENDERER,
    KIOSK_MODES,
    RENDERER_PREFIX,
    RENDERER_SERVICE,
    RENDER_AUTO,
)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
    }
)
PIN_SCHEMA = vol.Schema({vol.Required("pin"): str})


def split_address(text: str, port: int) -> tuple[str, int]:
    """The host and port in an address as a screen shows it: '192.168.1.20' (the port field
    then counts), '192.168.1.20:8765' (PleatView Light) or 'http://192.168.1.20:8765/' (the
    phone page's address)."""
    text = text.strip()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0]
    host, sep, tail = text.rpartition(":")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]  # [IPv6]:port
    elif text.startswith("[") and text.endswith("]"):
        return text[1:-1], port
    if sep and tail.isdigit() and 0 < int(tail) < 65536 and host and (":" not in host or text.startswith("[")):
        return host, int(tail)
    return text, port
APPROVAL_POLL_SECONDS = 1

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
        self._scheme = "http"
        self._approve_task: asyncio.Task[str | None] | None = None
        self._approval_tried = False

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return PleatViewOptionsFlow()

    @classmethod
    @callback
    def async_supports_options_flow(cls, config_entry: ConfigEntry) -> bool:
        """TVs have options; a renderer has only its name, which is the entry's own."""
        return not renderers.is_renderer(config_entry)

    def _client(self) -> PleatViewClient:
        return PleatViewClient(async_get_clientsession(self.hass), self._host, self._port, self._token)

    @property
    def _name(self) -> str:
        return self._info.get("name") or "PleatView"

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Add a TV by address."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._host, self._port = split_address(user_input[CONF_HOST], user_input[CONF_PORT])
            try:
                self._info = await self._client().get_info()
            except PleatViewError:
                errors["base"] = "cannot_connect"
            else:
                if "encoder" in self._info:  # a PleatView Renderer, not a TV
                    return await self._async_start_renderer(self._info.get("scheme") or "http")
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
        """Handle a TV advertising _pleatview._tcp, or a renderer advertising _pleatview-renderer._tcp."""
        device_id = discovery_info.properties.get("id")
        if not device_id:
            return self.async_abort(reason="invalid_discovery_info")
        self._host = discovery_info.host
        self._port = discovery_info.port or DEFAULT_PORT
        if discovery_info.type == RENDERER_SERVICE:
            self._info = dict(discovery_info.properties)
            return await self._async_start_renderer(discovery_info.properties.get("scheme") or "http")
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

    async def async_step_approve(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask the TV to allow Home Assistant: one press of OK there, no PIN to type."""
        if self._approve_task is None:
            self._approve_task = self.hass.async_create_task(self._async_wait_for_approval())
        if not self._approve_task.done():
            return self.async_show_progress(
                step_id="approve", progress_action="approve", progress_task=self._approve_task,
                description_placeholders={"name": self._name},
            )
        token = self._approve_task.result()
        self._approve_task = None
        self._approval_tried = True
        if token:
            self._token = token
            return self.async_show_progress_done(next_step_id="paired")
        return self.async_show_progress_done(next_step_id="pair")

    async def _async_wait_for_approval(self) -> str | None:
        """The token once someone at the TV presses OK; None when refused, too late or unsupported."""
        client = self._client()
        try:
            request = await client.request_approval("Home Assistant")
        except PleatViewError:
            return None
        request_id = str(request.get("request") or "")
        if not request_id:
            return None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + min(int(request.get("expires_in") or 120), 180)
        while loop.time() < deadline:
            await asyncio.sleep(APPROVAL_POLL_SECONDS)
            try:
                state = await client.approval_state(request_id)
            except (NotFound, InvalidAuth):
                return None
            except PleatViewError:
                continue
            if state.get("state") == "approved" and state.get("token"):
                return str(state["token"])
            if state.get("state") in ("denied", "expired"):
                return None
        return None

    async def async_step_paired(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Approved on the TV."""
        return self._async_paired()

    async def async_step_pair(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the PIN shown on the TV (after offering to approve on the TV, where it can)."""
        errors: dict[str, str] = {}
        if user_input is None and not self._approval_tried and "approve" in (self._info.get("pairing") or []):
            return await self.async_step_approve()
        if user_input is None and self._approval_tried:
            errors["base"] = "approval_failed"
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
                return self._async_paired()
        return self.async_show_form(
            step_id="pair",
            data_schema=PIN_SCHEMA,
            description_placeholders={"name": self._name},
            errors=errors,
        )

    # ---- PleatView Renderers ---------------------------------------------------------------

    async def _async_start_renderer(self, scheme: str) -> ConfigFlowResult:
        renderer_id = self._info.get("id")
        if not renderer_id:
            return self.async_abort(reason="invalid_discovery_info")
        self._scheme = "https" if scheme == "https" else "http"
        await self.async_set_unique_id(renderer_id, raise_on_progress=False)
        # A renderer at a new address only moves with its token: the PIN is asked again.
        self._abort_if_unique_id_configured()
        self.context["title_placeholders"] = {"name": self._info.get("name") or "PleatView Renderer"}
        return await self.async_step_renderer()

    async def async_step_renderer(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Name the renderer and enter the PIN from its log."""
        errors: dict[str, str] = {}
        url = f"{self._scheme}://{self._host}:{self._port}"
        if user_input is not None:
            fingerprint = None
            try:
                if self._scheme == "https":
                    fingerprint = await renderers.async_fetch_certificate(self.hass, url)
                token = await renderers.async_pair(self.hass, url, user_input["pin"].strip(), fingerprint)
            except ValueError as err:
                errors["base"] = str(err)
            except (aiohttp.ClientError, TimeoutError):
                errors["base"] = "cannot_connect"
            else:
                data = {CONF_KIND: KIND_RENDERER, CONF_HOST: self._host, CONF_PORT: self._port,
                        CONF_SCHEME: self._scheme, CONF_TOKEN: token, CONF_FINGERPRINT: fingerprint}
                if self.source == SOURCE_REAUTH:
                    return self.async_update_reload_and_abort(self._get_reauth_entry(), data_updates=data)
                return self.async_create_entry(title=user_input["name"].strip() or "PleatView Renderer", data=data)
        schema = vol.Schema({
            vol.Required("name", default=self._info.get("name") or "PleatView Renderer"): str,
            vol.Required("pin"): str,
        })
        return self.async_show_form(step_id="renderer", data_schema=schema, errors=errors,
                                    description_placeholders={"address": url})

    def _async_paired(self) -> ConfigFlowResult:
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
        """The TV (or renderer) forgot us (data cleared or pairing reset): pair again."""
        self._host = entry_data[CONF_HOST]
        self._port = entry_data[CONF_PORT]
        if entry_data.get(CONF_KIND) == KIND_RENDERER:
            self._scheme = entry_data.get(CONF_SCHEME) or "http"
            self._info = {"name": self._get_reauth_entry().title}
            return await self.async_step_renderer()
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
            for key in (CONF_HA_URL, CONF_RENDER_URL):
                url = (user_input.get(key) or "").strip()
                if key == CONF_RENDER_URL and (url == RENDER_AUTO or url.startswith(RENDERER_PREFIX)):
                    continue
                if url and not url.startswith(("http://", "https://")):
                    errors[key] = "invalid_url"
            if not errors:
                return self.async_create_entry(data=user_input)

        options = {**DEFAULT_OPTIONS, **self.config_entry.options, **(user_input or {})}
        current = (options.get(CONF_RENDER_URL) or "").strip().rstrip("/")
        # Paired renderers by name, Automatic when there are any, then renderers that answer
        # without pairing (the Home Assistant OS app, docker run) and any address in use.
        paired = renderers.choices(self.hass)
        paired_urls = {renderers.base_url(e.data) for e in renderers.renderer_entries(self.hass)}
        legacy = [r for r in await async_find_renderers(self.hass, () if current.startswith(("renderer:", "auto")) else (current,))
                  if r["value"] not in paired_urls]
        render_choices = ([{"value": RENDER_AUTO, "label": RENDER_AUTO}] if paired else []) + paired + legacy
        if current and current not in {r["value"] for r in render_choices}:
            render_choices.append({"value": current, "label": current})

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
                # The renderers Home Assistant found, or another address typed in.
                vol.Optional(CONF_RENDER_URL, description={"suggested_value": current or None}): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=render_choices, custom_value=True, mode=selector.SelectSelectorMode.DROPDOWN,
                        translation_key="render",
                    )
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
