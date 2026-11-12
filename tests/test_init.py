"""Tests for setting up, syncing and removing PleatView entries."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from custom_components.pleatview.const import (
    AUTH_MANAGED,
    CONF_ALLOW_CONTROL,
    CONF_AUTH_MODE,
    CONF_HA_TOKEN,
    CONF_TOKEN,
    CONF_USER_ID,
    DOMAIN,
    ISSUE_OLD_HA,
)
from custom_components.pleatview.coordinator import app_version
from homeassistant.auth.const import GROUP_ID_READ_ONLY
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component

from .conftest import HA_URL, make_entry, setup_entry
from .fake_tv import FakeTV


async def test_setup_pushes_config(hass: HomeAssistant, fake_tv: FakeTV) -> None:
    entry = await setup_entry(hass, make_entry(fake_tv))
    assert entry.state is ConfigEntryState.LOADED
    assert fake_tv.config["ha_url"] == HA_URL
    assert fake_tv.config["dashboards"] == [
        {"title": "Home", "path": "/lovelace/0"},
        {"title": "Cameras", "path": "/dashboard-cams"},
    ]
    assert "ha_token" not in fake_tv.config  # auth mode "none" leaves sign-in to the TV
    assert fake_tv.config["reopen_minutes"] == 10  # back from other apps after 10 quiet minutes
    assert fake_tv.config["rev"]
    assert hass.states.get("media_player.living_room_tv").state == "idle"
    assert hass.states.get("notify.living_room_tv") is not None
    await hass.config_entries.async_unload(entry.entry_id)


async def test_rotation_goes_to_screens_that_turn_their_picture(hass: HomeAssistant, fake_tv: FakeTV) -> None:
    entry = await setup_entry(hass, make_entry(fake_tv, options={"rotation": "90"}))
    assert "rotation" not in fake_tv.config  # older apps refuse settings they don't know
    await fake_tv.push(features=["home_without_ha", "rotation"])
    for _ in range(50):
        if fake_tv.config.get("rotation") == 90:
            break
        await asyncio.sleep(0.05)
    assert fake_tv.config["rotation"] == 90
    await hass.config_entries.async_unload(entry.entry_id)


async def test_old_home_assistant_gets_a_repair(hass: HomeAssistant, fake_tv: FakeTV) -> None:
    with patch("custom_components.pleatview.MAJOR_VERSION", 2025), patch("custom_components.pleatview.MINOR_VERSION", 1):
        entry = await setup_entry(hass, make_entry(fake_tv))
    issue = ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_OLD_HA)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_placeholders["safe_version"] == "2026.7"
    # Up to date, the next setup takes it away again.
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_OLD_HA) is None
    await hass.config_entries.async_unload(entry.entry_id)


async def test_webpage_dashboards_open_directly(hass: HomeAssistant, fake_tv: FakeTV, hass_ws_client) -> None:
    assert await async_setup_component(hass, "lovelace", {})
    entry = await setup_entry(hass, make_entry(fake_tv, options={"dashboards": "Home = /lovelace/0\nWeb = /dashboard-web"}))
    assert fake_tv.config["dashboards"][1] == {"title": "Web", "path": "/dashboard-web"}
    # The web server starts with the first client, so it comes after every setup.
    client = await hass_ws_client(hass)

    async def save_webpage(url: str) -> None:
        await client.send_json_auto_id({"type": "lovelace/config/save", "url_path": "dashboard-web",
                                        "config": {"strategy": {"type": "iframe", "url": url}}})
        assert (await client.receive_json())["success"]
        await hass.async_block_till_done()

    await client.send_json_auto_id({"type": "lovelace/dashboards/create", "url_path": "dashboard-web",
                                    "title": "Web", "mode": "storage"})
    assert (await client.receive_json())["success"]
    await save_webpage("https://grafana.example/d/1")
    # The TV opens the site itself: in Home Assistant's frame it may refuse to load or lose its cookies.
    assert fake_tv.config["dashboards"] == [
        {"title": "Home", "path": "/lovelace/0"},
        {"title": "Web", "path": "https://grafana.example/d/1"},
    ]
    await hass.services.async_call(
        DOMAIN, "show_dashboard", {"entity_id": "media_player.living_room_tv", "path": "/dashboard-web"}, blocking=True
    )
    assert fake_tv.commands[-1] == {"command": "show_dashboard", "path": "https://grafana.example/d/1"}

    # A changed address reaches the TV straight away.
    await save_webpage("https://grafana.example/d/2")
    assert fake_tv.config["dashboards"][1]["path"] == "https://grafana.example/d/2"

    # A page Home Assistant serves itself, such as /local/streams.html, stays in Home
    # Assistant's frame for apps before 1.0.12: they would show the default dashboard instead.
    await fake_tv.wait_for_stream()
    await save_webpage(f"{HA_URL}/local/streams.html?v=a,b")
    assert entry.runtime_data.dashboards[1]["path"] == "/dashboard-web"
    await save_webpage("/local/streams.html?v=a,b")
    assert entry.runtime_data.dashboards[1]["path"] == "/dashboard-web"
    # Once the TV runs 1.0.12, it opens the page directly, without the frontend around it.
    await fake_tv.push(app_version="1.0.12")
    async with asyncio.timeout(5):
        while fake_tv.config["dashboards"][1]["path"] != "/local/streams.html?v=a,b":
            await asyncio.sleep(0.02)

    # A protocol-relative address would be another site: it stays in Home Assistant's frame.
    await save_webpage("//example.com/page")
    assert entry.runtime_data.dashboards[1]["path"] == "/dashboard-web"
    await hass.config_entries.async_unload(entry.entry_id)


def test_app_versions() -> None:
    assert app_version("1.0.12") == (1, 0, 12)
    assert app_version("1.0.12-debug") == (1, 0, 12)
    assert app_version("1.0.9") < (1, 0, 12) < app_version("1.1.0")
    assert app_version(None) == app_version("dev") == (0,)


async def test_managed_user_lifecycle(hass: HomeAssistant, fake_tv: FakeTV, hass_owner_user) -> None:
    # hass_owner_user: a real instance always has an owner, so ours is never promoted to one.
    entry = await setup_entry(hass, make_entry(fake_tv, **{CONF_AUTH_MODE: AUTH_MANAGED, CONF_ALLOW_CONTROL: False}))
    user = await hass.auth.async_get_user(entry.data[CONF_USER_ID])
    assert user is not None
    assert user.local_only
    assert [group.id for group in user.groups] == [GROUP_ID_READ_ONLY]
    token = fake_tv.config["ha_token"]
    assert token == entry.data[CONF_HA_TOKEN]
    assert hass.auth.async_validate_access_token(token) is not None

    # A reload keeps the existing user and token.
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.data[CONF_HA_TOKEN] == token

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.auth.async_get_user(user.id) is None
    assert fake_tv.config["ha_url"] == ""
    assert fake_tv.config["ha_token"] == ""
    assert entry.data[CONF_TOKEN] not in fake_tv.tokens


async def test_rejected_token_starts_reauth(hass: HomeAssistant, fake_tv: FakeTV) -> None:
    entry = make_entry(fake_tv)
    fake_tv.tokens.clear()  # the TV's app data was wiped
    hass.config.internal_url = HA_URL
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1
    assert flows[0]["context"]["source"] == SOURCE_REAUTH

    result = await hass.config_entries.flow.async_configure(flows[0]["flow_id"], {"pin": fake_tv.pin})
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_TOKEN] in fake_tv.tokens


async def test_tv_restart_reapplies_config(hass: HomeAssistant, fake_tv: FakeTV) -> None:
    entry = await setup_entry(hass, make_entry(fake_tv))
    await fake_tv.wait_for_stream()
    rev = fake_tv.config["rev"]

    # The TV comes back with different settings and the stream reconnects.
    fake_tv.config.clear()
    fake_tv.state["config_rev"] = ""
    fake_tv.close_streams()
    async with asyncio.timeout(10):
        while fake_tv.config.get("rev") != rev:
            fake_tv._closing.clear()
            await asyncio.sleep(0.05)
    assert fake_tv.config["ha_url"] == HA_URL
    await hass.config_entries.async_unload(entry.entry_id)


async def test_dashboard_discovery_and_parsing(hass: HomeAssistant) -> None:
    from types import SimpleNamespace

    from custom_components.pleatview.coordinator import discover_dashboards, parse_dashboards

    def panel(component: str, title: str | None, admin: bool = False) -> SimpleNamespace:
        return SimpleNamespace(component_name=component, sidebar_title=title, require_admin=admin)

    hass.data["frontend_panels"] = {
        "lovelace": panel("lovelace", None),
        "home": panel("home", "home"),
        "dashboard-cams": panel("lovelace", "Cameras"),
        "energy": panel("energy", "energy"),
        "config": panel("config", "config", admin=True),
        "admin-only": panel("lovelace", "Secrets", admin=True),
        "profile": panel("profile", None),
    }
    assert discover_dashboards(hass) == [
        {"title": "Home", "path": "/home"},
        {"title": "Overview", "path": "/lovelace"},
        {"title": "Cameras", "path": "/dashboard-cams"},
        {"title": "Energy", "path": "/energy"},
    ]
    assert parse_dashboards("Cams = dashboard-cams\n# note\n\n/lovelace/0\nGrafana = https://g.example/d/1") == [
        {"title": "Cams", "path": "/dashboard-cams"},
        {"title": "/lovelace/0", "path": "/lovelace/0"},
        {"title": "Grafana", "path": "https://g.example/d/1"},
    ]
