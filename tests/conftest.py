"""Fixtures for PleatView tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator

from aiohttp import web
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pleatview.const import AUTH_NONE, CONF_AUTH_MODE, CONF_TOKEN, DOMAIN
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant

from .fake_tv import FakeTV

HA_URL = "http://ha.test:8123"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load custom_components/ in every test."""
    return


async def _serve(tv: FakeTV) -> web.AppRunner:
    runner = web.AppRunner(tv.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    tv.port = runner.addresses[0][1]
    return runner


@pytest.fixture
async def fake_tv(socket_enabled) -> AsyncGenerator[FakeTV]:
    """A fake TV listening on localhost."""
    tv = FakeTV()
    runner = await _serve(tv)
    yield tv
    tv.close_streams()
    await runner.cleanup()


@pytest.fixture
async def impostor(socket_enabled) -> AsyncGenerator[FakeTV]:
    """Another device on the network announcing the fake TV's id, which isn't secret."""
    tv = FakeTV()
    runner = await _serve(tv)
    yield tv
    tv.close_streams()
    await runner.cleanup()


def make_entry(tv: FakeTV, options: dict | None = None, **data) -> MockConfigEntry:
    """A config entry already paired with the fake TV."""
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=tv.info["id"],
        title=tv.info["name"],
        data={
            CONF_HOST: "127.0.0.1",
            CONF_PORT: tv.port,
            CONF_TOKEN: tv.issue_token(),
            CONF_AUTH_MODE: AUTH_NONE,
            **data,
        },
        options=options if options is not None else {"dashboards": "Home = /lovelace/0\nCameras = /dashboard-cams"},
    )


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> MockConfigEntry:
    """Add and set up an entry."""
    hass.config.internal_url = HA_URL
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry
