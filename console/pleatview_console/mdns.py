"""Finds PleatView screens on the local network by their mDNS announcement (`_pleatview._tcp`).

Written on the standard library (Debian has no Python mDNS package): it asks for the service
now and then, and reads the answers, PTR, SRV, TXT and A records, from multicast and unicast
replies. Only reads; it never answers for anything.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
import socket
import struct
import time
from typing import Any

_LOGGER = logging.getLogger(__name__)

GROUP = "224.0.0.251"
PORT = 5353
SERVICE = "_pleatview._tcp.local."
A, PTR, TXT, SRV = 1, 12, 16, 33
QUERY_EVERY = 60.0
FIRST_QUERIES = (0.0, 1.0, 3.0)
MAX_HOPS = 32


def _read_name(buf: bytes, off: int) -> tuple[str, int]:
    labels: list[str] = []
    end: int | None = None
    for _ in range(MAX_HOPS * 4):
        length = buf[off]
        if length & 0xC0 == 0xC0:
            if end is None:
                end = off + 2
            off = ((length & 0x3F) << 8) | buf[off + 1]
            continue
        off += 1
        if length == 0:
            return ".".join(labels) + ".", end if end is not None else off
        if off + length > len(buf):
            raise ValueError("label past the end")
        labels.append(buf[off : off + length].decode("utf-8", "replace"))
        off += length
    raise ValueError("name too long or looped")


def _read_txt(buf: bytes, start: int, end: int) -> dict[str, str]:
    values: dict[str, str] = {}
    while start < end:
        length = buf[start]
        item = buf[start + 1 : start + 1 + length].decode("utf-8", "replace")
        start += 1 + length
        key, _, value = item.partition("=")
        if key and key.lower() not in values:
            values[key.lower()] = value
    return values


def parse(buf: bytes) -> list[tuple[str, int, int, Any]]:
    """The PTR, SRV, TXT and A records in a DNS message: (name, type, ttl, data)."""
    try:
        questions, answers, authority, additional = struct.unpack_from("!4H", buf, 4)
        off = 12
        for _ in range(questions):
            _, off = _read_name(buf, off)
            off += 4
        records = []
        for _ in range(answers + authority + additional):
            name, off = _read_name(buf, off)
            rtype, _, ttl, length = struct.unpack_from("!HHIH", buf, off)
            off += 10
            start, off = off, off + length
            if off > len(buf):
                raise ValueError("record past the end")
            if rtype == PTR:
                data: Any = _read_name(buf, start)[0].lower()
            elif rtype == SRV:
                port = struct.unpack_from("!H", buf, start + 4)[0]
                data = (_read_name(buf, start + 6)[0].lower(), port)
            elif rtype == TXT:
                data = _read_txt(buf, start, off)
            elif rtype == A and length == 4:
                data = socket.inet_ntoa(buf[start:off])
            else:
                continue
            records.append((name.lower(), rtype, ttl, data))
        return records
    except (IndexError, struct.error, ValueError):
        return []


def query(*questions: tuple[str, int]) -> bytes:
    out = bytearray(struct.pack("!6H", 0, 0, len(questions), 0, 0, 0))
    for name, qtype in questions:
        for label in name.rstrip(".").split("."):
            raw = label.encode()[:63]
            out += bytes([len(raw)]) + raw
        out += b"\0" + struct.pack("!HH", qtype, 1)
    return bytes(out)


class Browser(asyncio.DatagramProtocol):
    """Keeps a table of the PleatView screens announced on the network."""

    def __init__(self, on_change: Callable[[], None] | None = None) -> None:
        self._on_change = on_change
        self._transport: asyncio.DatagramTransport | None = None
        self._task: asyncio.Task | None = None
        self._ptr: dict[str, float] = {}
        self._srv: dict[str, tuple[str, int, float]] = {}
        self._txt: dict[str, tuple[dict[str, str], float]] = {}
        self._addr: dict[str, tuple[str, float]] = {}
        self._last: dict[str, dict[str, Any]] = {}

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.create_datagram_endpoint(lambda: self, sock=self._socket())
        self._task = asyncio.create_task(self._ask())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        if self._transport:
            self._transport.close()

    def found(self) -> dict[str, dict[str, Any]]:
        """Screens by their id: host, port, name, version and api."""
        now = time.monotonic()
        screens: dict[str, dict[str, Any]] = {}
        for instance, until in self._ptr.items():
            srv, txt = self._srv.get(instance), self._txt.get(instance)
            if until < now or not srv or srv[2] < now or not txt or not txt[0].get("id"):
                continue
            address = self._addr.get(srv[0])
            if not address or address[1] < now:
                continue
            values = txt[0]
            screens[values["id"]] = {
                "id": values["id"],
                "name": values.get("name") or instance.split(".", 1)[0],
                "host": address[0],
                "port": srv[1],
                "version": values.get("version", ""),
                "api": values.get("api", ""),
            }
        return screens

    # asyncio.DatagramProtocol

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if len(data) < 12 or not data[2] & 0x80:  # only responses
            return
        now = time.monotonic()
        for name, rtype, ttl, value in parse(data):
            until = now + ttl
            if rtype == PTR and name == SERVICE:
                self._ptr[value] = until
            elif rtype == SRV:
                self._srv[name] = (value[0], value[1], until)
            elif rtype == TXT:
                self._txt[name] = (value, until)
            elif rtype == A:
                self._addr[name] = (value, until)
        self._changed()

    def error_received(self, exc: Exception) -> None:
        _LOGGER.debug("mDNS: %s", exc)

    # Inside

    def _changed(self) -> None:
        current = self.found()
        if current != self._last:
            self._last = current
            if self._on_change:
                self._on_change()

    async def _ask(self) -> None:
        delays = [b - a for a, b in zip((0.0, *FIRST_QUERIES), FIRST_QUERIES)]
        while True:
            for delay in delays:
                await asyncio.sleep(delay)
                self._send()
            delays = [QUERY_EVERY]
            self._forget_expired()

    def _send(self) -> None:
        if not self._transport:
            return
        questions = [(SERVICE, PTR)]
        now = time.monotonic()
        # Ask again for whatever an announcement left out.
        for instance, until in self._ptr.items():
            if until < now:
                continue
            if instance not in self._srv:
                questions.append((instance, SRV))
            if instance not in self._txt:
                questions.append((instance, TXT))
            srv = self._srv.get(instance)
            if srv and srv[0] not in self._addr:
                questions.append((srv[0], A))
        try:
            self._transport.sendto(query(*questions[:20]), (GROUP, PORT))
        except OSError as err:
            _LOGGER.debug("mDNS query failed: %s", err)

    def _forget_expired(self) -> None:
        now = time.monotonic()
        for table in (self._ptr, self._srv, self._txt, self._addr):
            for key in [k for k, v in table.items() if (v if isinstance(v, float) else v[-1]) < now]:
                del table[key]
        self._changed()

    @staticmethod
    def _socket() -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        try:
            sock.bind(("", PORT))
            membership = struct.pack("4s4s", socket.inet_aton(GROUP), socket.inet_aton("0.0.0.0"))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
        except OSError as err:
            # Another responder holds the port without sharing it: ask from any port instead,
            # and responders answer that port directly (RFC 6762, section 6.7).
            _LOGGER.info("mDNS port busy (%s); asking from another port", err)
            sock.close()
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.bind(("", 0))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        sock.setblocking(False)
        return sock
