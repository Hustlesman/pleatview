"""Tests for the wall of YouTube streams that Home Assistant serves."""

from __future__ import annotations

from custom_components.pleatview.wall import MAX_STREAMS, video_ids
from homeassistant.core import HomeAssistant

from .conftest import make_entry, setup_entry
from .fake_tv import FakeTV


async def test_the_wall_page(hass: HomeAssistant, fake_tv: FakeTV, hass_client_no_auth) -> None:
    entry = await setup_entry(hass, make_entry(fake_tv))
    client = await hass_client_no_auth()

    # No sign-in: the TV, the renderer and a browser all load it as a plain page.
    response = await client.get("/api/pleatview/wall?v=rFZHOHl-L8A,ROboUHfPUrU,XsOU8JnEpNM,VGnFLdQW39A,ZZZZZZZZZZZ")
    assert response.status == 200
    assert response.content_type == "text/html"
    page = await response.text()
    assert page.count("<iframe ") == 5
    assert "grid-template-columns:repeat(3,1fr)" in page
    assert 'src="https://www.youtube-nocookie.com/embed/rFZHOHl-L8A?autoplay=1&amp;mute=1' in page
    assert page.count('referrerpolicy="strict-origin-when-cross-origin"') == 5

    # Only video IDs reach the page. (Home Assistant refuses a <script> in the address itself.)
    assert (await client.get("/api/pleatview/wall", params={"v": "<script>alert(1)</script>"})).status == 400
    response = await client.get("/api/pleatview/wall", params={"v": 'abcdefghijk" onload="alert(1)'})
    assert response.status == 200
    page = await response.text()
    assert "onload" not in page
    assert "<iframe" not in page
    assert "Put their video IDs after" in page
    await hass.config_entries.async_unload(entry.entry_id)


def test_video_ids() -> None:
    assert video_ids(" rFZHOHl-L8A , short,,ROboUHfPUrU ") == ["rFZHOHl-L8A", "ROboUHfPUrU"]
    many = ",".join(f"video-id-{n}" for n in range(10, 40))
    assert len(video_ids(many)) == MAX_STREAMS
