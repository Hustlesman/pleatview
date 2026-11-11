"""The screen: what it shows, its state, and the commands it takes (docs/protocol.md).

A thin agent: Home Assistant renders the dashboards; this plays the stream, follows which
dashboard should show (and reports it, so Home Assistant renders that one), and plays media
and notifications on top. Nothing else runs here.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any, Callable

from .config import Config, is_http
from .player import Player
from .playlist import MediaCache

_LOGGER = logging.getLogger(__name__)

FEATURES = ["render_stream", "home_without_ha", "home_playlist"]
STREAM_RETRY = 2.0
PIN_SHOWN = 120  # seconds the PIN stays up after someone starts pairing


def _number(value: Any, low: float, high: float, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} must be a number from {low:g} to {high:g}")
    return float(value)


class Screen:
    def __init__(self, *, config: Config, save_config: Callable[[Config], None], player: Player,
                 setup: Callable[[], dict[str, str]], version: str, size: tuple[int, int],
                 cache: MediaCache | None = None) -> None:
        self.config = config
        self.save_config = save_config
        self.player = player
        self.setup = setup  # name, address, port, pin
        self.listeners: set[asyncio.Queue] = set()
        self.target: str | None = None  # what Home Assistant should render (a path or URL)
        self.media: dict[str, Any] | None = None
        self.standby = False
        self.pin_until = 0.0
        self.playing: str | None = None  # "stream", "playlist" or "media"
        self.playlist_index = 0
        self.playlist_failures = 0
        self.cache = cache
        self._timers: dict[str, asyncio.TimerHandle] = {}
        self._rotation: asyncio.Task | None = None
        self.state: dict[str, Any] = {
            "foreground": True, "power": "on", "view": "setup", "url": None, "dashboard": None,
            "ha_connected": None, "auth_error": False, "last_error": None, "overlay": None, "media": None,
            "volume": 1.0, "muted": False, "config_rev": config.rev, "overlay_permission": True,
            "sign_in": None, "battery": None, "app_version": version,
            "screen": {"width": size[0], "height": size[1], "density": 1.0}, "features": FEATURES,
        }

    # State

    def update(self, **changes: Any) -> None:
        if all(self.state.get(k) == v for k, v in changes.items()):
            return
        self.state.update(changes)
        for queue in list(self.listeners):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(dict(self.state))

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=20)
        self.listeners.add(queue)
        return queue

    # What shows

    async def show(self) -> None:
        """Puts the right thing on the screen: setup, the stream, or nothing (standby)."""
        config = self.config
        self.update(view="dashboard" if config.configured else "setup", config_rev=config.rev)
        if self.standby:
            await self.player.blank()
            return
        if self.media:
            return
        if not config.configured or time.monotonic() < self.pin_until:
            await self.player.blank()
            await self.player.text(self.setup_text(), PIN_SHOWN if config.configured else 86400)
            if not config.configured:
                return
        if config.home_playlist and self.at_home:
            await self.play_playlist()
            return
        if config.render_stream:
            self.playing = "stream"
            await self.player.play(config.render_stream, live=True)
            if time.monotonic() >= self.pin_until:
                await self.player.clear_text()
        else:
            self.playing = None
            await self.player.blank()
            await self.player.text(
                "PleatView\n\nThis screen plays dashboards that Home Assistant renders.\n"
                "In Home Assistant, open this screen's options and choose a renderer\n"
                "under Render on Home Assistant.", 86400)

    @property
    def at_home(self) -> bool:
        return self.target in (None, "", self.config.home_path)

    async def play_playlist(self) -> None:
        """The next item of the home playlist, from the cache when it's there."""
        items = self.config.home_playlist
        item = items[self.playlist_index % len(items)]
        self.playing = "playlist"
        source = self.cache.source(item["url"]) if self.cache else item["url"]
        if time.monotonic() >= self.pin_until:
            await self.player.clear_text()
        await self.player.play(source, image_seconds=item["seconds"] if item["kind"] == "image" else None)

    async def _playlist_next(self, failed: bool) -> None:
        items = self.config.home_playlist
        if not items or self.playing != "playlist":
            return
        self.playlist_failures = self.playlist_failures + 1 if failed else 0
        self.playlist_index = (self.playlist_index + 1) % len(items)
        if self.playlist_failures >= len(items):  # nothing plays (offline, nothing cached yet)
            self.playlist_failures = 0
            self._later("playlist", 15, self.show)
        elif failed:
            self._later("playlist", 1, self.show)
        else:
            await self.show()

    def setup_text(self) -> str:
        s = self.setup()
        pin = f"{s['pin'][:3]} {s['pin'][3:]}"
        return (f"PleatView\n\nIn Home Assistant: Settings → Devices & services → PleatView\n"
                f"{s['name']} · {s['address']}:{s['port']}\n\nPIN  {pin}")

    async def pin_requested(self) -> None:
        """Someone asked who this screen is, which comes before pairing: show the PIN a while."""
        if time.monotonic() < self.pin_until or self.standby:
            return
        self.pin_until = time.monotonic() + PIN_SHOWN
        await self.player.text(self.setup_text(), PIN_SHOWN)

    async def pin_changed(self) -> None:
        if not self.config.configured or time.monotonic() < self.pin_until:
            await self.player.text(self.setup_text(), PIN_SHOWN if self.config.configured else 86400)

    async def on_player(self, event: dict[str, Any]) -> None:
        name = event.get("event")
        if name in ("ready", "restarted"):
            await self.show()
        elif name == "end-file" and event.get("reason") in ("eof", "error"):
            if self.media:
                self.media = None
                self.update(media=None)
                await self.show()
            elif self.playing == "playlist" and not self.standby:
                await self._playlist_next(failed=event.get("reason") == "error")
            elif self.config.render_stream and not self.standby:
                # The live stream stopped (Home Assistant restarted, or the renderer did).
                self._later("stream", STREAM_RETRY, self.show)
        elif name == "property-change" and self.media:
            prop, value = event.get("name"), event.get("data")
            if prop == "pause" and value is not None:
                self.media["state"] = "paused" if value else "playing"
            elif prop == "time-pos" and isinstance(value, (int, float)):
                self.media["position"] = round(value, 1)
            elif prop == "duration" and isinstance(value, (int, float)):
                self.media["duration"] = round(value, 1)
            else:
                return
            self.media["updated_at"] = time.time()
            if prop != "time-pos" or int(value or 0) % 5 == 0:
                self.update(media=dict(self.media))

    def _later(self, key: str, seconds: float, action: Callable[[], Any]) -> None:
        if handle := self._timers.pop(key, None):
            handle.cancel()
        loop = asyncio.get_running_loop()
        self._timers[key] = loop.call_later(seconds, lambda: asyncio.ensure_future(action()))

    # Which dashboard Home Assistant should render

    def go_to(self, target: str) -> None:
        was_home = self.at_home
        self._go_to(target)
        if self.config.home_playlist and was_home != self.at_home and not self.media:
            asyncio.ensure_future(self.show())  # between the playlist and a rendered page

    def _go_to(self, target: str) -> None:
        config = self.config
        url = config.resolve(target) if target else None
        self.target = target
        dashboard = target if target and not is_http(target) else (
            url[len(config.ha_url):] if url and config.ha_url and url.startswith(config.ha_url + "/") else None)
        self.update(url=url, dashboard=dashboard)

    async def home(self) -> None:
        if handle := self._timers.pop("hold", None):
            handle.cancel()
        await self.stop_media()
        self.go_to(self.config.home_path if self.config.configured else "")

    def restart_rotation(self) -> None:
        if self._rotation:
            self._rotation.cancel()
        if self.config.rotate_seconds and len(self.config.dashboards) > 1:
            self._rotation = asyncio.create_task(self._rotate())

    async def _rotate(self) -> None:
        while True:
            await asyncio.sleep(self.config.rotate_seconds)
            if "hold" not in self._timers and not self.media:
                self.cycle(1)

    def cycle(self, step: int) -> None:
        paths = [d["path"] for d in self.config.dashboards]
        if not paths:
            return
        index = paths.index(self.target) if self.target in paths else -1
        self.go_to(paths[(index + step) % len(paths)])

    # Configuration

    async def configure(self, update: dict[str, Any]) -> Config:
        old, new = self.config, self.config.merge(update)
        self.config = new
        self.save_config(new)
        if self.cache and new.home_playlist != old.home_playlist:
            self.playlist_index = 0
            self.cache.keep(new.home_playlist)
        if (old.home_path, old.ha_url) != (new.home_path, new.ha_url) or self.target is None:
            await self.home()
        self.restart_rotation()
        if (old.render_stream, old.configured, old.home_playlist) != (new.render_stream, new.configured, new.home_playlist):
            await self.show()
        self.update(config_rev=new.rev, view="dashboard" if new.configured else "setup")
        return new

    # Commands

    async def command(self, data: dict[str, Any]) -> None:
        name = data.get("command")
        duration = _number(data.get("duration"), 0, 86400, "duration")
        if name in ("show_dashboard", "show_url"):
            target = data.get("path") if name == "show_dashboard" else data.get("url")
            if not isinstance(target, str) or not target.strip():
                raise ValueError("path is required" if name == "show_dashboard" else "url is required")
            if name == "show_url" and not is_http(target):
                raise ValueError("url must start with http:// or https://")
            await self.stop_media()
            self.go_to(target.strip())
            if duration:
                self._later("hold", duration, self._end_hold)
        elif name in ("play_media", "show_image"):
            url = data.get("url")
            if not isinstance(url, str) or not url.startswith(("http://", "https://", "rtsp://", "rtsps://")):
                raise ValueError("url must start with http://, https://, rtsp:// or rtsps://")
            volume = _number(data.get("volume"), 0, 1, "volume")
            start = _number(data.get("start_position"), 0, 86400 * 7, "start_position")
            self.media = {"state": "buffering", "url": url, "mime_type": data.get("mime_type"),
                          "title": data.get("title"), "artist": data.get("artist"), "image_url": data.get("image_url"),
                          "mode": data.get("mode") or "fullscreen", "position": 0, "duration": None,
                          "updated_at": time.time()}
            self.update(media=dict(self.media))
            self.playing = "media"
            await self.player.clear_text()
            await self.player.play(url, loop=bool(data.get("loop")), volume=volume, start=start)
            if name == "show_image" and duration:
                self._later("media", duration, self.stop_media)
        elif name == "notify":
            message = data.get("message")
            if not isinstance(message, str) or not message:
                raise ValueError("message is required")
            title = data.get("title") if isinstance(data.get("title"), str) else ""
            await self.player.text(f"{title}\n{message}" if title else message, duration or 10)
        elif name in ("play", "pause"):
            await self.player.set("pause", name == "pause")
        elif name == "stop":
            await self.stop_media()
        elif name == "seek":
            position = _number(data.get("position"), 0, 86400 * 7, "position")
            await self.player.command("seek", position or 0, "absolute")
        elif name == "set_volume":
            level = _number(data.get("level"), 0, 1, "level")
            await self.player.set("volume", round((level or 0) * 100))
            self.update(volume=level)
        elif name == "mute":
            muted = bool(data.get("muted"))
            await self.player.set("mute", muted)
            self.update(muted=muted)
        elif name == "home":
            await self.home()
        elif name == "reload":
            await self.show()
        elif name in ("next_dashboard", "previous_dashboard"):
            self.cycle(1 if name == "next_dashboard" else -1)
        elif name == "turn_off":
            self.standby = True
            await self.stop_media(show=False)
            self.update(power="standby")
            await self.show()
        elif name == "turn_on":
            self.standby = False
            self.update(power="on")
            await self.show()
        elif name == "forget_sign_ins":
            pass  # nothing is signed in here: Home Assistant renders the pages
        else:
            raise ValueError(f"unknown command: {name}")

    async def _end_hold(self) -> None:
        self._timers.pop("hold", None)
        await self.home()

    async def stop_media(self, show: bool = True) -> None:
        if self.media:
            self.media = None
            self.update(media=None)
            if show:
                await self.show()
