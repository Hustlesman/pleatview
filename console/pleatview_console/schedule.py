"""Schedules: which page a screen shows as its home page, by day and time, in its site's time zone.

An entry targets a screen, a group or a whole site. When several are on at once, the most
specific wins (a screen's own over its groups' over its site's), then the one added first.
Outside every entry, a screen shows its own home page.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
LEVEL = {"screen": 0, "group": 1, "site": 2}


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def valid_zone(name: Any) -> bool:
    if not isinstance(name, str) or not name or len(name) > 64:
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def minutes(value: str) -> int:
    hours, mins = value.split(":")
    return int(hours) * 60 + int(mins)


def is_on(entry: dict[str, Any], local: datetime) -> bool:
    """Whether the entry is on at this local time. An end before the start runs past midnight;
    the same start and end means the whole day."""
    start, end, now = minutes(entry["start"]), minutes(entry["end"]), local.hour * 60 + local.minute
    today = str(local.isoweekday()) in entry["days"]
    yesterday = str((local - timedelta(days=1)).isoweekday()) in entry["days"]
    if start == end:
        return today
    if start < end:
        return today and start <= now < end
    return (today and now >= start) or (yesterday and now < end)


def active(entries: list[dict[str, Any]], screen: dict[str, Any], groups: list[int], site_zone: str,
           now: datetime | None = None) -> dict[str, Any] | None:
    """The entry that decides the screen's home page now, or None."""
    local = (now or datetime.now(timezone.utc)).astimezone(zone(site_zone))
    mine = {f"screen:{screen['id']}", f"site:{screen['site_id']}", *(f"group:{g}" for g in groups)}
    candidates = [e for e in entries if e["target"] in mine and is_on(e, local)]
    if not candidates:
        return None
    return min(candidates, key=lambda e: (LEVEL[e["target"].split(":")[0]], e["id"]))
