"""Tests for dashboards rendered on Home Assistant by the PleatView Renderer."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import patch

from aiohttp import web
import pytest

from custom_components.pleatview.const import AUTH_MANAGED, CONF_AUTH_MODE, CONF_HA_TOKEN
from custom_components.pleatview.render import screen_size
from homeassistant.core import HomeAssistant

from .conftest import HA_URL, make_entry, setup_entry
from .fake_tv import FakeTV

PLAYLIST = "#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1.0,\ns7.ts\n"


class FakeRenderer:
    """The renderer's API (renderer/pleatview_renderer/server.py), recording what it's asked."""

    def __init__(self) -> None:
        self.url = ""
        self.screens: dict[str, dict[str, Any]] = {}
        self.deleted: list[str] = []

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_put("/api/sessions/{key}", self._put)
        app.router.add_delete("/api/sessions/{key}", self._delete)
        app.router.add_get("/hls/{key}/{name}", self._hls)
        app.router.add_get("/api/info", self._info)
        app.router.add_post("/api/pair", self._pair)
        return app

    async def _pair(self, request: web.Request) -> web.Response:
        if (await request.json()).get("pin") != "123456":
            return web.json_response({"error": "invalid_pin"}, status=403)
        return web.json_response({"token": "paired-token"})

    async def _info(self, request: web.Request) -> web.Response:
        return web.json_response(getattr(self, "info", {"version": "test", "encoder": "x264", "sessions": {}}))

    async def _put(self, request: web.Request) -> web.Response:
        self.screens[request.match_info["key"]] = await request.json()
        return web.json_response({"playlist": f"/hls/{request.match_info['key']}/index.m3u8"})

    async def _delete(self, request: web.Request) -> web.Response:
        self.deleted.append(request.match_info["key"])
        return web.Response(status=204)

    async def _hls(self, request: web.Request) -> web.Response:
        if request.match_info["key"] not in self.screens:
            return web.Response(status=404)
        if request.match_info["name"] == "index.m3u8":
            return web.Response(text=PLAYLIST, content_type="application/vnd.apple.mpegurl")
        return web.Response(body=b"\x47" * 188, content_type="video/mp2t")


@pytest.fixture
async def renderer(socket_enabled) -> AsyncGenerator[FakeRenderer]:
    fake = FakeRenderer()
    runner = web.AppRunner(fake.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    fake.url = f"http://127.0.0.1:{runner.addresses[0][1]}"
    yield fake
    await runner.cleanup()


async def _until(check, timeout: float = 5) -> None:
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(0.02)


async def test_rendered_dashboards(hass: HomeAssistant, fake_tv: FakeTV, renderer: FakeRenderer, hass_client_no_auth) -> None:
    entry = make_entry(
        fake_tv,
        options={"dashboards": "Home = /lovelace/0", "render_url": renderer.url + "/", "zoom": 75, "kiosk": "sidebar"},
        **{CONF_AUTH_MODE: AUTH_MANAGED, CONF_HA_TOKEN: "tv-token"},
    )
    entry = await setup_entry(hass, entry)
    device = entry.runtime_data
    # Apps that don't play rendered dashboards don't get them (and refuse settings they don't know).
    await fake_tv.wait_for_stream()
    await fake_tv.push(app_version="1.0.14")
    await asyncio.sleep(0.2)
    assert "render_stream" not in fake_tv.config
    assert renderer.screens == {}

    await fake_tv.push(features=["render_stream"], screen={"width": 1920, "height": 1080, "density": 2.0})
    await _until(lambda: "render_stream" in fake_tv.config and entry.entry_id in renderer.screens)
    stream = fake_tv.config["render_stream"]
    assert stream == f"{HA_URL}{device.renderer.stream_path}"
    assert stream.startswith(f"{HA_URL}/api/pleatview/render/{entry.entry_id}/")
    assert renderer.screens[entry.entry_id] == {
        "url": "http://ha.test:8123/lovelace/0", "width": 1920, "height": 1080, "scale": 1.5,
        "kiosk": "sidebar", "ha_url": HA_URL, "token": entry.data[CONF_HA_TOKEN],
    }
    assert hass.auth.async_validate_access_token(entry.data[CONF_HA_TOKEN]) is not None

    # The renderer follows the page the TV wants.
    await fake_tv.push(url="http://ha.test:8123/dashboard-cams")
    await _until(lambda: renderer.screens[entry.entry_id]["url"] == "http://ha.test:8123/dashboard-cams")

    # Home Assistant passes the stream on, to whoever has the stream's secret address.
    client = await hass_client_no_auth()
    path = stream.removeprefix(HA_URL)
    response = await client.get(path)
    assert response.status == 200
    assert await response.text() == PLAYLIST
    response = await client.get(path.replace("index.m3u8", "s7.ts"))
    assert response.status == 200
    assert response.content_type == "video/mp2t"
    assert (await client.get(path.replace(device.renderer.secret, "guess"))).status == 404
    assert (await client.get(path.replace("index.m3u8", "..%2Fsecrets.yaml"))).status in (400, 404)
    assert (await client.get(path.replace("index.m3u8", "secrets.yaml"))).status == 404
    assert (await client.get(f"/api/pleatview/render/other/{device.renderer.secret}/index.m3u8")).status == 404

    await hass.config_entries.async_unload(entry.entry_id)
    assert renderer.deleted == [entry.entry_id]


async def test_without_a_renderer_the_tv_renders(hass: HomeAssistant, fake_tv: FakeTV) -> None:
    entry = await setup_entry(hass, make_entry(fake_tv))
    await fake_tv.wait_for_stream()
    await fake_tv.push(features=["render_stream"])
    await asyncio.sleep(0.2)
    assert entry.runtime_data.renderer is None
    assert "render_stream" not in fake_tv.config
    await hass.config_entries.async_unload(entry.entry_id)


def test_screen_sizes() -> None:
    assert screen_size({}) == (1920, 1080, 2.0)
    assert screen_size({"screen": {"width": 1280, "height": 720, "density": 1.5}}) == (1280, 720, 1.5)
    # A 4K TV is rendered at 1080p, at half its density so dashboards keep their size.
    assert screen_size({"screen": {"width": 3840, "height": 2160, "density": 4.0}}) == (1920, 1080, 2.0)
    # A phone held upright stays upright, at most 1920 pixels tall.
    assert screen_size({"screen": {"width": 1080, "height": 2340, "density": 3.0}}) == (886, 1920, 3.0 * 1920 / 2340)
    assert screen_size({"screen": {"width": "big"}}) == (1920, 1080, 2.0)


async def test_renderers_found_for_the_options(hass: HomeAssistant, fake_tv: FakeTV, renderer: FakeRenderer) -> None:
    """The TV's options list the renderers that answer, and keep one typed in."""
    from custom_components.pleatview import render

    renderer.info = {"version": "1.0.16", "encoder": "vaapi", "sessions": {}}
    with patch.object(render, "CANDIDATES", (renderer.url, "http://127.0.0.1:9")):
        found = await render.async_find_renderers(hass, ("http://127.0.0.1:9/",))
    assert found == [{"value": renderer.url, "label": f"{renderer.url} · PleatView Renderer 1.0.16 (vaapi)"}]


async def test_paired_renderers_by_name_and_automatic(hass: HomeAssistant, renderer: FakeRenderer) -> None:
    """A paired renderer is found by its entry; Automatic picks the least busy one and keeps it."""
    from custom_components.pleatview import renderers
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    host, port = renderer.url.removeprefix("http://").split(":")
    busy = MockConfigEntry(domain="pleatview", title="Rack A", unique_id="r-a", data={
        "kind": "renderer", "host": host, "port": int(port), "scheme": "http", "token": "tok-a"})
    busy.add_to_hass(hass)
    renderer.info = {"version": "1.0.17", "encoder": "vaapi", "max_screens": 4, "sessions": {"x": {}}, "load": {"cpu": 0.2}}
    assert await hass.config_entries.async_setup(busy.entry_id)
    await hass.async_block_till_done()
    target = renderers.resolve(hass, f"renderer:{busy.entry_id}")
    assert (target.base_url, target.token) == (renderer.url, "tok-a")
    assert target.kwargs() == {"headers": {"Authorization": "Bearer tok-a"}}
    assert renderers.resolve(hass, "auto").entry_id == busy.entry_id
    assert renderers.resolve(hass, "renderer:missing") is None
    assert renderers.resolve(hass, "http://10.0.0.9:8780/").base_url == "http://10.0.0.9:8780"
    labels = [c["label"] for c in renderers.choices(hass)]
    assert labels == ["Rack A (1/4 screens)"]
    state = hass.states.get("sensor.rack_a_screens")
    assert state is not None and state.state == "1"


async def test_a_renderer_pairs_from_its_announcement(hass: HomeAssistant, renderer: FakeRenderer) -> None:
    """Discovered over mDNS, named, paired with the PIN from its log: an entry of its own."""
    from ipaddress import ip_address

    from homeassistant.config_entries import SOURCE_ZEROCONF
    from homeassistant.data_entry_flow import FlowResultType
    from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

    host, port = renderer.url.removeprefix("http://").split(":")
    info = ZeroconfServiceInfo(
        ip_address=ip_address(host), ip_addresses=[ip_address(host)], port=int(port), hostname="rack.local.",
        type="_pleatview-renderer._tcp.local.", name="Rack A._pleatview-renderer._tcp.local.",
        properties={"id": "r-1", "name": "Rack A", "version": "1.0.17", "scheme": "http"})
    result = await hass.config_entries.flow.async_init("pleatview", context={"source": SOURCE_ZEROCONF}, data=info)
    assert result["step_id"] == "renderer"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"name": "Rack A", "pin": "000000"})
    assert result["errors"] == {"base": "invalid_pin"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"name": "Rack A", "pin": "123456"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Rack A"
    assert result["data"]["kind"] == "renderer" and result["data"]["token"] == "paired-token"

