"""The screen's configuration (docs/protocol.md, "Configuration").

The light client takes every key Home Assistant sends, so it can be managed like any screen,
but only uses what a screen that plays a stream needs: the dashboards (to report which one it
shows, for Home Assistant to render), the stream, the name and the rotation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
import re
from typing import Any
from urllib.parse import urlsplit

KIOSK_MODES = ("full", "sidebar", "off")


def is_http(value: str) -> bool:
    return bool(re.match(r"^https?://", value, re.I))


def base_url(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if not is_http(value):
        raise ValueError("ha_url must start with http:// or https://")
    parts = urlsplit(value)
    if not parts.hostname:
        raise ValueError("ha_url is not a valid URL")
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


def target(value: str) -> str:
    value = value.strip()
    if not value or is_http(value):
        return value
    return value if value.startswith("/") else "/" + value


def _int(key: str, value: Any, low: int, high: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{key} must be a number")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be a number") from None
    if not low <= number <= high:
        raise ValueError(f"{key} must be between {low} and {high}")
    return number


def _bool(key: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be true or false")
    return value


def _str(key: str, value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{key} must be text")
    return value


@dataclass(frozen=True)
class Config:
    device_name: str = ""
    ha_url: str = ""
    ha_token: str = ""
    dashboards: list[dict[str, str]] = field(default_factory=list)
    home_dashboard: str = ""
    kiosk: str = "full"
    zoom: int = 100
    keep_screen_on: bool = True
    launch_on_boot: bool = True
    reload_minutes: int = 0
    rotate_seconds: int = 0
    return_home_minutes: int = 0
    reopen_minutes: int = 10
    render_stream: str = ""
    home_playlist: list[dict[str, Any]] = field(default_factory=list)
    # How the screen is mounted, degrees turned clockwise: the renderer turns the stream; this
    # screen turns its own text (the setup screen, the PIN).
    rotation: int = 0
    rev: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.ha_url) or is_http(self.home_dashboard) or bool(self.home_playlist)

    @property
    def home_path(self) -> str:
        return self.home_dashboard or (self.dashboards[0]["path"] if self.dashboards else "/")

    def resolve(self, path_or_url: str) -> str:
        value = path_or_url.strip()
        return value if is_http(value) else self.ha_url + target(value)

    def public(self) -> dict[str, Any]:
        data = asdict(self)
        data["ha_token_set"] = bool(data.pop("ha_token"))
        return data

    def merge(self, update: Any) -> Config:
        """A partial update. Unknown keys are refused; an update without `rev` clears it."""
        if not isinstance(update, dict):
            raise ValueError("config must be an object")
        changes: dict[str, Any] = {}
        for key, value in update.items():
            if key == "device_name":
                changes[key] = _str(key, value).strip()[:64]
            elif key == "ha_url":
                changes[key] = base_url(_str(key, value))
            elif key == "ha_token":
                changes[key] = _str(key, value).strip()
            elif key == "dashboards":
                if not isinstance(value, list) or len(value) > 50:
                    raise ValueError("dashboards must be a list of up to 50")
                boards = []
                for item in value:
                    if not isinstance(item, dict):
                        raise ValueError("each dashboard needs a title and a path")
                    path = target(_str("path", item.get("path", "")))
                    if path:
                        boards.append({"title": _str("title", item.get("title", path))[:64], "path": path})
                changes[key] = boards
            elif key == "home_dashboard":
                changes[key] = target(_str(key, value))
            elif key == "kiosk":
                if value not in KIOSK_MODES:
                    raise ValueError(f"kiosk must be one of {', '.join(KIOSK_MODES)}")
                changes[key] = value
            elif key == "zoom":
                changes[key] = _int(key, value, 25, 400)
            elif key in ("keep_screen_on", "launch_on_boot"):
                changes[key] = _bool(key, value)
            elif key == "reload_minutes":
                changes[key] = _int(key, value, 0, 7 * 24 * 60)
            elif key == "rotate_seconds":
                changes[key] = _int(key, value, 0, 24 * 60 * 60)
            elif key in ("return_home_minutes", "reopen_minutes"):
                changes[key] = _int(key, value, 0, 24 * 60)
            elif key == "render_stream":
                stream = _str(key, value).strip()
                if stream and not is_http(stream):
                    raise ValueError("render_stream must start with http:// or https://")
                changes[key] = stream
            elif key == "home_playlist":
                if not isinstance(value, list) or len(value) > 500:
                    raise ValueError("home_playlist must be a list of up to 500 items")
                items = []
                for i, item in enumerate(value):
                    if not isinstance(item, dict) or not is_http(str(item.get("url", ""))):
                        raise ValueError(f"playlist item {i} needs an http(s) url")
                    kind = item.get("kind", "video")
                    if kind not in ("video", "audio", "image"):
                        raise ValueError(f"playlist item {i}: kind must be video, audio or image")
                    seconds = _int(f"playlist item {i} seconds", item.get("seconds", 10), 1, 86400)
                    items.append({"url": item["url"], "kind": kind, "seconds": seconds})
                changes[key] = items
            elif key == "rotation":
                if value not in (0, 90, 180, 270) or isinstance(value, bool):
                    raise ValueError("rotation must be 0, 90, 180 or 270")
                changes[key] = value
            elif key == "rev":
                changes[key] = _str(key, value)[:64]
            elif key == "ha_token_set":
                continue  # read-only, echoed back by clients that round-trip GET /api/config
            else:
                raise ValueError(f"unknown config key: {key}")
        if "rev" not in update:
            changes["rev"] = ""
        merged = replace(self, **changes)
        if merged.ha_token and "ha_token" not in update and merged.ha_url != self.ha_url:
            merged = replace(merged, ha_token="")  # the token belongs to the Home Assistant it was made for
        return merged

    @classmethod
    def load(cls, data: Any) -> Config:
        data = data if isinstance(data, dict) else {}
        try:
            loaded = cls().merge({k: v for k, v in data.items() if k in cls.__dataclass_fields__ and k != "rev"})
        except ValueError:
            return cls()
        return replace(loaded, rev=str(data.get("rev", ""))[:64])

    def stored(self) -> dict[str, Any]:
        return asdict(self)
