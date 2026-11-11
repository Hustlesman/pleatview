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

from .keys import Sealer, new_tenant_key, open_sealed, seal

TENANT_ID = re.compile(r"^[0-9a-f]{16}$")
ROLES = ("owner", "operator", "viewer")
SESSION_IDLE = 12 * 3600
SESSION_MAX = 7 * 24 * 3600

CONSOLE_V1 = """
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

TENANT_V1 = """
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


# Two-factor sign-in, recovery codes, passwords to change at first sign-in, console settings.
CONSOLE_V2 = """
ALTER TABLE users ADD COLUMN totp TEXT;
ALTER TABLE users ADD COLUMN totp_pending TEXT;
ALTER TABLE users ADD COLUMN totp_step INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN must_change INTEGER NOT NULL DEFAULT 0;
CREATE TABLE recovery_codes (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    hash TEXT NOT NULL,
    PRIMARY KEY (user_id, hash)
);
CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

CONSOLE_SCHEMA = [CONSOLE_V1, CONSOLE_V2]
# The settings the console keeps on each screen it manages (home page, zoom).
TENANT_V2 = """
ALTER TABLE screens ADD COLUMN config TEXT NOT NULL DEFAULT '{}';
"""

# Groups of screens, pages (web pages and playlists) and the media library.
TENANT_V3 = """
CREATE TABLE groups (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    created REAL NOT NULL
);
CREATE TABLE group_screens (
    group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    screen_id INTEGER NOT NULL REFERENCES screens(id) ON DELETE CASCADE,
    PRIMARY KEY (group_id, screen_id)
);
CREATE TABLE media (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    token TEXT NOT NULL UNIQUE,
    size INTEGER NOT NULL,
    status TEXT NOT NULL,
    progress REAL NOT NULL DEFAULT 0,
    kind TEXT,
    duration REAL,
    playable TEXT,
    summary TEXT,
    error TEXT,
    created REAL NOT NULL
);
CREATE TABLE pages (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('web', 'playlist')),
    url TEXT,
    items TEXT NOT NULL DEFAULT '[]',
    token TEXT NOT NULL UNIQUE,
    created REAL NOT NULL
);
ALTER TABLE screens ADD COLUMN home_page INTEGER REFERENCES pages(id) ON DELETE SET NULL;
"""

TENANT_SCHEMA = [TENANT_V1, TENANT_V2, TENANT_V3]


def _connect(path: Path, migrations: list[str]) -> sqlite3.Connection:
    """Opens a database and brings it up to date, one migration per version."""
    db = sqlite3.connect(path, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA journal_mode = WAL")
    version = db.execute("PRAGMA user_version").fetchone()[0]
    for number, script in enumerate(migrations[version:], start=version + 1):
        db.executescript(f"BEGIN; {script} PRAGMA user_version = {number}; COMMIT;")
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
        allowed = {"site_id", "name", "host", "port", "token", "info", "config"}
        if not fields or set(fields) - allowed:
            raise ValueError(f"cannot update {sorted(set(fields) - allowed)}")
        for key in ("info", "config"):
            if key in fields:
                fields[key] = json.dumps(fields[key], sort_keys=True)
        columns = ", ".join(f"{name} = ?" for name in fields)
        self.db.execute(f"UPDATE screens SET {columns} WHERE id = ?", (*fields.values(), screen_id))

    def delete_screen(self, screen_id: int) -> None:
        self.db.execute("DELETE FROM screens WHERE id = ?", (screen_id,))

    @staticmethod
    def _screen(row: sqlite3.Row) -> dict[str, Any]:
        screen = dict(row)
        screen["info"] = json.loads(screen["info"] or "{}")
        screen["config"] = json.loads(screen.get("config") or "{}")
        return screen

    # Groups

    def groups(self) -> list[dict[str, Any]]:
        groups = [dict(r) for r in self.db.execute("SELECT id, name FROM groups ORDER BY name, id")]
        members: dict[int, list[int]] = {}
        for row in self.db.execute("SELECT group_id, screen_id FROM group_screens ORDER BY screen_id"):
            members.setdefault(row[0], []).append(row[1])
        for group in groups:
            group["screens"] = members.get(group["id"], [])
        return groups

    def group(self, group_id: int) -> dict[str, Any] | None:
        return next((g for g in self.groups() if g["id"] == group_id), None)

    def save_group(self, group_id: int | None, name: str, screen_ids: list[int]) -> int:
        with self.db:
            self.db.execute("BEGIN")
            if group_id is None:
                group_id = int(self.db.execute("INSERT INTO groups (name, created) VALUES (?, ?)",
                                               (name, time.time())).lastrowid)
            else:
                self.db.execute("UPDATE groups SET name = ? WHERE id = ?", (name, group_id))
                self.db.execute("DELETE FROM group_screens WHERE group_id = ?", (group_id,))
            self.db.executemany("INSERT INTO group_screens (group_id, screen_id) VALUES (?, ?)",
                                [(group_id, s) for s in dict.fromkeys(screen_ids)])
        return group_id

    def delete_group(self, group_id: int) -> None:
        self.db.execute("DELETE FROM groups WHERE id = ?", (group_id,))

    # Media library

    def media(self) -> list[dict[str, Any]]:
        return [self._media(r) for r in self.db.execute("SELECT * FROM media ORDER BY created DESC, id DESC")]

    def media_item(self, media_id: int) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM media WHERE id = ?", (media_id,)).fetchone()
        return self._media(row) if row else None

    def media_by_token(self, token: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM media WHERE token = ?", (token,)).fetchone()
        return self._media(row) if row else None

    def add_media(self, name: str, size: int) -> tuple[int, str]:
        token = secrets.token_urlsafe(18)
        cur = self.db.execute(
            "INSERT INTO media (name, token, size, status, created) VALUES (?, ?, ?, 'waiting', ?)",
            (name, token, size, time.time()),
        )
        return int(cur.lastrowid), token

    def update_media(self, media_id: int, **fields: Any) -> None:
        allowed = {"name", "size", "status", "progress", "kind", "duration", "playable", "summary", "error"}
        if not fields or set(fields) - allowed:
            raise ValueError(f"cannot update {sorted(set(fields) - allowed)}")
        if "summary" in fields and not isinstance(fields["summary"], (str, type(None))):
            fields["summary"] = json.dumps(fields["summary"], sort_keys=True)
        columns = ", ".join(f"{name} = ?" for name in fields)
        self.db.execute(f"UPDATE media SET {columns} WHERE id = ?", (*fields.values(), media_id))

    def delete_media(self, media_id: int) -> None:
        self.db.execute("DELETE FROM media WHERE id = ?", (media_id,))

    @staticmethod
    def _media(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["summary"] = json.loads(item["summary"]) if item["summary"] else None
        return item

    # Pages: a web page by its address, or a playlist of media the console plays.

    def pages(self) -> list[dict[str, Any]]:
        return [self._page(r) for r in self.db.execute("SELECT * FROM pages ORDER BY name, id")]

    def page(self, page_id: int) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM pages WHERE id = ?", (page_id,)).fetchone()
        return self._page(row) if row else None

    def page_by_token(self, token: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM pages WHERE token = ?", (token,)).fetchone()
        return self._page(row) if row else None

    def save_page(self, page_id: int | None, *, name: str, kind: str, url: str | None,
                  items: list[dict[str, Any]]) -> int:
        if page_id is None:
            cur = self.db.execute(
                "INSERT INTO pages (name, kind, url, items, token, created) VALUES (?, ?, ?, ?, ?, ?)",
                (name, kind, url, json.dumps(items), secrets.token_urlsafe(18), time.time()),
            )
            return int(cur.lastrowid)
        self.db.execute("UPDATE pages SET name = ?, kind = ?, url = ?, items = ? WHERE id = ?",
                        (name, kind, url, json.dumps(items), page_id))
        return page_id

    def delete_page(self, page_id: int) -> None:
        self.db.execute("DELETE FROM pages WHERE id = ?", (page_id,))

    def screens_with_page(self, page_id: int) -> list[int]:
        return [r[0] for r in self.db.execute("SELECT id FROM screens WHERE home_page = ?", (page_id,))]

    def set_home_page(self, screen_id: int, page_id: int | None) -> None:
        self.db.execute("UPDATE screens SET home_page = ? WHERE id = ?", (page_id, screen_id))

    @staticmethod
    def _page(row: sqlite3.Row) -> dict[str, Any]:
        page = dict(row)
        page["items"] = json.loads(page["items"] or "[]")
        return page

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

    def add_user(self, username: str, name: str, password_hash: str, *, admin: bool = False,
                 must_change: bool = False) -> int:
        cur = self.db.execute(
            "INSERT INTO users (username, name, password, admin, created, must_change) VALUES (?, ?, ?, ?, ?, ?)",
            (username, name, password_hash, int(admin), time.time(), int(must_change)),
        )
        return int(cur.lastrowid)

    def delete_user(self, user_id: int) -> None:
        self.db.execute("DELETE FROM users WHERE id = ?", (user_id,))

    def user(self, user_id: int) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def user_by_name(self, username: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return dict(row) if row else None

    def set_password(self, user_id: int, password_hash: str, *, must_change: bool = False) -> None:
        """A new password ends every session of the user."""
        with self.db:
            self.db.execute("BEGIN")
            self.db.execute("UPDATE users SET password = ?, must_change = ? WHERE id = ?",
                            (password_hash, int(must_change), user_id))
            self.db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    # Two-factor sign-in. Secrets are sealed with the master key, bound to the user.

    def _seal_totp(self, user_id: int, secret: str) -> str:
        return seal(self.master, secret, f"totp:{user_id}")

    def totp_secret(self, user: dict[str, Any], *, pending: bool = False) -> str | None:
        sealed = user["totp_pending" if pending else "totp"]
        return open_sealed(self.master, sealed, f"totp:{user['id']}") if sealed else None

    def set_totp_pending(self, user_id: int, secret: str) -> None:
        self.db.execute("UPDATE users SET totp_pending = ? WHERE id = ?", (self._seal_totp(user_id, secret), user_id))

    def enable_totp(self, user_id: int, step: int, recovery_hashes: list[str]) -> None:
        with self.db:
            self.db.execute("BEGIN")
            self.db.execute("UPDATE users SET totp = totp_pending, totp_pending = NULL, totp_step = ? WHERE id = ?",
                            (step, user_id))
            self._replace_recovery(user_id, recovery_hashes)

    def disable_totp(self, user_id: int) -> None:
        with self.db:
            self.db.execute("BEGIN")
            self.db.execute("UPDATE users SET totp = NULL, totp_pending = NULL, totp_step = 0 WHERE id = ?", (user_id,))
            self.db.execute("DELETE FROM recovery_codes WHERE user_id = ?", (user_id,))

    def set_totp_step(self, user_id: int, step: int) -> None:
        self.db.execute("UPDATE users SET totp_step = ? WHERE id = ?", (step, user_id))

    def replace_recovery(self, user_id: int, hashes: list[str]) -> None:
        with self.db:
            self.db.execute("BEGIN")
            self._replace_recovery(user_id, hashes)

    def _replace_recovery(self, user_id: int, hashes: list[str]) -> None:
        self.db.execute("DELETE FROM recovery_codes WHERE user_id = ?", (user_id,))
        self.db.executemany("INSERT INTO recovery_codes (user_id, hash) VALUES (?, ?)",
                            [(user_id, h) for h in hashes])

    def use_recovery(self, user_id: int, code_hash: str) -> bool:
        cur = self.db.execute("DELETE FROM recovery_codes WHERE user_id = ? AND hash = ?", (user_id, code_hash))
        return cur.rowcount == 1

    def recovery_left(self, user_id: int) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM recovery_codes WHERE user_id = ?", (user_id,)).fetchone()[0])

    # Console settings

    def setting(self, key: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        self.db.execute("INSERT INTO settings (key, value) VALUES (?, ?)"
                        " ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, json.dumps(value)))

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

    def remove_member(self, user_id: int, tenant_id: str) -> None:
        self.db.execute("DELETE FROM members WHERE user_id = ? AND tenant_id = ?", (user_id, tenant_id))

    def members(self, tenant_id: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT u.id, u.username, u.name, u.admin, u.totp IS NOT NULL AS two_factor, m.role,"
                " (SELECT COUNT(*) FROM members o WHERE o.user_id = u.id) AS tenants"
                " FROM members m JOIN users u ON u.id = m.user_id WHERE m.tenant_id = ? ORDER BY u.name",
                (tenant_id,),
            )
        ]

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
