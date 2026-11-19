"""A wall's piece: one screen's part of a page drawn across several screens, each playing its
own stream. Its playlist says how far behind the time each segment was drawn to play
(EXT-X-SERVER-CONTROL:HOLD-BACK) and the time at its timestamp zero (EXT-X-PLEATVIEW-CLOCK).
mpv shows the timestamp of the frame on screen (time-pos, with rebase-start-time=no); kept
that far behind by this screen's clock (NTP), every screen of the wall shows the same frame.

Late: a little faster, or a jump forward when far off. Early: a pause for as long, as there's
nothing kept from before to go back to. A stream that isn't a wall's piece is left alone.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import urllib.request
from typing import Any

_LOGGER = logging.getLogger(__name__)

CLOCK = re.compile(r"^#EXT-X-PLEATVIEW-CLOCK:(-?\d+(?:\.\d+)?)\s*$", re.M)
HOLD_BACK = re.compile(r"^#EXT-X-SERVER-CONTROL:.*?\bHOLD-BACK=(\d+(?:\.\d+)?)", re.M)
WRAP = (1 << 33) / 90_000  # seconds: MPEG-TS timestamps go round every 26.5 hours
LOOK = 0.25  # seconds between looks
FETCH_AGAIN = 30  # seconds: the renderer starting again starts a new clock
START = 2  # seconds the stream has to start before it's looked at


def wall_clock(playlist: str) -> tuple[float, float] | None:
    """The piece's clock and the delay it asks for, or None if it isn't a wall's piece."""
    clock, hold = CLOCK.search(playlist), HOLD_BACK.search(playlist)
    return (float(clock.group(1)), float(hold.group(1))) if clock and hold else None


def drawn_at(clock: float, position: float, now: float) -> float:
    """When the frame at this timestamp was drawn: of the times the timestamps that go round
    could stand for, the one nearest now."""
    turns = round((now - clock - position) / WRAP)
    return clock + position + turns * WRAP


def step(behind: float, hold_back: float) -> tuple[str, float]:
    """What to do, from how far behind the frame on screen was drawn (seconds): ("speed", x),
    ("seek", seconds forward) or ("wait", seconds paused)."""
    off = behind - hold_back  # more than nothing: late
    if off > 1:
        return "seek", off
    if off < -0.25:
        return "wait", -off
    if abs(off) <= 0.015:  # within half a frame
        return "speed", 1.0
    return "speed", round(1 + max(-0.05, min(0.05, off / 2)), 3)


def fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310 - the stream's own address (http or https)
        return response.read(256 * 1024).decode(errors="replace")


class InStep:
    """Keeps mpv's live stream at the delay its playlist asks for, while it plays it."""

    def __init__(self, player: Any, url: str, fetch_playlist=fetch, clock=time.time, active=lambda: True) -> None:
        self.player, self.url, self.fetch, self.now, self.active = player, url, fetch_playlist, clock, active
        self.task: asyncio.Task | None = None
        self.speed = 1.0

    def start(self) -> None:
        self.task = asyncio.ensure_future(self._run())

    def stop(self) -> None:
        if self.task:
            self.task.cancel()
            self.task = None

    async def _run(self) -> None:
        found: tuple[float, float] | None = None
        fetched = -FETCH_AGAIN
        try:
            await asyncio.sleep(START)  # the stream starting first
            while self.active():
                if time.monotonic() - fetched >= FETCH_AGAIN:
                    try:
                        text = await asyncio.to_thread(self.fetch, self.url)
                    except (OSError, ValueError):
                        fetched = time.monotonic() - FETCH_AGAIN + 5  # again in a few seconds; meanwhile as it was
                    else:
                        fetched = time.monotonic()
                        found = wall_clock(text)
                        if found is None:  # not a wall's piece: left alone
                            if self.speed != 1.0:
                                self.speed = 1.0
                                await self.player.set("speed", 1.0)
                            return
                await self.look(found)
                await asyncio.sleep(LOOK)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - mpv restarting: the stream is shown again, and kept in step again
            _LOGGER.info("Keeping the stream in step stopped: %s", err)

    async def look(self, found: tuple[float, float] | None) -> None:
        if found is None or not self.active():
            return
        answer = await self.player.command("get_property", "time-pos")
        position = answer.get("data")
        paused = (await self.player.command("get_property", "pause")).get("data")
        if not isinstance(position, (int, float)) or paused:
            return
        now = self.now()
        clock, hold_back = found
        what, value = step(now - drawn_at(clock, position, now), hold_back)
        if what == "seek":
            await self.player.command("seek", value, "relative+exact")
            value, what = 1.0, "speed"
        elif what == "wait":
            await self.player.set("pause", True)
            await asyncio.sleep(value)
            await self.player.set("pause", False)
            value, what = 1.0, "speed"
        if value != self.speed:
            self.speed = value
            await self.player.set("speed", value)
