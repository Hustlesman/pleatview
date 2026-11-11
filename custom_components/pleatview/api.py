"""Async client for the PleatView TV app's local API (see docs/protocol.md)."""

from __future__ import annotations

from collections.abc import Callable
import hashlib
import hmac
import json
import logging
import secrets
from typing import Any

import aiohttp
from yarl import URL

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)
# The TV sends a comment ping every 20 s, so a silent minute means the link is dead.
STREAM_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=60)


class PleatViewError(Exception):
    """Base class for PleatView errors."""


class CannotConnect(PleatViewError):
    """The TV could not be reached."""


class InvalidAuth(PleatViewError):
    """The TV no longer accepts our pairing token."""


class InvalidPin(PleatViewError):
    """The pairing PIN was wrong."""


class TooManyAttempts(PleatViewError):
    """Pairing is temporarily locked after repeated wrong PINs."""


class RequestRejected(PleatViewError):
    """The TV rejected a command or configuration value."""


class NotFound(PleatViewError):
    """The TV doesn't know the request (an older app)."""


class PleatViewClient:
    """Talks to one TV over HTTP."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int,
        token: str | None = None,
    ) -> None:
        """Initialize the client."""
        self._session = session
        self.host = host
        self.port = port
        self.token = token
        self._base = URL.build(scheme="http", host=host, port=port)

    @property
    def base_url(self) -> str:
        """Return the TV's base URL (also serves the phone setup page)."""
        return str(self._base)

    async def get_info(self) -> dict[str, Any]:
        """Return device identity; works without pairing."""
        return await self._request("GET", "/api/info", auth=False)

    async def pair(self, pin: str, client: str) -> str:
        """Exchange the PIN shown on the TV for an API token."""
        data = await self._request(
            "POST", "/api/pair", {"pin": pin, "client": client}, auth=False
        )
        token = data.get("token")
        if not isinstance(token, str) or not token:
            raise PleatViewError("Pairing response did not contain a token")
        self.token = token
        return token

    async def request_approval(self, client: str) -> dict[str, Any]:
        """Asks the person at the TV to allow pairing with OK (apps listing "approve" in
        /api/info's `pairing`). Returns the request's id and how long it waits."""
        return await self._request("POST", "/api/pair/request", {"client": client}, auth=False)

    async def approval_state(self, request_id: str) -> dict[str, Any]:
        """pending, approved (with the token, handed over once), denied or expired."""
        return await self._request("GET", f"/api/pair/request/{request_id}", auth=False)

    async def verify(self) -> bool:
        """Check that this address is the TV that issued our token, without sending the token.

        The TV answers a fresh nonce with an HMAC keyed by the hash of each token it issued.
        Apps before 1.0.4 don't know the request, and anything else can't answer it.
        """
        if not self.token:
            return False
        nonce = secrets.token_hex(16)
        try:
            data = await self._request("POST", "/api/verify", {"nonce": nonce}, auth=False)
        except (InvalidAuth, InvalidPin, TooManyAttempts, RequestRejected, NotFound):
            return False
        key = hashlib.sha256(self.token.encode()).hexdigest().encode()
        expected = hmac.new(key, nonce.encode(), hashlib.sha256).hexdigest()
        proofs = data.get("proofs")
        return isinstance(proofs, list) and any(
            isinstance(proof, str) and hmac.compare_digest(proof, expected) for proof in proofs
        )

    async def get_state(self) -> dict[str, Any]:
        """Return the current state snapshot."""
        return await self._request("GET", "/api/state")

    async def get_config(self) -> dict[str, Any]:
        """Return the TV's configuration (secrets omitted)."""
        return await self._request("GET", "/api/config")

    async def set_config(self, config: dict[str, Any]) -> dict[str, Any]:
        """Update the TV's configuration; only the given keys change."""
        return await self._request("POST", "/api/config", config)

    async def command(self, command: str, **params: Any) -> None:
        """Send a command. Parameters that are None are left out."""
        payload = {"command": command}
        payload.update({key: value for key, value in params.items() if value is not None})
        await self._request("POST", "/api/command", payload)

    async def unpair(self) -> None:
        """Revoke our token on the TV."""
        await self._request("POST", "/api/unpair", {})

    async def listen(self, on_state: Callable[[dict[str, Any]], None]) -> None:
        """Consume the Server-Sent Events stream until it ends or fails.

        ``on_state`` is called with the full state on connect and after every change.
        """
        headers = {"Accept": "text/event-stream"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            async with self._session.get(
                self._base.with_path("/api/events"),
                headers=headers,
                timeout=STREAM_TIMEOUT,
                allow_redirects=False,
            ) as resp:
                if resp.status == 401:
                    raise InvalidAuth("The TV rejected the pairing token")
                if resp.status != 200:
                    raise PleatViewError(f"Event stream returned HTTP {resp.status}")
                event: str | None = None
                data: list[str] = []
                async for raw in resp.content:
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if not line:
                        if event == "state" and data:
                            try:
                                state = json.loads("\n".join(data))
                            except ValueError:
                                _LOGGER.debug("Ignoring malformed state event")
                            else:
                                if isinstance(state, dict):
                                    on_state(state)
                        event, data = None, []
                        continue
                    if line.startswith(":"):
                        continue  # keep-alive comment
                    field, _, value = line.partition(":")
                    value = value.removeprefix(" ")
                    if field == "event":
                        event = value
                    elif field == "data":
                        data.append(value)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CannotConnect(str(err) or type(err).__name__) from err

    async def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        auth: bool = True,
    ) -> dict[str, Any]:
        headers = {}
        if auth and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            async with self._session.request(
                method,
                self._base.with_path(path),
                json=payload,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
                # The TV never redirects: whatever does isn't the TV.
                allow_redirects=False,
            ) as resp:
                try:
                    data = await resp.json(content_type=None)
                except ValueError:
                    data = None
                if not isinstance(data, dict):
                    data = {}
                error = str(data.get("error", ""))
                if resp.status == 401:
                    raise InvalidAuth("The TV rejected the pairing token")
                if resp.status == 403 and error == "invalid_pin":
                    raise InvalidPin
                if resp.status == 429:
                    raise TooManyAttempts(data.get("retry_after"))
                if resp.status == 400:
                    raise RequestRejected(error or "bad request")
                if resp.status == 404:
                    raise NotFound(error or "not found")
                if resp.status >= 400:
                    raise PleatViewError(f"HTTP {resp.status} {error}".strip())
                return data
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CannotConnect(str(err) or type(err).__name__) from err
