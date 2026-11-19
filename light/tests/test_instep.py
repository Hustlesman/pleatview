"""A wall's piece kept in step (instep.py): the delay its playlist asks for behind the time its
frames were drawn, by mpv's timestamps and this screen's clock."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pleatview_light import instep  # noqa: E402

PIECE = ("#EXTM3U\n#EXT-X-VERSION:6\n#EXT-X-TARGETDURATION:1\n#EXT-X-SERVER-CONTROL:HOLD-BACK=4.000\n"
         "#EXT-X-PLEATVIEW-CLOCK:1795060000.000\n#EXT-X-MEDIA-SEQUENCE:4\n#EXTINF:1.000000,\n"
         "#EXT-X-PROGRAM-DATE-TIME:2026-11-19T04:00:05.151Z\nt1-4.ts\n")


class Player:
    """mpv as instep.py sees it: the frame on screen's timestamp, moving with the clock."""

    def __init__(self, clock: list[float], position: float) -> None:
        self.clock, self.base, self.position = clock, clock[0], position
        self.calls: list[tuple] = []
        self.paused = False

    def time_pos(self) -> float:
        return self.position + (self.clock[0] - self.base)

    async def command(self, *args):
        self.calls.append(args)
        if args[:2] == ("get_property", "time-pos"):
            return {"data": self.time_pos()}
        if args[:2] == ("get_property", "pause"):
            return {"data": self.paused}
        if args[0] == "seek":
            self.position += args[1]
        return {}

    async def set(self, name, value):
        self.calls.append(("set", name, value))
        if name == "pause":
            self.paused = value


class Steps(unittest.TestCase):
    def test_the_walls_clock_from_the_playlist(self) -> None:
        self.assertEqual(instep.wall_clock(PIECE), (1795060000.0, 4.0))
        self.assertIsNone(instep.wall_clock("#EXTM3U\n#EXT-X-TARGETDURATION:1\ns1.ts\n"))  # a screen of its own

    def test_what_to_do(self) -> None:
        self.assertEqual(instep.step(4.0, 4), ("speed", 1.0))
        self.assertEqual(instep.step(4.01, 4), ("speed", 1.0))
        self.assertEqual(instep.step(4.2, 4), ("speed", 1.05))
        self.assertEqual(instep.step(3.9, 4), ("speed", 0.95))
        self.assertEqual(instep.step(6.5, 4), ("seek", 2.5))
        self.assertEqual(instep.step(3.0, 4), ("wait", 1.0))

    def test_timestamps_that_went_round(self) -> None:
        clock = 1795000000.0
        now = clock + instep.WRAP + 100
        self.assertAlmostEqual(instep.drawn_at(clock, 96.0, now), now - 4)  # mpv's timestamp after the wrap
        self.assertAlmostEqual(instep.drawn_at(clock, instep.WRAP + 96.0, now), now - 4)  # or counted on through it


class Loop(unittest.IsolatedAsyncioTestCase):
    async def test_a_piece_joined_early_waits_then_holds(self) -> None:
        clock = [1795060100.0]
        # On screen: the frame drawn 2.5 s ago (joined near the live edge): 1.5 s early.
        player = Player(clock, position=100.0 - 2.5)
        keeper = instep.InStep(player, "http://r/t1.m3u8", fetch_playlist=lambda url: PIECE, clock=lambda: clock[0])

        async def sleep(seconds):  # time passes as the loop waits
            clock[0] += seconds
        with mock.patch.object(instep.asyncio, "sleep", side_effect=sleep), mock.patch.object(instep.asyncio, "to_thread",
                                                                                                  side_effect=lambda f, *a: asyncio.sleep(0, f(*a))):
            await keeper.look(instep.wall_clock(PIECE))
        self.assertIn(("set", "pause", True), player.calls)
        self.assertEqual(player.calls[-1], ("set", "pause", False))
        # Paused for 1.5 s while the clock ran on: now 4 s behind.
        player.base = clock[0]
        player.position = 100.0 - 2.5
        self.assertAlmostEqual(clock[0] - (1795060000.0 + player.time_pos()), 4.0, places=3)

    async def test_a_late_piece_jumps_forward_and_a_slightly_late_one_speeds_up(self) -> None:
        clock = [1795060100.0]
        player = Player(clock, position=100.0 - 7)  # 3 s late
        keeper = instep.InStep(player, "x", fetch_playlist=lambda url: PIECE, clock=lambda: clock[0])
        await keeper.look(instep.wall_clock(PIECE))
        self.assertEqual(player.calls[2], ("seek", 3.0, "relative+exact"))
        player.position -= 0.1  # 0.1 s late
        await keeper.look(instep.wall_clock(PIECE))
        self.assertEqual(player.calls[-1], ("set", "speed", 1.05))
        player.position += 0.1
        await keeper.look(instep.wall_clock(PIECE))
        self.assertEqual(player.calls[-1], ("set", "speed", 1.0))

    async def test_a_stream_of_its_own_is_left_alone(self) -> None:
        player = Player([0.0], 0)
        keeper = instep.InStep(player, "x", fetch_playlist=lambda url: "#EXTM3U\ns1.ts\n")
        with mock.patch.object(instep, "START", 0):
            keeper.start()
            await asyncio.wait_for(keeper.task, 2)
        self.assertEqual(player.calls, [])

    async def test_it_stops_when_something_else_plays(self) -> None:
        playing = ["stream"]
        player = Player([1795060100.0], 96.0)
        keeper = instep.InStep(player, "x", fetch_playlist=lambda url: PIECE, active=lambda: playing[0] == "stream")
        with mock.patch.object(instep, "START", 0), mock.patch.object(instep, "LOOK", 0.01):
            keeper.start()
            await asyncio.sleep(0.1)
            playing[0] = "media"
            await asyncio.wait_for(keeper.task, 2)
        self.assertTrue(keeper.task.done())


if __name__ == "__main__":
    unittest.main()
