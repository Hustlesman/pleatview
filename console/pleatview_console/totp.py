"""Two-factor sign-in with an authenticator app (TOTP, RFC 6238), and recovery codes.

Standard library only: HMAC-SHA1, 30-second steps, 6 digits, accepting one step either side
for clocks that drift. The caller keeps the last step used, so a code works only once.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

STEP = 30
DIGITS = 6
WINDOW = 1
ISSUER = "PleatView Console"
RECOVERY_CODES = 10


def new_secret() -> str:
    """A 160-bit secret in base32, as authenticator apps take it."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def code_at(secret: str, step: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(number % 10**DIGITS).zfill(DIGITS)


def verify(secret: str, code: str, last_step: int, now: float | None = None) -> int | None:
    """The time step the code belongs to, if it's right and newer than last_step; else None."""
    code = "".join(code.split())
    if len(code) != DIGITS or not code.isdigit():
        return None
    current = int((time.time() if now is None else now) // STEP)
    for step in range(current - WINDOW, current + WINDOW + 1):
        if step > last_step and hmac.compare_digest(code_at(secret, step), code):
            return step
    return None


def uri(secret: str, account: str) -> str:
    """An otpauth:// link: authenticator apps on the same phone open it directly."""
    label = quote(f"{ISSUER}:{account}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(ISSUER)}&algorithm=SHA1&digits={DIGITS}&period={STEP}"


def new_recovery_codes() -> list[str]:
    """Ten one-time codes like 'k7m2-q9xw-4tbn', for when the phone is lost."""
    alphabet = "23456789abcdefghjkmnpqrstuvwxyz"
    return ["-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3))
            for _ in range(RECOVERY_CODES)]


def recovery_hash(code: str) -> str:
    return hashlib.sha256("".join(code.lower().split()).replace("-", "").encode()).hexdigest()
