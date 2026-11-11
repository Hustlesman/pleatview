"""Announces the screen as `_pleatview._tcp` over mDNS, with the standard library only.

Answers questions for the service, this screen's instance and its host name, and announces
itself when it starts and when its name changes. Shares port 5353 with avahi-daemon.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
from typing import Callable

_LOGGER = logging.getLogger(__name__)

GROUP = "224.0.0.251"
PORT = 5353
SERVICE = "_pleatview._tcp.local."
A, PTR, TXT, SRV, ANY = 1, 12, 16, 33, 255
TTL_HOST, TTL_OTHER = 120, 4500


def encode_name(name: str) -> bytes:
    out = b""
    for label in name.rstrip(".").split("."):
        raw = label.encode()[:63]
        out += bytes([len(raw)]) + raw
    return out + b"\0"


def read_name(buf: bytes, off: int) -> tuple[str, int]:
    labels: list[str] = []
    end = None
    for _ in range(128):
        length = buf[off]
        if length & 0xC0 == 0xC0:
            end = end if end is not None else off + 2
            off = ((length & 0x3F) << 8) | buf[off + 1]
            continue
        off += 1
        if length == 0:
            return ".".join(labels).lower() + ".", end if end is not None else off
        labels.append(buf[off:off + length].decode("utf-8", "replace"))
        off += length
    raise ValueError("name too long")


def questions(buf: bytes) -> list[tuple[str, int]]:
    """The questions in a query; nothing for responses or anything unreadable."""
    try:
        flags, count = struct.unpack_from("!HH", buf, 2)
        if flags & 0x8000:
            return []
        off, out = 12, []
        for _ in range(count):
            name, off = read_name(buf, off)
            qtype = struct.unpack_from("!H", buf, off)[0]
            off += 4
            out.append((name, qtype))
        return out
    except (IndexError, struct.error, ValueError):
        return []


def record(name: str, rtype: int, ttl: int, data: bytes, flush: bool = True) -> bytes:
    rclass = 0x8001 if flush else 0x0001
    return encode_name(name) + struct.pack("!HHIH", rtype, rclass, ttl, len(data)) + data


class Responder(asyncio.DatagramProtocol):
    def __init__(self, info: Callable[[], dict[str, str]], port: int) -> None:
        self.info = info  # id, name, api, version
        self.port = port
        self.transport: asyncio.DatagramTransport | None = None

    # Names

    @property
    def instance(self) -> str:
        name = (self.info().get("name") or "PleatView").replace(".", " ")[:63]
        return f"{name}.{SERVICE}"

    @property
    def host(self) -> str:
        return f"pleatview-{self.info()['id'][:8]}.local."

    def records(self, ttl: int | None = None) -> dict[str, bytes]:
        info = self.info()
        txt = b"".join(bytes([len(item)]) + item for item in (
            f"{k}={info.get(k, '')}".encode()[:255] for k in ("id", "name", "api", "version")))
        return {
            "ptr": record(SERVICE, PTR, TTL_OTHER if ttl is None else ttl, encode_name(self.instance), flush=False),
            "srv": record(self.instance, SRV, TTL_HOST if ttl is None else ttl,
                          struct.pack("!HHH", 0, 0, self.port) + encode_name(self.host)),
            "txt": record(self.instance, TXT, TTL_OTHER if ttl is None else ttl, txt),
            "a": record(self.host, A, TTL_HOST if ttl is None else ttl, socket.inet_aton(local_address())),
        }

    def answer(self, asked: list[tuple[str, int]]) -> tuple[list[bytes], list[bytes]] | None:
        recs = self.records()
        answers: list[bytes] = []
        for name, qtype in asked:
            if name == SERVICE.lower() and qtype in (PTR, ANY):
                answers.append(recs["ptr"])
            elif name == self.instance.lower() and qtype in (SRV, TXT, ANY):
                answers += [recs["srv"], recs["txt"]] if qtype == ANY else [recs["srv" if qtype == SRV else "txt"]]
            elif name == self.host.lower() and qtype in (A, ANY):
                answers.append(recs["a"])
        if not answers:
            return None
        extra = [r for key, r in recs.items() if key != "ptr" and r not in answers]
        return answers, extra

    @staticmethod
    def message(answers: list[bytes], extra: list[bytes], ident: int = 0) -> bytes:
        return struct.pack("!6H", ident, 0x8400, 0, len(answers), 0, len(extra)) + b"".join(answers + extra)

    # Network

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.create_datagram_endpoint(lambda: self, sock=self._socket())
        await self.announce()

    async def announce(self, ttl: int | None = None) -> None:
        """Unsolicited answers, twice a second apart, as RFC 6762 asks; ttl 0 says goodbye."""
        recs = self.records(ttl)
        packet = self.message([recs["ptr"], recs["srv"], recs["txt"], recs["a"]], [])
        for delay in (0, 1):
            await asyncio.sleep(delay)
            self._send(packet, (GROUP, PORT))

    def stop(self) -> None:
        if self.transport:
            self.transport.close()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        reply = self.answer(questions(data))
        if not reply:
            return
        if addr[1] != PORT:  # a one-shot query from another port: answer it directly
            self._send(self.message(*reply, ident=struct.unpack_from("!H", data)[0]), addr)
        else:
            self._send(self.message(*reply), (GROUP, PORT))

    def _send(self, packet: bytes, addr: tuple[str, int]) -> None:
        if self.transport:
            try:
                self.transport.sendto(packet, addr)
            except OSError as err:
                _LOGGER.debug("mDNS: %s", err)

    @staticmethod
    def _socket() -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.bind(("", PORT))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                        struct.pack("4s4s", socket.inet_aton(GROUP), socket.inet_aton("0.0.0.0")))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        sock.setblocking(False)
        return sock


def local_address() -> str:
    """This machine's address on the local network (no packet is sent to find it)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
