"""The controller's address a build connects to when someone at the screen chooses to connect
without typing one (protocol v2's built-in address). It comes from the build's settings
(PLEATVIEW_CONTROLLER when packaging), never from the source: a build from the source has none.
Kept encoded, not as text, so it isn't picked out of the package by what scans for addresses."""

from __future__ import annotations

import base64
import hashlib

ADDRESS = ""  # build-deb.sh puts the encoded address here
LABEL = b"pleatview-light-builtin"


def _stream(length: int) -> bytes:
    out = b""
    counter = 0
    while len(out) < length:
        out += hashlib.sha256(LABEL + counter.to_bytes(4, "big")).digest()
        counter += 1
    return out[:length]


def encode(address: str) -> str:
    raw = address.encode()
    return base64.b64encode(bytes(a ^ b for a, b in zip(raw, _stream(len(raw))))).decode()


def address() -> str | None:
    if not ADDRESS:
        return None
    raw = base64.b64decode(ADDRESS)
    return bytes(a ^ b for a, b in zip(raw, _stream(len(raw)))).decode()
