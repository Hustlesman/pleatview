"""Tests for the TV API client."""

from __future__ import annotations

import asyncio
import hashlib
import hmac

import aiohttp
import pytest

from custom_components.pleatview.api import (
    CannotConnect,
    InvalidAuth,
    InvalidPin,
    PleatViewClient,
    RequestRejected,
)

from .fake_tv import FakeTV


async def test_pair_and_commands(fake_tv: FakeTV) -> None:
    async with aiohttp.ClientSession() as session:
        client = PleatViewClient(session, "127.0.0.1", fake_tv.port)
        assert (await client.get_info())["id"] == "tv-1"
        with pytest.raises(InvalidPin):
            await client.pair("000000", "test")
        token = await client.pair(fake_tv.pin, "test")
        assert token in fake_tv.tokens
        await client.command("show_dashboard", path="/lovelace/1", duration=None)
        assert fake_tv.commands == [{"command": "show_dashboard", "path": "/lovelace/1"}]
        with pytest.raises(RequestRejected, match="explode"):
            await client.command("explode")


async def test_verify(fake_tv: FakeTV, impostor: FakeTV) -> None:
    async with aiohttp.ClientSession() as session:
        token = fake_tv.issue_token()
        assert await PleatViewClient(session, "127.0.0.1", fake_tv.port, token).verify()
        assert not await PleatViewClient(session, "127.0.0.1", impostor.port, token).verify()
        assert not await PleatViewClient(session, "127.0.0.1", fake_tv.port, "forged").verify()
        assert fake_tv.authorizations == []  # the token itself never goes out
        fake_tv.verify_supported = False  # apps before 1.0.4
        assert not await PleatViewClient(session, "127.0.0.1", fake_tv.port, token).verify()


def test_verify_vector() -> None:
    """The same vector is in the app's PairingTest and in docs/protocol.md."""
    key = hashlib.sha256(b"token-1").hexdigest().encode()
    proof = hmac.new(key, b"00112233445566778899aabbccddeeff", hashlib.sha256).hexdigest()
    assert proof == "cfd2cb0b0606646c50484f870216c1870afd7f225109845b6435fdfd40887c7e"


async def test_errors(fake_tv: FakeTV) -> None:
    async with aiohttp.ClientSession() as session:
        with pytest.raises(InvalidAuth):
            await PleatViewClient(session, "127.0.0.1", fake_tv.port, "forged").get_state()
        with pytest.raises(CannotConnect):
            await PleatViewClient(session, "127.0.0.1", 1).get_info()


async def test_event_stream(fake_tv: FakeTV) -> None:
    received: list[dict] = []
    async with aiohttp.ClientSession() as session:
        client = PleatViewClient(session, "127.0.0.1", fake_tv.port, fake_tv.issue_token())
        task = asyncio.create_task(client.listen(received.append))
        await fake_tv.wait_for_stream()
        await fake_tv.push(power="standby")
        async with asyncio.timeout(5):
            while len(received) < 2:
                await asyncio.sleep(0.01)
        assert received[0]["power"] == "on"
        assert received[1]["power"] == "standby"
        fake_tv.close_streams()
        await asyncio.wait_for(task, 5)  # returns when the TV closes the stream


async def test_event_stream_rejects_bad_token(fake_tv: FakeTV) -> None:
    async with aiohttp.ClientSession() as session:
        with pytest.raises(InvalidAuth):
            await PleatViewClient(session, "127.0.0.1", fake_tv.port, "forged").listen(lambda _: None)
