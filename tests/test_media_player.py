"""Tests for the PleatView media player, notify entity and services."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from custom_components.pleatview.const import DASHBOARDS_ROOT, DOMAIN, HLS_MIME
from homeassistant.components import camera  # noqa: F401  (imported so it can be patched)
from homeassistant.components.media_player import (
    ATTR_INPUT_SOURCE,
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    ATTR_MEDIA_EXTRA,
    ATTR_MEDIA_VOLUME_LEVEL,
    DOMAIN as MP_DOMAIN,
    SERVICE_PLAY_MEDIA,
    SERVICE_SELECT_SOURCE,
)
from homeassistant.components.media_source import PlayMedia
from homeassistant.const import ATTR_ENTITY_ID, SERVICE_MEDIA_STOP, SERVICE_VOLUME_SET
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
import pytest

from .conftest import HA_URL, make_entry, setup_entry
from .fake_tv import FakeTV

ENTITY = "media_player.living_room_tv"


async def _wait_for(predicate, timeout: float = 5) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


async def test_state_follows_the_tv(hass: HomeAssistant, fake_tv: FakeTV) -> None:
    entry = await setup_entry(hass, make_entry(fake_tv))
    await fake_tv.wait_for_stream()
    state = hass.states.get(ENTITY)
    assert state.state == "idle"
    assert state.attributes["source"] == "Home"
    assert state.attributes["source_list"] == ["Home", "Cameras"]

    await fake_tv.push(
        dashboard="/dashboard-cams/1",
        media={
            "state": "playing", "url": "http://x/clip.mp4", "mime_type": "video/mp4", "title": "Clip",
            "artist": None, "image_url": None, "mode": "fullscreen", "position": 3.0, "duration": 10.0,
            "updated_at": 1_700_000_000.0,
        },
    )
    await _wait_for(lambda: hass.states.get(ENTITY).state == "playing")
    state = hass.states.get(ENTITY)
    assert state.attributes["media_title"] == "Clip"
    assert state.attributes["media_duration"] == 10.0
    assert state.attributes["source"] == "Cameras"
    # Signed camera and media links stay out of the history.
    assert {"media_content_id", "url"} <= state.state_info["unrecorded_attributes"]

    # Behind another app it's off, unless a camera shows in a corner over that app.
    await fake_tv.push(media=None, foreground=False)
    await _wait_for(lambda: hass.states.get(ENTITY).state == "off")
    await fake_tv.push(overlay={"kind": "video", "url": "http://x/cam.m3u8", "mode": "pip"})
    await _wait_for(lambda: hass.states.get(ENTITY).state == "playing")
    assert hass.states.get(ENTITY).attributes["media_content_type"] == "video"
    await fake_tv.push(overlay=None, foreground=True)

    await fake_tv.push(media=None, power="standby")
    await _wait_for(lambda: hass.states.get(ENTITY).state == "off")

    fake_tv.close_streams()  # TV goes away
    await _wait_for(lambda: hass.states.get(ENTITY).state == "unavailable")
    await hass.config_entries.async_unload(entry.entry_id)


async def test_sign_ins(hass: HomeAssistant, fake_tv: FakeTV) -> None:
    entry = await setup_entry(hass, make_entry(fake_tv))
    await fake_tv.wait_for_stream()
    assert hass.states.get(ENTITY).attributes["sign_in_needed"] is None

    # The TV reports a dashboard waiting for a username and password; automations can react.
    await fake_tv.push(sign_in={"kind": "form", "title": "Grafana", "site": "http://grafana.lan:3000", "failed": False})
    await _wait_for(lambda: hass.states.get(ENTITY).attributes["sign_in_needed"] == "Grafana")

    target = {ATTR_ENTITY_ID: ENTITY}
    await hass.services.async_call(DOMAIN, "forget_sign_ins", {**target, "site": "http://grafana.lan:3000"}, blocking=True)
    await hass.services.async_call(DOMAIN, "forget_sign_ins", target, blocking=True)
    assert fake_tv.commands[-2:] == [
        {"command": "forget_sign_ins", "site": "http://grafana.lan:3000"},
        {"command": "forget_sign_ins"},
    ]
    await hass.config_entries.async_unload(entry.entry_id)


async def test_media_player_services(hass: HomeAssistant, fake_tv: FakeTV) -> None:
    entry = await setup_entry(hass, make_entry(fake_tv))
    target = {ATTR_ENTITY_ID: ENTITY}

    await hass.services.async_call(MP_DOMAIN, SERVICE_SELECT_SOURCE, {**target, ATTR_INPUT_SOURCE: "Cameras"}, blocking=True)
    await hass.services.async_call(MP_DOMAIN, SERVICE_VOLUME_SET, {**target, ATTR_MEDIA_VOLUME_LEVEL: 0.4}, blocking=True)
    await hass.services.async_call(
        MP_DOMAIN,
        SERVICE_PLAY_MEDIA,
        {**target, ATTR_MEDIA_CONTENT_TYPE: "dashboard", ATTR_MEDIA_CONTENT_ID: "/lovelace/energy", ATTR_MEDIA_EXTRA: {"duration": 30}},
        blocking=True,
    )
    await hass.services.async_call(
        MP_DOMAIN,
        SERVICE_PLAY_MEDIA,
        {**target, ATTR_MEDIA_CONTENT_TYPE: "music", ATTR_MEDIA_CONTENT_ID: "https://radio.example/stream", ATTR_MEDIA_EXTRA: {"title": "Radio"}},
        blocking=True,
    )
    await hass.services.async_call(MP_DOMAIN, SERVICE_MEDIA_STOP, target, blocking=True)
    assert fake_tv.commands == [
        {"command": "show_dashboard", "path": "/dashboard-cams"},
        {"command": "set_volume", "level": 0.4},
        {"command": "show_dashboard", "path": "/lovelace/energy", "duration": 30},
        {"command": "play_media", "url": "https://radio.example/stream", "mime_type": "audio/*", "title": "Radio"},
        {"command": "stop"},
    ]

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(MP_DOMAIN, SERVICE_SELECT_SOURCE, {**target, ATTR_INPUT_SOURCE: "Nope"}, blocking=True)
    await hass.config_entries.async_unload(entry.entry_id)


async def test_play_media_source_is_resolved_and_signed(hass: HomeAssistant, fake_tv: FakeTV) -> None:
    entry = await setup_entry(hass, make_entry(fake_tv))
    with patch(
        "homeassistant.components.media_source.async_resolve_media",
        return_value=PlayMedia(url="/media/local/song.mp3", mime_type="audio/mpeg"),
    ):
        await hass.services.async_call(
            MP_DOMAIN,
            SERVICE_PLAY_MEDIA,
            {ATTR_ENTITY_ID: ENTITY, ATTR_MEDIA_CONTENT_TYPE: "music", ATTR_MEDIA_CONTENT_ID: "media-source://media_source/local/song.mp3", "announce": True},
            blocking=True,
        )
    command = fake_tv.commands[-1]
    assert command["command"] == "play_media"
    assert command["url"].startswith(f"{HA_URL}/media/local/song.mp3?authSig=")
    assert command["mime_type"] == "audio/mpeg"
    assert command["announce"] is True
    await hass.config_entries.async_unload(entry.entry_id)


async def test_media_uses_the_tvs_home_assistant_url(hass: HomeAssistant, fake_tv: FakeTV) -> None:
    # A TV that reaches Home Assistant at another address gets media there too, not only dashboards.
    tv_url = "http://192.168.1.10:8123/"
    entry = await setup_entry(hass, make_entry(fake_tv, options={"ha_url": tv_url}))
    with patch(
        "homeassistant.components.media_source.async_resolve_media",
        return_value=PlayMedia(url="/api/tts_proxy/abc.mp3", mime_type="audio/mpeg"),
    ):
        await hass.services.async_call(
            MP_DOMAIN,
            SERVICE_PLAY_MEDIA,
            {ATTR_ENTITY_ID: ENTITY, ATTR_MEDIA_CONTENT_TYPE: "music", ATTR_MEDIA_CONTENT_ID: "media-source://tts/x"},
            blocking=True,
        )
    target = {ATTR_ENTITY_ID: ENTITY}
    with patch("homeassistant.components.camera.async_request_stream", return_value="/api/hls/abc/master_playlist.m3u8"):
        await hass.services.async_call(DOMAIN, "show_camera", {**target, "camera": "camera.door"}, blocking=True)
    await hass.services.async_call(DOMAIN, "notify", {**target, "message": "Ding dong", "camera": "camera.door"}, blocking=True)
    await hass.services.async_call(
        MP_DOMAIN,
        SERVICE_PLAY_MEDIA,
        {ATTR_ENTITY_ID: ENTITY, ATTR_MEDIA_CONTENT_TYPE: "music", ATTR_MEDIA_CONTENT_ID: "https://radio.example/stream"},
        blocking=True,
    )

    tts, hls, notify, radio = fake_tv.commands[-4:]
    assert tts["url"] == "http://192.168.1.10:8123/api/tts_proxy/abc.mp3"  # Home Assistant doesn't sign these
    assert hls["url"].startswith("http://192.168.1.10:8123/api/hls/abc/master_playlist.m3u8")
    assert notify["image_url"].startswith("http://192.168.1.10:8123/api/camera_proxy/camera.door?authSig=")
    assert radio["url"] == "https://radio.example/stream"
    await hass.config_entries.async_unload(entry.entry_id)


async def test_pleatview_services(hass: HomeAssistant, fake_tv: FakeTV) -> None:
    entry = await setup_entry(hass, make_entry(fake_tv))
    target = {ATTR_ENTITY_ID: ENTITY}

    await hass.services.async_call(DOMAIN, "show_dashboard", {**target, "path": "/lovelace/cams", "duration": 60}, blocking=True)
    await hass.services.async_call(DOMAIN, "show_url", {**target, "url": "https://example.org", "mode": "pip", "size": 40}, blocking=True)
    with patch("homeassistant.components.camera.async_request_stream", return_value="/api/hls/abc/master_playlist.m3u8"):
        await hass.services.async_call(DOMAIN, "show_camera", {**target, "camera": "camera.door", "duration": 30}, blocking=True)
    with patch("homeassistant.components.camera.async_request_stream", side_effect=HomeAssistantError("no stream")):
        await hass.services.async_call(DOMAIN, "show_camera", {**target, "camera": "camera.door", "mode": "fullscreen"}, blocking=True)
    # Home Assistant's stream, caught shutting down its idle HLS output: one more try.
    with patch("homeassistant.components.camera.async_request_stream",
               side_effect=[ValueError("Stream is not configured for format 'hls'"), "/api/hls/def/master_playlist.m3u8"]), \
            patch("custom_components.pleatview.media_player.asyncio.sleep"):
        await hass.services.async_call(DOMAIN, "show_camera", {**target, "camera": "camera.door"}, blocking=True)
    with patch("homeassistant.components.camera.async_request_stream", side_effect=ValueError("not configured")), \
            patch("custom_components.pleatview.media_player.asyncio.sleep"):
        await hass.services.async_call(DOMAIN, "show_camera", {**target, "camera": "camera.door"}, blocking=True)
    await hass.services.async_call(DOMAIN, "notify", {**target, "message": "Ding dong", "title": "Door", "camera": "camera.door"}, blocking=True)
    await hass.services.async_call(DOMAIN, "home", target, blocking=True)
    await hass.services.async_call("notify", "send_message", {ATTR_ENTITY_ID: "notify.living_room_tv", "message": "Hi", "title": "PleatView"}, blocking=True)

    show_dashboard, show_url, hls, mjpeg, retried, gave_up, notify, home, send_message = fake_tv.commands
    assert show_dashboard == {"command": "show_dashboard", "path": "/lovelace/cams", "duration": 60}
    assert show_url == {"command": "show_url", "url": "https://example.org", "mode": "pip", "size": 40}
    assert hls["command"] == "play_media"
    assert hls["mime_type"] == HLS_MIME
    assert hls["mode"] == "pip"
    assert hls["duration"] == 30
    assert hls["url"].startswith(f"{HA_URL}/api/hls/abc/master_playlist.m3u8")
    # The picture stream, for a TV that can't play the video beside another app's.
    assert hls["fallback_url"].startswith(f"{HA_URL}/api/camera_proxy_stream/camera.door?authSig=")
    assert mjpeg["command"] == "show_image"
    assert mjpeg["url"].startswith(f"{HA_URL}/api/camera_proxy_stream/camera.door?authSig=")
    assert retried["url"].startswith(f"{HA_URL}/api/hls/def/master_playlist.m3u8")
    assert gave_up["command"] == "show_image"
    assert notify["image_url"].startswith(f"{HA_URL}/api/camera_proxy/camera.door?authSig=")
    assert notify["title"] == "Door"
    assert home == {"command": "home"}
    assert send_message == {"command": "notify", "message": "Hi", "title": "PleatView"}

    with pytest.raises(HomeAssistantError, match="rejected"):
        await entry.runtime_data.async_command("explode")
    await hass.config_entries.async_unload(entry.entry_id)


async def test_browse_media_offers_dashboards(hass: HomeAssistant, fake_tv: FakeTV, hass_ws_client) -> None:
    entry = await setup_entry(hass, make_entry(fake_tv))
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "media_player/browse_media", "entity_id": ENTITY})
    response = await client.receive_json()
    assert response["success"], response
    assert response["result"]["children"][0]["media_content_id"] == DASHBOARDS_ROOT

    await client.send_json(
        {"id": 2, "type": "media_player/browse_media", "entity_id": ENTITY,
         "media_content_type": "dashboards", "media_content_id": DASHBOARDS_ROOT}
    )
    response = await client.receive_json()
    assert response["success"], response
    assert [(c["title"], c["media_content_id"], c["media_content_type"]) for c in response["result"]["children"]] == [
        ("Home", "/lovelace/0", "dashboard"),
        ("Cameras", "/dashboard-cams", "dashboard"),
    ]
    await hass.config_entries.async_unload(entry.entry_id)
