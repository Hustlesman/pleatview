"""A small WebSocket client (RFC 6455) on asyncio streams, for protocol v2: text frames of JSON,
pings answered, messages up to 1 MiB. Its frame functions serve the tests' server too."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import ssl
import struct
from typing import Any
from urllib.parse import urlsplit

MAX_MESSAGE = 1 << 20
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
TEXT, BINARY, CLOSE, PING, PONG, CONTINUATION = 0x1, 0x2, 0x8, 0x9, 0xA, 0x0


class Closed(Exception):
    """The connection ended; `code` is the close code, when the other side sent one."""

    def __init__(self, code: int | None = None, reason: str = "") -> None:
        super().__init__(f"closed ({code}) {reason}".strip())
        self.code = code
        self.reason = reason


def accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()


def encode(opcode: int, payload: bytes, *, mask: bool) -> bytes:
    """One final frame. Clients mask what they send; servers don't."""
    length = len(payload)
    head = bytes([0x80 | opcode])
    flag = 0x80 if mask else 0
    if length < 126:
        head += bytes([flag | length])
    elif length < 1 << 16:
        head += bytes([flag | 126]) + struct.pack("!H", length)
    else:
        head += bytes([flag | 127]) + struct.pack("!Q", length)
    if not mask:
        return head + payload
    key = os.urandom(4)
    return head + key + bytes(b ^ key[i % 4] for i, b in enumerate(payload))


async def read_frame(reader: asyncio.StreamReader) -> tuple[bool, int, bytes]:
    """(final, opcode, payload), unmasked."""
    first, second = await reader.readexactly(2)
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", await reader.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", await reader.readexactly(8))[0]
    if length > MAX_MESSAGE:
        raise Closed(1009, "message too big")
    key = await reader.readexactly(4) if second & 0x80 else None
    payload = await reader.readexactly(length)
    if key:
        payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    return bool(first & 0x80), first & 0x0F, payload


class WebSocket:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.close_code: int | None = None

    async def send_json(self, data: dict[str, Any]) -> None:
        if self.writer.is_closing():
            raise Closed(self.close_code)
        self.writer.write(encode(TEXT, json.dumps(data).encode(), mask=True))
        await self.writer.drain()

    async def receive_json(self) -> dict[str, Any]:
        """The next JSON object; raises Closed when the connection ends."""
        parts: list[bytes] = []
        while True:
            try:
                final, opcode, payload = await read_frame(self.reader)
            except (asyncio.IncompleteReadError, ConnectionError) as err:
                raise Closed(self.close_code) from err
            if opcode == PING:
                if not self.writer.is_closing():
                    self.writer.write(encode(PONG, payload, mask=True))
                    await self.writer.drain()
                continue
            if opcode == PONG:
                continue
            if opcode == CLOSE:
                code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else None
                self.close_code = code
                await self.close(code or 1000)
                raise Closed(code, payload[2:].decode("utf-8", "replace"))
            if opcode not in (TEXT, BINARY, CONTINUATION):
                raise Closed(1002, "unknown frame")
            parts.append(payload)
            if sum(map(len, parts)) > MAX_MESSAGE:
                raise Closed(1009, "message too big")
            if final:
                data = json.loads(b"".join(parts))
                if not isinstance(data, dict):
                    raise ValueError("expected a JSON object")
                return data

    async def close(self, code: int = 1000) -> None:
        if self.writer.is_closing():
            return
        try:
            self.writer.write(encode(CLOSE, struct.pack("!H", code), mask=True))
            await self.writer.drain()
        except (OSError, RuntimeError):
            pass
        self.writer.close()


async def connect(url: str, *, subprotocol: str, pin: str | None = None, timeout: float = 15,
                  context: ssl.SSLContext | None = None) -> WebSocket:
    """Opens a wss:// connection. With `pin` (hex of the SHA-256 of the server's certificate),
    only that certificate is accepted; otherwise the system's authorities decide."""
    parts = urlsplit(url)
    if parts.scheme != "wss" or not parts.hostname:
        raise ValueError("only wss:// addresses")
    if context is None:
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        if pin:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE  # the pin below decides instead
    host, port = parts.hostname, parts.port or 443
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port, ssl=context, server_hostname=host), timeout)
    try:
        if pin:
            der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
            if hashlib.sha256(der).hexdigest() != pin.lower():
                raise ssl.SSLError("the server's certificate isn't the one pinned")
        key = base64.b64encode(os.urandom(16)).decode()
        target = f"[{host}]" if ":" in host else host
        if parts.port and parts.port != 443:
            target += f":{parts.port}"
        writer.write((f"GET {parts.path or '/'} HTTP/1.1\r\nHost: {target}\r\nUpgrade: websocket\r\n"
                      f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                      f"Sec-WebSocket-Protocol: {subprotocol}\r\n\r\n").encode())
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
        lines = head.decode("latin-1").split("\r\n")
        headers = {k.strip().lower(): v.strip() for k, _, v in (line.partition(":") for line in lines[1:] if line)}
        if " 101 " not in f"{lines[0]} " or headers.get("sec-websocket-accept") != accept_key(key):
            raise ConnectionError(f"not a WebSocket: {lines[0]}")
        if headers.get("sec-websocket-protocol") != subprotocol:
            raise ConnectionError("the server doesn't speak " + subprotocol)
    except BaseException:
        writer.close()
        raise
    return WebSocket(reader, writer)
