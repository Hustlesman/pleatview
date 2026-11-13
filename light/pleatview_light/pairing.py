"""PIN pairing, as in the Android and Linux apps (docs/protocol.md, "Pairing and authentication").

The PIN shows on the screen; a client that knows it gets a bearer token. Only SHA-256 hashes
of tokens are kept, the newest 8. Five wrong PINs lock pairing for a minute and change the PIN.
"""

from __future__ import annotations

import logging

import hashlib
import hmac
import json
import re
import secrets
import time
from typing import Any, Callable

PIN_LENGTH = 6
MAX_FAILURES = 5
LOCKOUT_SECONDS = 60
MAX_TOKENS = 8


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(sha256(a), sha256(b))


def new_pin() -> str:
    return "".join(str(secrets.randbelow(10)) for _ in range(PIN_LENGTH))


_LOGGER = logging.getLogger(__name__)

class Pairing:
    def __init__(self, load: Callable[[], str | None], save: Callable[[str], None],
                 clock: Callable[[], float] = time.monotonic, on_changed: Callable[[], None] = lambda: None) -> None:
        self._save = save
        self._clock = clock
        self.on_changed = on_changed
        self.records: list[dict[str, Any]] = []
        self.failures = 0
        self.locked_until = 0.0
        self.pin = new_pin()
        try:
            stored = json.loads(load() or "[]")
        except ValueError:
            stored = []  # unreadable records: start unpaired, as the other apps do
        for record in stored if isinstance(stored, list) else []:
            if isinstance(record, dict) and isinstance(record.get("hash"), str):
                self.records.append({"hash": record["hash"], "client": str(record.get("client", "")),
                                     "created": record.get("created", 0)})

    @property
    def paired(self) -> bool:
        return bool(self.records)

    def pair(self, candidate: Any, client: Any) -> dict[str, Any]:
        refused = self._check(candidate)
        if refused:
            return refused
        token = secrets.token_hex(32)
        self.records.append({"hash": sha256(token), "client": str(client or "").strip()[:64] or "client",
                             "created": time.time()})
        del self.records[:-MAX_TOKENS]
        self._persist()
        self.rotate_pin()
        return {"result": "paired", "token": token}

    def _check(self, candidate: Any) -> dict[str, Any] | None:
        now = self._clock()
        if now < self.locked_until:
            return {"result": "locked_out", "retry_after": int(self.locked_until - now) + 1}
        digits = re.sub(r"\D", "", str(candidate or ""))
        if not _same(digits, self.pin):
            self.failures += 1
            if self.failures >= MAX_FAILURES:
                self.failures = 0
                self.locked_until = now + LOCKOUT_SECONDS
                self.rotate_pin()
                _LOGGER.warning("Pairing locked for %d s after %d wrong PINs; the PIN changed", LOCKOUT_SECONDS, MAX_FAILURES)
            return {"result": "invalid_pin"}
        self.failures = 0
        return None

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        digest = sha256(token)
        match = False
        for record in self.records:  # every record, so timing doesn't tell which matched
            match = _same(record["hash"], digest) or match
        return match

    def prove(self, nonce: str) -> list[str]:
        """One HMAC-SHA256 of the nonce per paired client, keyed with its token's hash."""
        return [hmac.new(r["hash"].encode(), nonce.encode(), hashlib.sha256).hexdigest() for r in self.records]

    def revoke(self, token: str) -> None:
        digest = sha256(token or "")
        before = len(self.records)
        self.records = [r for r in self.records if r["hash"] != digest]
        if len(self.records) != before:
            self._persist()
        self.on_changed()

    def rotate_pin(self) -> None:
        self.pin = new_pin()
        self.on_changed()

    def _persist(self) -> None:
        self._save(json.dumps(self.records))
