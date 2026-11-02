"""Tests for the battery and charging sensors."""

from __future__ import annotations

import asyncio

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .conftest import make_entry, setup_entry
from .fake_tv import FakeTV

BATTERY = "sensor.living_room_tv_battery"
CHARGING = "binary_sensor.living_room_tv_charging"


async def _wait_for(predicate, timeout: float = 5) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


async def test_a_tv_has_no_battery_sensors(hass: HomeAssistant, fake_tv: FakeTV) -> None:
    await setup_entry(hass, make_entry(fake_tv))
    assert hass.states.get(BATTERY) is None
    assert hass.states.get(CHARGING) is None


async def test_a_tablet_reports_its_battery(hass: HomeAssistant, fake_tv: FakeTV) -> None:
    fake_tv.state["battery"] = {"level": 87, "charging": True}
    await setup_entry(hass, make_entry(fake_tv))
    await fake_tv.wait_for_stream()
    assert hass.states.get(BATTERY).state == "87"
    assert hass.states.get(BATTERY).attributes["unit_of_measurement"] == "%"
    assert hass.states.get(CHARGING).state == STATE_ON
    registry = er.async_get(hass)
    assert registry.async_get(BATTERY).entity_category == "diagnostic"

    await fake_tv.push(battery={"level": 20, "charging": False})
    await _wait_for(lambda: hass.states.get(BATTERY).state == "20")
    assert hass.states.get(CHARGING).state == STATE_OFF

    await fake_tv.push(battery=None)
    await _wait_for(lambda: hass.states.get(BATTERY).state == STATE_UNAVAILABLE)
    assert hass.states.get(CHARGING).state == STATE_UNAVAILABLE
