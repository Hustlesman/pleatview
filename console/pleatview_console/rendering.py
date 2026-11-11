"""Screens rendered by a PleatView Renderer the console pairs with (R1).

For screens that can't run pages themselves, such as PleatView Light on a Raspberry Pi Zero,
the console has a renderer show the screen's home page and passes the screen the stream, as
Home Assistant does: the renderer stays behind the console, and each screen's stream address
carries a secret only that screen gets.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

TIMEOUT = aiohttp.ClientTimeout(total=20)
MAX_SIDE = 1920


@dataclass(frozen=True)
class Target:
    base_url: str
    token: str
    fingerprint: str | None = None

    def kwargs(self) -> dict[str, Any]:
        out: dict[str, Any] = {"headers": {"Authorization": f"Bearer {self.token}"}}
        if self.fingerprint:
            out["ssl"] = aiohttp.Fingerprint(bytes.fromhex(self.fingerprint))
        return out


async def fetch_certificate(session: aiohttp.ClientSession, url: str) -> str:
    """The SHA-256 fingerprint of an HTTPS renderer's certificate, pinned when pairing."""
    async with session.get(f"{url}/api/info", ssl=False, timeout=TIMEOUT) as response:
        ssl_object = response.connection.transport.get_extra_info("ssl_object") if response.connection else None
        der = ssl_object.getpeercert(binary_form=True) if ssl_object else None
    if not der:
        raise aiohttp.ClientError("no certificate")
    return hashlib.sha256(der).hexdigest()


async def info(session: aiohttp.ClientSession, url: str, fingerprint: str | None = None,
               token: str | None = None) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if token:
        kwargs["headers"] = {"Authorization": f"Bearer {token}"}
    if fingerprint:
        kwargs["ssl"] = aiohttp.Fingerprint(bytes.fromhex(fingerprint))
    async with session.get(f"{url}/api/info", timeout=TIMEOUT, **kwargs) as response:
        data = await response.json(content_type=None)
    if not isinstance(data, dict) or "encoder" not in data:
        raise ValueError("That address isn't a PleatView Renderer")
    return data


async def pair(session: aiohttp.ClientSession, url: str, pin: str, fingerprint: str | None) -> str:
    kwargs: dict[str, Any] = {"ssl": aiohttp.Fingerprint(bytes.fromhex(fingerprint))} if fingerprint else {}
    async with session.post(f"{url}/api/pair", json={"pin": pin, "client": "PleatView Console"},
                            timeout=TIMEOUT, **kwargs) as response:
        data = await response.json(content_type=None)
    if response.status == 200 and isinstance(data, dict) and data.get("token"):
        return str(data["token"])
    if response.status == 429:
        raise ValueError("Too many wrong PINs. Wait a minute and use the new PIN in the renderer's log.")
    raise ValueError("Wrong PIN. It's in the renderer's log, and changes after each pairing.")


def size(state: dict[str, Any] | None) -> tuple[int, int, float]:
    """The screen's size and density, at most 1080p, as Home Assistant renders it."""
    screen = (state or {}).get("screen") or {}
    try:
        width, height, density = int(screen["width"]), int(screen["height"]), float(screen.get("density") or 1)
    except (KeyError, TypeError, ValueError):
        return 1920, 1080, 1.0
    factor = min(1.0, MAX_SIDE / max(width, height), 1080 / min(width, height))
    return round(width * factor) // 2 * 2, round(height * factor) // 2 * 2, max(0.25, density * factor)


class Rendering:
    """The console's sessions on renderers, one per rendered screen."""

    def __init__(self, session: aiohttp.ClientSession, master: bytes) -> None:
        self.session = session
        self._master = master
        self._shown: dict[str, tuple[str, dict[str, Any]]] = {}  # key -> (renderer url, body)
        self._targets: dict[str, tuple[Target, dict[str, Any]]] = {}  # secret -> (target, what to show)

    def key(self, tenant_id: str, screen_id: int) -> str:
        return f"console-{tenant_id}-{screen_id}"

    def secret(self, tenant_id: str, screen_id: int) -> str:
        """Stable across restarts, and unguessable without the master key."""
        return hmac.new(self._master, f"render:{tenant_id}:{screen_id}".encode(), hashlib.sha256).hexdigest()[:32]

    async def show(self, tenant_id: str, screen_id: int, target: Target, url: str, state: dict[str, Any] | None) -> None:
        width, height, scale = size(state)
        body = {"url": url, "width": width, "height": height, "scale": round(scale, 3), "kiosk": "off"}
        key = self.key(tenant_id, screen_id)
        self._targets[self.secret(tenant_id, screen_id)] = (target, {"key": key, "body": body})
        if self._shown.get(key) == (target.base_url, body):
            return
        try:
            async with self.session.put(f"{target.base_url}/api/sessions/{key}", json=body, timeout=TIMEOUT,
                                        **target.kwargs()) as response:
                if response.status != 200:
                    _LOGGER.warning("Renderer %s refused %s: %s", target.base_url, url, (await response.text())[:200])
                    return
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.warning("Renderer %s not reached: %s", target.base_url, err)
            return
        self._shown[key] = (target.base_url, body)

    async def stop(self, tenant_id: str, screen_id: int) -> None:
        key = self.key(tenant_id, screen_id)
        entry = self._targets.pop(self.secret(tenant_id, screen_id), None)
        self._shown.pop(key, None)
        if entry:
            try:
                async with self.session.delete(f"{entry[0].base_url}/api/sessions/{key}", timeout=TIMEOUT,
                                               **entry[0].kwargs()):
                    pass
            except (aiohttp.ClientError, TimeoutError):
                pass

    async def fetch(self, secret: str, name: str) -> tuple[int, bytes, str, str]:
        """A file of a screen's stream: (status, body, content type, cache control)."""
        entry = next((v for s, v in self._targets.items() if hmac.compare_digest(s, secret)), None)
        if not entry:
            return 404, b"", "text/plain", "no-store"
        target, shown = entry
        try:
            async with self.session.get(f"{target.base_url}/hls/{shown['key']}/{name}", timeout=TIMEOUT,
                                        **target.kwargs()) as response:
                if response.status == 404:
                    # The renderer restarted and forgot the screen: show it again.
                    self._shown.pop(shown["key"], None)
                    try:
                        async with self.session.put(f"{target.base_url}/api/sessions/{shown['key']}",
                                                    json=shown["body"], timeout=TIMEOUT, **target.kwargs()):
                            pass
                    except (aiohttp.ClientError, TimeoutError):
                        pass
                body = await response.read()
                return (response.status, body, response.content_type,
                        response.headers.get("Cache-Control", "no-cache"))
        except (aiohttp.ClientError, TimeoutError):
            return 502, b"", "text/plain", "no-store"
