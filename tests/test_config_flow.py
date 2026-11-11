"""Tests for the PleatView config and options flows."""

from __future__ import annotations

from ipaddress import ip_address
from unittest.mock import patch

from custom_components.pleatview.api import PleatViewError
from custom_components.pleatview.const import (
    AUTH_MANAGED,
    AUTH_NONE,
    AUTH_TOKEN,
    CONF_ALLOW_CONTROL,
    CONF_AUTH_MODE,
    CONF_HA_TOKEN,
    CONF_TOKEN,
    CONF_USER_ID,
    DOMAIN,
)
from homeassistant.auth.const import GROUP_ID_READ_ONLY
from homeassistant.config_entries import SOURCE_RECONFIGURE, SOURCE_USER, SOURCE_ZEROCONF
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .conftest import make_entry, setup_entry
from .fake_tv import FakeTV

SETUP = "custom_components.pleatview.async_setup_entry"


def discovery(tv: FakeTV, **properties: str) -> ZeroconfServiceInfo:
    return ZeroconfServiceInfo(
        ip_address=ip_address("127.0.0.1"),
        ip_addresses=[ip_address("127.0.0.1")],
        hostname="living-room-tv.local.",
        name="Living Room TV._pleatview._tcp.local.",
        port=tv.port,
        type="_pleatview._tcp.local.",
        properties={"id": "tv-1", "api": "1", **properties},
    )


async def _pair(hass: HomeAssistant, flow_id: str, tv: FakeTV) -> dict:
    """Enter a wrong PIN, then the right one; returns the result of the right one."""
    result = await hass.config_entries.flow.async_configure(flow_id, {"pin": "000000"})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_pin"}
    return await hass.config_entries.flow.async_configure(flow_id, {"pin": tv.pin})


async def test_user_flow_defaults_to_managed_user(hass: HomeAssistant, fake_tv: FakeTV) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: fake_tv.port}
    )
    assert result["step_id"] == "pair"
    with patch(SETUP, return_value=True):
        result = await _pair(hass, result["flow_id"], fake_tv)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Living Room TV"
    assert result["result"].unique_id == "tv-1"
    assert result["data"][CONF_AUTH_MODE] == AUTH_MANAGED
    assert result["data"][CONF_ALLOW_CONTROL] is True
    assert result["data"][CONF_TOKEN] in fake_tv.tokens


async def test_user_flow_cannot_connect(hass: HomeAssistant, socket_enabled) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: 1}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_zeroconf_flow(hass: HomeAssistant, fake_tv: FakeTV) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=discovery(fake_tv)
    )
    assert result["step_id"] == "pair"
    assert result["description_placeholders"]["name"] == "Living Room TV"
    with patch(SETUP, return_value=True):
        result = await _pair(hass, result["flow_id"], fake_tv)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_AUTH_MODE] == AUTH_MANAGED
    assert result["data"][CONF_ALLOW_CONTROL] is True


async def test_manual_setup_while_discovered(hass: HomeAssistant, fake_tv: FakeTV) -> None:
    discovered = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=discovery(fake_tv)
    )
    assert discovered["step_id"] == "pair"
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: fake_tv.port}
    )
    assert result["step_id"] == "pair"
    with patch(SETUP, return_value=True):
        result = await _pair(hass, result["flow_id"], fake_tv)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)


async def test_zeroconf_updates_moved_tv(hass: HomeAssistant, fake_tv: FakeTV) -> None:
    entry = make_entry(fake_tv)
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_HOST: "10.0.0.99"})
    with patch(SETUP, return_value=True):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_ZEROCONF}, data=discovery(fake_tv)
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == "127.0.0.1"


async def test_zeroconf_ignores_an_impostor(hass: HomeAssistant, fake_tv: FakeTV, impostor: FakeTV) -> None:
    entry = make_entry(fake_tv)
    entry.add_to_hass(hass)
    with patch(SETUP, return_value=True):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_ZEROCONF}, data=discovery(impostor)
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_PORT] == fake_tv.port
    assert impostor.authorizations == []


async def test_zeroconf_keeps_tv_where_it_answers(hass: HomeAssistant, fake_tv: FakeTV) -> None:
    entry = make_entry(fake_tv)
    entry.add_to_hass(hass)
    info = discovery(fake_tv)
    info.ip_address = ip_address("10.0.2.15")
    info.ip_addresses = [info.ip_address]
    with (
        patch(SETUP, return_value=True),
        patch("custom_components.pleatview.api.PleatViewClient.verify", side_effect=PleatViewError("unreachable")),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_ZEROCONF}, data=info)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == "127.0.0.1"


async def test_zeroconf_without_id(hass: HomeAssistant, fake_tv: FakeTV) -> None:
    info = discovery(fake_tv)
    info.properties.pop("id")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_ZEROCONF}, data=info)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "invalid_discovery_info"


async def test_reconfigure_sign_in(
    hass: HomeAssistant, fake_tv: FakeTV, hass_owner_user, hass_access_token: str
) -> None:
    entry = await setup_entry(hass, make_entry(fake_tv, **{CONF_AUTH_MODE: AUTH_MANAGED, CONF_ALLOW_CONTROL: True}))
    first_user = entry.data[CONF_USER_ID]

    async def reconfigure(user_input: dict) -> dict:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
        )
        assert result["step_id"] == "reconfigure"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)
        await hass.async_block_till_done()
        return result

    # Your own token: validated, the dedicated user is removed, the token goes to the TV.
    result = await reconfigure({CONF_AUTH_MODE: AUTH_TOKEN, CONF_HA_TOKEN: "nonsense"})
    assert result["errors"] == {CONF_HA_TOKEN: "invalid_ha_token"}
    result = await reconfigure({CONF_AUTH_MODE: AUTH_TOKEN, CONF_HA_TOKEN: hass_access_token})
    assert result["reason"] == "reconfigure_successful"
    assert await hass.auth.async_get_user(first_user) is None
    assert CONF_USER_ID not in entry.data
    assert fake_tv.config["ha_token"] == hass_access_token

    # Read-only dedicated user.
    await reconfigure({CONF_AUTH_MODE: AUTH_MANAGED, CONF_ALLOW_CONTROL: False})
    user = await hass.auth.async_get_user(entry.data[CONF_USER_ID])
    assert [group.id for group in user.groups] == [GROUP_ID_READ_ONLY]
    assert fake_tv.config["ha_token"] == entry.data[CONF_HA_TOKEN] != hass_access_token

    # Sign in on the TV: user removed and the TV forgets the token.
    await reconfigure({CONF_AUTH_MODE: AUTH_NONE})
    assert await hass.auth.async_get_user(user.id) is None
    assert CONF_HA_TOKEN not in entry.data
    assert fake_tv.config["ha_token"] == ""
    await hass.config_entries.async_unload(entry.entry_id)


async def test_options_flow(hass: HomeAssistant, fake_tv: FakeTV) -> None:
    entry = make_entry(fake_tv)
    entry.add_to_hass(hass)
    with patch(SETUP, return_value=True), patch(
        "custom_components.pleatview.config_flow.async_find_renderers", return_value=[]
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["step_id"] == "init"
        defaults = {
            "kiosk": "full", "zoom": 100, "rotate_seconds": 0, "return_home_minutes": 0,
            "reload_minutes": 0, "keep_screen_on": True, "launch_on_boot": True,
        }
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {**defaults, "ha_url": "ha.local:8123"}
        )
        assert result["errors"] == {"ha_url": "invalid_url"}
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {**defaults, "zoom": 75, "dashboards": "Cams = /dashboard-cams"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["zoom"] == 75
    assert entry.options["dashboards"] == "Cams = /dashboard-cams"


async def test_pairing_approved_on_the_tv(hass: HomeAssistant, fake_tv: FakeTV) -> None:
    """A TV that can ask its viewer: one press of OK, no PIN."""
    fake_tv.info["pairing"] = ["pin", "approve"]
    fake_tv.approve = "allow"
    with patch("custom_components.pleatview.config_flow.APPROVAL_POLL_SECONDS", 0.01), patch(SETUP, return_value=True):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: fake_tv.port})
        assert result["type"] is FlowResultType.SHOW_PROGRESS
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_TOKEN] in fake_tv.tokens


async def test_pairing_refused_on_the_tv_falls_back_to_the_pin(hass: HomeAssistant, fake_tv: FakeTV) -> None:
    fake_tv.info["pairing"] = ["pin", "approve"]
    fake_tv.approve = "deny"
    with patch("custom_components.pleatview.config_flow.APPROVAL_POLL_SECONDS", 0.01), patch(SETUP, return_value=True):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: fake_tv.port})
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
        assert result["step_id"] == "pair"
        assert result["errors"] == {"base": "approval_failed"}
        result = await _pair(hass, result["flow_id"], fake_tv)
    assert result["type"] is FlowResultType.CREATE_ENTRY

