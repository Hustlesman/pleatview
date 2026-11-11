"""Passwords and sign-in throttling, with Python's standard library only."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

# OWASP's scrypt parameters for 32 MiB of memory: N = 2^15, r = 8, p = 3.
SCRYPT_LOG_N, SCRYPT_R, SCRYPT_P = 15, 8, 3
SCRYPT_MAXMEM = 64 * 1024 * 1024
MIN_PASSWORD = 12
MAX_PASSWORD = 1024


def check_new_password(password: str) -> str | None:
    """Why a new password can't be used, or None."""
    if len(password) < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters."
    if len(password) > MAX_PASSWORD:
        return "That password is too long."
    return None


def _scrypt(password: str, salt: bytes, log_n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=1 << log_n, r=r, p=p, maxmem=SCRYPT_MAXMEM, dklen=32)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = _scrypt(password, salt, SCRYPT_LOG_N, SCRYPT_R, SCRYPT_P)
    b64 = lambda raw: base64.b64encode(raw).decode()  # noqa: E731
    return f"scrypt${SCRYPT_LOG_N}${SCRYPT_R}${SCRYPT_P}${b64(salt)}${b64(digest)}"


def check_password(password: str, stored: str) -> bool:
    try:
        scheme, log_n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        actual = _scrypt(password, base64.b64decode(salt), int(log_n), int(r), int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, base64.b64decode(digest))


# A hash to check against when the user doesn't exist, so the answer takes as long either way.
DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


class Throttle:
    """After 5 failures for a key (a user name, or an address), waits a minute, doubling each
    further failure up to an hour. A success clears the key."""

    FREE = 5
    FIRST_WAIT = 60
    LONGEST_WAIT = 3600

    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self._failures: dict[str, tuple[int, float]] = {}

    def wait(self, *keys: str) -> int:
        """Seconds to wait before another try with any of the keys."""
        now = self._clock()
        return max((int(until - now) + 1 for _, until in map(self._get, keys) if until > now), default=0)

    def failed(self, *keys: str) -> None:
        now = self._clock()
        for key in keys:
            count, _ = self._get(key)
            count += 1
            until = 0.0
            if count >= self.FREE:
                until = now + min(self.FIRST_WAIT * 2 ** (count - self.FREE), self.LONGEST_WAIT)
            self._failures[key] = (count, until)
        if len(self._failures) > 10_000:  # forget the oldest, so the table can't grow without end
            for key in list(self._failures)[:1000]:
                del self._failures[key]

    def succeeded(self, *keys: str) -> None:
        for key in keys:
            self._failures.pop(key, None)

    def _get(self, key: str) -> tuple[int, float]:
        return self._failures.get(key, (0, 0.0))
