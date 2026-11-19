"""The integration's own services: its screens listed, and one moved to a controller elsewhere."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from custom_components.pleatview.const import DOMAIN
from homeassistant.auth.const import GROUP_ID_ADMIN, GROUP_ID_USER
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError, Unauthorized

from .conftest import make_entry, setup_entry
from .fake_tv import FakeTV

URL = "wss://controller.example.com/api/pleatview/v2"
TOKEN = "one-time-token-0123456789"


async def _admin(hass: HomeAssistant) -> Context:
    user = await hass.auth.async_create_user("Site renderer", group_ids=[GROUP_ID_ADMIN])
    return Context(user_id=user.id)


async def test_screens_are_listed(hass: HomeAssistant, fake_tv: FakeTV) -> None:
    fake_tv.state["features"] = ["protocol_v2", "render_stream"]
    await setup_entry(hass, make_entry(fake_tv))
    result = await hass.services.async_call(DOMAIN, "list_screens", {}, blocking=True, return_response=True,
                                            context=await _admin(hass))
    assert result == {"screens": [{"id": "tv-1", "name": "Living Room TV", "manufacturer": "Google", "model": "Chromecast",
                                   "app_version": "0.1.0", "online": True, "connects_out": True}]}


async def test_a_screen_connects_out_to_a_controller_elsewhere(hass: HomeAssistant, fake_tv: FakeTV) -> None:
    fake_tv.state["features"] = ["protocol_v2"]
    await setup_entry(hass, make_entry(fake_tv))
    fingerprint = "ab" * 32
    with patch("custom_components.pleatview.services.persistent_notification.async_create") as told:
        result = await hass.services.async_call(
            DOMAIN, "enrol_with", {"screen": "tv-1", "url": URL, "token": TOKEN, "fingerprint": fingerprint},
            blocking=True, return_response=True, context=await _admin(hass))
    assert result == {"screen": "tv-1", "enrolled": True}
    assert fake_tv.enrolments == [{"url": URL, "token": TOKEN, "fingerprint": fingerprint}]
    # The site sees that it happened.
    assert "Living Room TV now connects out to the controller at controller.example.com" in told.call_args.args[1]


async def test_only_screens_that_connect_out_are_moved(hass: HomeAssistant, fake_tv: FakeTV) -> None:
    fake_tv.state["features"] = ["render_stream"]
    await setup_entry(hass, make_entry(fake_tv))
    context = await _admin(hass)
    with pytest.raises(ServiceValidationError, match="1.0.19"):
        await hass.services.async_call(DOMAIN, "enrol_with", {"screen": "tv-1", "url": URL, "token": TOKEN},
                                       blocking=True, context=context)
    with pytest.raises(ServiceValidationError, match="No PleatView screen"):
        await hass.services.async_call(DOMAIN, "enrol_with", {"screen": "tv-9", "url": URL, "token": TOKEN},
                                       blocking=True, context=context)
    assert fake_tv.enrolments == []


@pytest.mark.parametrize("url", ["ws://controller.example.com/api/pleatview/v2", "wss://controller.example.com/elsewhere",
                                 "https://controller.example.com/api/pleatview/v2"])
async def test_only_a_controllers_v2_address(hass: HomeAssistant, fake_tv: FakeTV, url: str) -> None:
    fake_tv.state["features"] = ["protocol_v2"]
    await setup_entry(hass, make_entry(fake_tv))
    with pytest.raises(Exception):  # noqa: B017 - the schema refuses it (vol.Invalid)
        await hass.services.async_call(DOMAIN, "enrol_with", {"screen": "tv-1", "url": url, "token": TOKEN},
                                       blocking=True, context=await _admin(hass))
    assert fake_tv.enrolments == []


async def test_what_the_screen_refuses_is_said(hass: HomeAssistant, fake_tv: FakeTV) -> None:
    fake_tv.state["features"] = ["protocol_v2"]
    await setup_entry(hass, make_entry(fake_tv))
    fake_tv.tokens.clear()  # it no longer accepts Home Assistant's pairing
    with pytest.raises(HomeAssistantError, match="didn't take it"):
        await hass.services.async_call(DOMAIN, "enrol_with", {"screen": "tv-1", "url": URL, "token": TOKEN},
                                       blocking=True, context=await _admin(hass))


async def test_only_administrators(hass: HomeAssistant, fake_tv: FakeTV) -> None:
    fake_tv.state["features"] = ["protocol_v2"]
    await setup_entry(hass, make_entry(fake_tv))
    await _admin(hass)  # the first user made is the owner
    user = await hass.auth.async_create_user("Someone", group_ids=[GROUP_ID_USER])
    for service, data in (("list_screens", {}), ("enrol_with", {"screen": "tv-1", "url": URL, "token": TOKEN})):
        with pytest.raises(Unauthorized):
            await hass.services.async_call(DOMAIN, service, data, blocking=True,
                                           return_response=service == "list_screens", context=Context(user_id=user.id))
    assert fake_tv.enrolments == []
