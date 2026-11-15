"""Where pages from a controller may go: the public internet, and only the places on the
site's own network that the site lists (docs/renderer-connect-out.md).

The browser showing a controller's pages sends every connection through this filter, a SOCKS5
proxy on 127.0.0.1: each connection is one tunnel to one host and port, checked before it's
made. The filter looks names up itself and connects to the address it checked, so a name whose
answer changes between the check and the connection (DNS rebinding) gets nowhere.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import socket
import struct
import time
from typing import Iterable

_LOGGER = logging.getLogger(__name__)

Address = ipaddress.IPv4Address | ipaddress.IPv6Address

# Never the public internet, whatever Python's tables say: ways to reach IPv4 inside IPv6
# (a NAT64 gateway, 6to4, Teredo, IPv4-compatible and -mapped addresses), and documentation.
NOT_PUBLIC = tuple(ipaddress.ip_network(n) for n in (
    "64:ff9b::/96", "64:ff9b:1::/48", "2002::/16", "2001::/32", "::/96", "::ffff:0:0/96", "2001:db8::/32",
    "100.64.0.0/10", "192.0.0.0/24", "198.18.0.0/15",
))
HANDSHAKE_SECONDS = 10
CONNECT_SECONDS = 10
MOST_AT_ONCE = 256
REFUSALS_LOGGED_EVERY = 300.0

# SOCKS5 (RFC 1928): what the filter speaks with the browser.
VERSION, NO_AUTH, NO_METHOD = 5, 0, 0xFF
CONNECT = 1
IPV4, DOMAIN, IPV6 = 1, 3, 4
SUCCEEDED, NOT_ALLOWED, UNREACHABLE, REFUSED, NOT_SUPPORTED, BAD_ADDRESS = 0, 2, 4, 5, 7, 8


def public(address: Address) -> bool:
    """An address on the public internet."""
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return False
    if (address.is_private or address.is_loopback or address.is_link_local or address.is_multicast
            or address.is_reserved or address.is_unspecified or not address.is_global):
        return False
    return not any(address.version == n.version and address in n for n in NOT_PUBLIC)


class Places:
    """The places on the site's network that pages may load: `host:port` (a name or an
    address) or a network (an address with a prefix length, any port)."""

    def __init__(self, entries: Iterable[str] = ()) -> None:
        self.hosts: set[tuple[str, int]] = set()
        self.networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        for raw in entries:
            entry = raw.strip()
            if not entry:
                continue
            if "/" in entry:
                self.networks.append(ipaddress.ip_network(entry, strict=False))
                continue
            host, _, port = entry.rpartition(":")
            if not host or not port.isdigit() or not 0 < int(port) < 65536:
                raise ValueError(f"site_pages: {entry!r} is host:port or a network such as 192.168.1.40/32")
            self.hosts.add((host.strip("[]").lower().rstrip("."), int(port)))

    @classmethod
    def parse(cls, text: str) -> "Places":
        return cls(part for part in text.replace("\n", ",").split(","))

    def named(self, host: str, port: int) -> bool:
        return (host.lower().rstrip("."), port) in self.hosts

    def holds(self, address: Address, port: int) -> bool:
        return (str(address), port) in self.hosts or any(address.version == n.version and address in n
                                                         for n in self.networks)

    def __bool__(self) -> bool:
        return bool(self.hosts or self.networks)


def ip(text: str) -> Address | None:
    try:
        return ipaddress.ip_address(text.strip("[]").split("%", 1)[0])
    except ValueError:
        return None


class Filter:
    """The SOCKS5 proxy the browser uses for a controller's pages."""

    def __init__(self, places: Places | None = None,
                 resolve=None) -> None:  # noqa: ANN001 - async (host, port) -> list of addresses
        self.places = places or Places()
        self.resolve = resolve or self._resolve
        self.port: int | None = None
        self._server: asyncio.base_events.Server | None = None
        self._open = 0
        self._tunnels: set[asyncio.Task] = set()
        self._refusals: dict[str, float] = {}

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self._server:
            self._server.close()
        for task in list(self._tunnels):
            task.cancel()
        if self._tunnels:
            await asyncio.gather(*self._tunnels, return_exceptions=True)
        if self._server:
            await self._server.wait_closed()

    @staticmethod
    async def _resolve(host: str, port: int) -> list[Address]:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        found = []
        for info in infos:
            address = ip(info[4][0])
            if address is not None and address not in found:
                found.append(address)
        return found

    async def destinations(self, host: str, port: int) -> list[Address]:
        """The addresses this host and port may be reached at, in the order to try; none if
        it may not be."""
        literal = ip(host)
        if literal is not None:
            return [literal] if public(literal) or self.places.holds(literal, port) else []
        if not host or len(host) > 253 or host.lower().rstrip(".") in ("localhost", "supervisor"):
            return [] if not self.places.named(host, port) else await self.resolve(host, port)
        try:
            found = await self.resolve(host, port)
        except OSError:
            return []
        if self.places.named(host, port):
            return found  # the site named this place itself
        return [a for a in found if public(a) or self.places.holds(a, port)]

    def _refused(self, what: str) -> None:
        now = time.monotonic()
        if now - self._refusals.get(what, float("-inf")) < REFUSALS_LOGGED_EVERY:
            return
        if len(self._refusals) > 1000:
            self._refusals.clear()
        self._refusals[what] = now
        _LOGGER.info("A page from the controller can't reach %s: not on the public internet, and not in site_pages", what)

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._open >= MOST_AT_ONCE:
            writer.close()
            return
        self._open += 1
        task = asyncio.current_task()
        if task:
            self._tunnels.add(task)
            task.add_done_callback(self._tunnels.discard)
        upstream: asyncio.StreamWriter | None = None
        try:
            try:
                host, port = await asyncio.wait_for(self._handshake(reader, writer), HANDSHAKE_SECONDS)
            except (ValueError, asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
                return
            addresses = await self.destinations(host, port)
            if not addresses:
                self._refused(f"{host}:{port}")
                await self._reply(writer, NOT_ALLOWED)
                return
            for address in addresses:
                try:
                    up_reader, upstream = await asyncio.wait_for(asyncio.open_connection(str(address), port), CONNECT_SECONDS)
                    break
                except (OSError, asyncio.TimeoutError):
                    continue
            if upstream is None:
                await self._reply(writer, UNREACHABLE)
                return
            await self._reply(writer, SUCCEEDED)
            await asyncio.gather(_pipe(reader, upstream), _pipe(up_reader, writer))
        finally:
            self._open -= 1
            for w in (writer, upstream):
                if w is not None:
                    with contextlib.suppress(Exception):
                        w.close()

    async def _handshake(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> tuple[str, int]:
        version, count = await reader.readexactly(2)
        if version != VERSION:
            raise ValueError("not SOCKS5")  # hang up at once, waiting for nothing
        methods = await reader.readexactly(count)
        if version != VERSION or NO_AUTH not in methods:
            writer.write(bytes([VERSION, NO_METHOD]))
            await writer.drain()
            raise ValueError("not SOCKS5 without authentication")
        writer.write(bytes([VERSION, NO_AUTH]))
        await writer.drain()
        version, command, _, kind = await reader.readexactly(4)
        if version != VERSION:
            raise ValueError("not SOCKS5")
        if kind == IPV4:
            host = str(ipaddress.IPv4Address(await reader.readexactly(4)))
        elif kind == IPV6:
            host = str(ipaddress.IPv6Address(await reader.readexactly(16)))
        elif kind == DOMAIN:
            length = (await reader.readexactly(1))[0]
            host = (await reader.readexactly(length)).decode("ascii", errors="strict")  # names come as punycode
        else:
            await self._reply(writer, BAD_ADDRESS)
            raise ValueError("unknown address type")
        port = struct.unpack("!H", await reader.readexactly(2))[0]
        if command != CONNECT:
            await self._reply(writer, NOT_SUPPORTED)
            raise ValueError("only CONNECT")
        if port == 0:
            await self._reply(writer, NOT_ALLOWED)
            raise ValueError("port 0")
        return host, port

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, code: int) -> None:
        writer.write(bytes([VERSION, code, 0, IPV4, 0, 0, 0, 0, 0, 0]))
        with contextlib.suppress(ConnectionError):
            await writer.drain()


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        with contextlib.suppress(Exception):
            if writer.can_write_eof():
                writer.write_eof()
