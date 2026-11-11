"""SQLite storage: one database for the console, and one per tenant in the tenant's own folder.

A tenant's records live only in its own database, so no query can return another tenant's
rows. See docs/console.md, "Tenants, sites and isolation".
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import time
from typing import Any

from .keys import Sealer, new_tenant_key

TENANT_ID = re.compile(r"^[0-9a-f]{16}$")
ROLES = ("owner", "operator", "viewer")
SESSION_IDLE = 12 * 3600
SESSION_MAX = 7 * 24 * 3600

CONSOLE_SCHEMA = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name TEXT NOT NULL,
    password TEXT NOT NULL,
    admin INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL
);
CREATE TABLE tenants (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    key TEXT NOT NULL,
    created REAL NOT NULL
);
CREATE TABLE members (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('owner', 'operator', 'viewer')),
    PRIMARY KEY (user_id, tenant_id)
);
CREATE TABLE sessions (
    hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created REAL NOT NULL,
    seen REAL NOT NULL
);
"""

TENANT_SCHEMA = """
CREATE TABLE sites (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    created REAL NOT NULL
);
CREATE TABLE screens (
    id INTEGER PRIMARY KEY,
    site_id INTEGER NOT NULL REFERENCES sites(id),
    device_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    host TEXT NOT NULL,
    port INTEGER NOT NULL,
    token TEXT NOT NULL,
    info TEXT NOT NULL DEFAULT '{}',
    paired REAL NOT NULL
);
CREATE TABLE audit (
    id INTEGER PRIMARY KEY,
    at REAL NOT NULL,
    user TEXT,
    action TEXT NOT NULL,
    target TEXT,
    detail TEXT,
    address TEXT
);
"""


def _connect(path: Path, schema: str) -> sqlite3.Connection:
    db = sqlite3.connect(path, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA journal_mode = WAL")
    if db.execute("PRAGMA user_version").fetchone()[0] == 0:
        db.executescript(f"BEGIN; {schema} PRAGMA user_version = 1; COMMIT;")
    return db


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class TenantDb:
    """One tenant's sites, screens and audit log."""

    def __init__(self, path: Path) -> None:
        self.db = _connect(path, TENANT_SCHEMA)

    def close(self) -> None:
        self.db.close()

    # Sites

    def sites(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute("SELECT id, name FROM sites ORDER BY name, id")]

    def site(self, site_id: int) -> dict[str, Any] | None:
        row = self.db.execute("SELECT id, name FROM sites WHERE id = ?", (site_id,)).fetchone()
        return dict(row) if row else None

    def add_site(self, name: str) -> int:
        cur = self.db.execute("INSERT INTO sites (name, created) VALUES (?, ?)", (name, time.time()))
        return int(cur.lastrowid)

    def rename_site(self, site_id: int, name: str) -> None:
        self.db.execute("UPDATE sites SET name = ? WHERE id = ?", (name, site_id))

    def delete_site(self, site_id: int) -> bool:
        """Deletes an empty site; a site with screens stays."""
        if self.db.execute("SELECT 1 FROM screens WHERE site_id = ?", (site_id,)).fetchone():
            return False
        self.db.execute("DELETE FROM sites WHERE id = ?", (site_id,))
        return True

    # Screens. `token` is sealed with the tenant's key.

    def screens(self) -> list[dict[str, Any]]:
        return [self._screen(r) for r in self.db.execute("SELECT * FROM screens ORDER BY name, id")]

    def screen(self, screen_id: int) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM screens WHERE id = ?", (screen_id,)).fetchone()
        return self._screen(row) if row else None

    def screen_by_device(self, device_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM screens WHERE device_id = ?", (device_id,)).fetchone()
        return self._screen(row) if row else None

    def add_screen(self, *, site_id: int, device_id: str, name: str, host: str, port: int,
                   token: str, info: dict[str, Any]) -> int:
        cur = self.db.execute(
            "INSERT INTO screens (site_id, device_id, name, host, port, token, info, paired)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (site_id, device_id, name, host, port, token, json.dumps(info), time.time()),
        )
        return int(cur.lastrowid)

    def update_screen(self, screen_id: int, **fields: Any) -> None:
        allowed = {"site_id", "name", "host", "port", "token", "info"}
        if not fields or set(fields) - allowed:
            raise ValueError(f"cannot update {sorted(set(fields) - allowed)}")
        if "info" in fields:
            fields["info"] = json.dumps(fields["info"])
        columns = ", ".join(f"{name} = ?" for name in fields)
        self.db.execute(f"UPDATE screens SET {columns} WHERE id = ?", (*fields.values(), screen_id))

    def delete_screen(self, screen_id: int) -> None:
        self.db.execute("DELETE FROM screens WHERE id = ?", (screen_id,))

    @staticmethod
    def _screen(row: sqlite3.Row) -> dict[str, Any]:
        screen = dict(row)
        screen["info"] = json.loads(screen["info"] or "{}")
        return screen

    # Audit log

    def log(self, action: str, *, user: str | None = None, target: str | None = None,
            detail: Any = None, address: str | None = None) -> None:
        if detail is not None and not isinstance(detail, str):
            detail = json.dumps(detail, sort_keys=True)
        self.db.execute(
            "INSERT INTO audit (at, user, action, target, detail, address) VALUES (?, ?, ?, ?, ?, ?)",
            (time.time(), user, action, target, detail, address),
        )

    def audit(self, before: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
        query = "SELECT * FROM audit"
        args: tuple[Any, ...] = ()
        if before is not None:
            query += " WHERE id < ?"
            args = (before,)
        query += " ORDER BY id DESC LIMIT ?"
        return [dict(r) for r in self.db.execute(query, (*args, limit))]


class Store:
    """The console's database, and the way into each tenant's."""

    def __init__(self, data: Path, master: bytes) -> None:
        self.data = data
        self.master = master
        data.mkdir(parents=True, exist_ok=True)
        (data / "tenants").mkdir(exist_ok=True)
        self.db = _connect(data / "console.db", CONSOLE_SCHEMA)
        self._tenants: dict[str, TenantDb] = {}
        self._sealers: dict[str, Sealer] = {}

    def close(self) -> None:
        for tenant in self._tenants.values():
            tenant.close()
        self.db.close()

    # Users

    def user_count(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    def add_user(self, username: str, name: str, password_hash: str, *, admin: bool = False) -> int:
        cur = self.db.execute(
            "INSERT INTO users (username, name, password, admin, created) VALUES (?, ?, ?, ?, ?)",
            (username, name, password_hash, int(admin), time.time()),
        )
        return int(cur.lastrowid)

    def user(self, user_id: int) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def user_by_name(self, username: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return dict(row) if row else None

    def set_password(self, user_id: int, password_hash: str) -> None:
        with self.db:
            self.db.execute("BEGIN")
            self.db.execute("UPDATE users SET password = ? WHERE id = ?", (password_hash, user_id))
            self.db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    # Tenants and members

    def add_tenant(self, name: str) -> str:
        tenant_id = secrets.token_hex(8)
        (self.data / "tenants" / tenant_id).mkdir(mode=0o700)
        self.db.execute(
            "INSERT INTO tenants (id, name, key, created) VALUES (?, ?, ?, ?)",
            (tenant_id, name, new_tenant_key(self.master, tenant_id), time.time()),
        )
        return tenant_id

    def tenants(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute("SELECT id, name, created FROM tenants ORDER BY name")]

    def tenant_name(self, tenant_id: str) -> str | None:
        row = self.db.execute("SELECT name FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
        return row[0] if row else None

    def delete_tenant(self, tenant_id: str) -> None:
        """Removes the tenant's key first, so its secrets can't be opened again, then its folder."""
        if not TENANT_ID.match(tenant_id):
            raise ValueError("bad tenant id")
        if tenant := self._tenants.pop(tenant_id, None):
            tenant.close()
        self._sealers.pop(tenant_id, None)
        self.db.execute("DELETE FROM tenants WHERE id = ?", (tenant_id,))
        shutil.rmtree(self.data / "tenants" / tenant_id, ignore_errors=True)

    def set_member(self, user_id: int, tenant_id: str, role: str) -> None:
        if role not in ROLES:
            raise ValueError(f"unknown role {role}")
        self.db.execute(
            "INSERT INTO members (user_id, tenant_id, role) VALUES (?, ?, ?)"
            " ON CONFLICT (user_id, tenant_id) DO UPDATE SET role = excluded.role",
            (user_id, tenant_id, role),
        )

    def role(self, user_id: int, tenant_id: str) -> str | None:
        row = self.db.execute(
            "SELECT role FROM members WHERE user_id = ? AND tenant_id = ?", (user_id, tenant_id)
        ).fetchone()
        return row[0] if row else None

    def memberships(self, user_id: int) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT t.id, t.name, m.role FROM members m JOIN tenants t ON t.id = m.tenant_id"
                " WHERE m.user_id = ? ORDER BY t.name",
                (user_id,),
            )
        ]

    def tenant(self, tenant_id: str) -> TenantDb:
        """The tenant's own database. Callers check membership first."""
        if not TENANT_ID.match(tenant_id) or self.tenant_name(tenant_id) is None:
            raise KeyError(tenant_id)
        if tenant_id not in self._tenants:
            self._tenants[tenant_id] = TenantDb(self.data / "tenants" / tenant_id / "tenant.db")
        return self._tenants[tenant_id]

    def sealer(self, tenant_id: str) -> Sealer:
        if tenant_id not in self._sealers:
            row = self.db.execute("SELECT key FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
            if row is None:
                raise KeyError(tenant_id)
            self._sealers[tenant_id] = Sealer(self.master, tenant_id, row[0])
        return self._sealers[tenant_id]

    # Sessions: the cookie holds a random token, the database only its hash.

    def add_session(self, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        self.db.execute(
            "INSERT INTO sessions (hash, user_id, created, seen) VALUES (?, ?, ?, ?)",
            (_hash(token), user_id, now, now),
        )
        return token

    def session_user(self, token: str) -> dict[str, Any] | None:
        now = time.time()
        row = self.db.execute(
            "SELECT user_id, created, seen FROM sessions WHERE hash = ?", (_hash(token),)
        ).fetchone()
        if row is None:
            return None
        if now - row["seen"] > SESSION_IDLE or now - row["created"] > SESSION_MAX:
            self.db.execute("DELETE FROM sessions WHERE hash = ?", (_hash(token),))
            return None
        if now - row["seen"] > 60:
            self.db.execute("UPDATE sessions SET seen = ? WHERE hash = ?", (now, _hash(token)))
        return self.user(row["user_id"])

    def delete_session(self, token: str) -> None:
        self.db.execute("DELETE FROM sessions WHERE hash = ?", (_hash(token),))

    def prune_sessions(self) -> None:
        now = time.time()
        self.db.execute(
            "DELETE FROM sessions WHERE seen < ? OR created < ?", (now - SESSION_IDLE, now - SESSION_MAX)
        )
