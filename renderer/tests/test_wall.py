"""A wall: one page across several screens, drawn once and cut into a stream for each screen
(session.Tile), every stream with the same clock (wall.py). Run inside the renderer's image."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from aiohttp.test_utils import AioHTTPTestCase

from pleatview_renderer import server, session, wall
from pleatview_renderer.session import Screen, Tile

TWO = {"url": "http://a/wall", "width": 3840, "height": 1080, "scale": 2,
       "tiles": [{"x": 0, "y": 0, "width": 1920, "height": 1080, "sound": True},
                 {"x": 1920, "y": 0, "width": 1920, "height": 1080, "rotation": 90}]}


def segment_with(pts: int, sound: int | None = None) -> bytes:
    """The start of an MPEG-TS segment: its tables, then a picture's first packet stamped `pts`
    (and before it, with `sound`, a sound's stamped so)."""
    def packet(pid: int, payload: bytes, start: bool = True) -> bytes:
        head = bytes([0x47, (0x40 if start else 0) | (pid >> 8), pid & 0xFF, 0x10])
        return (head + payload).ljust(188, b"\xff")

    def stamp(value: int) -> bytes:
        return bytes([0x21 | ((value >> 29) & 0x0E), (value >> 22) & 0xFF, 0x01 | ((value >> 14) & 0xFE), (value >> 7) & 0xFF,
                      0x01 | ((value << 1) & 0xFE)])
    pat = bytes([0, 0x00, 0xB0, 13, 0, 1, 0xC1, 0, 0, 0, 1, 0xF0, 0x00]) + b"\0\0\0\0"  # program 1 -> PMT 0x1000
    pmt = bytes([0, 0x02, 0xB0, 23, 0, 1, 0xC1, 0, 0, 0xE1, 0x00, 0xF0, 0,
                 0x0F, 0xE1, 0x01, 0xF0, 0,   # AAC on 0x101
                 0x1B, 0xE1, 0x00, 0xF0, 0]) + b"\0\0\0\0"  # H.264 on 0x100
    out = packet(0, pat) + packet(0x1000, pmt)
    if sound is not None:
        out += packet(0x101, b"\x00\x00\x01\xc0\x00\x00\x80\x80\x05" + stamp(sound))
    return out + packet(0x100, b"\x00\x00\x01\xe0\x00\x00\x80\x80\x05" + stamp(pts))


class WallScreenTest(unittest.TestCase):
    def test_a_wall_is_checked(self) -> None:
        screen = server.parse_screen(TWO)
        self.assertEqual(screen.tiles, (Tile(0, 0, 1920, 1080, 0, True), Tile(1920, 0, 1920, 1080, 90, False)))
        self.assertEqual(screen.page_size, (3840, 1080))
        self.assertEqual(screen.tiles[1].size, (1080, 1920))
        self.assertEqual(server.weight(screen), 2)
        self.assertEqual(server.weight(Screen("http://a")), 1)
        self.assertTrue(screen.restart_needed(server.parse_screen({**TWO, "tiles": TWO["tiles"][::-1]})))
        one = TWO["tiles"][0]
        for bad in ({"tiles": [one]}, {"tiles": [one] * 17}, {"tiles": [one, {**one, "x": 3000}]},
                    {"tiles": [one, {**one, "sound": True}]}, {"tiles": [one, {**one, "rotation": 45}]},
                    {"tiles": [one, {**one, "width": "1920"}]}, {"tiles": [one, {**one, "width": 32}]},
                    {"width": 7682}, {"width": 7680, "height": 4320}, {"rotation": 90}, {"tiles": "two"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                server.parse_screen({**TWO, **bad})
        self.assertEqual(server.parse_screen({**TWO, "width": 7680, "height": 2160}).width, 7680)  # wider than a screen of its own may be

    def test_one_ffmpeg_cuts_the_pieces(self) -> None:
        screen = server.parse_screen(TWO)
        args = session.ffmpeg_args("x264", screen, ":10", Path("/f"), with_sound=True)
        self.assertEqual(args[args.index("-video_size") + 1], "3840x1080")
        self.assertEqual(args[args.index("-filter_complex") + 1],
                         "[0:v]split=2[c0][c1];[c0]crop=1920:1080:0:0[t0];[c1]crop=1920:1080:1920:0,transpose=2[t1]")
        self.assertEqual([args[i + 1] for i, a in enumerate(args) if a == "-map"], ["[t0]", "1:a", "[t1]"])  # sound with one piece
        self.assertIn("/f/t1-%d.ts", args)
        self.assertEqual(args[-1], "/f/t1.m3u8")
        self.assertTrue(all("program_date_time" in args[i + 1] for i, a in enumerate(args) if a == "-hls_flags"))
        self.assertEqual(args.count("-g"), 2)  # a keyframe every segment in each
        vaapi = session.ffmpeg_args("vaapi", screen, ":10", Path("/f"), with_sound=False)
        self.assertIn("transpose=2,format=nv12,hwupload[t1]", vaapi[vaapi.index("-filter_complex") + 1])
        self.assertNotIn("1:a", vaapi)
        # A screen of its own: as it was.
        single = session.ffmpeg_args("x264", Screen("http://a"), ":10", Path("/f"), with_sound=False)
        self.assertNotIn("-filter_complex", single)
        self.assertEqual(single[-1], "/f/index.m3u8")
        self.assertNotIn("program_date_time", single[single.index("-hls_flags") + 1])


class ClockTest(unittest.TestCase):
    PLAYLIST = ("#EXTM3U\n#EXT-X-VERSION:6\n#EXT-X-TARGETDURATION:1\n#EXT-X-MEDIA-SEQUENCE:4\n#EXT-X-INDEPENDENT-SEGMENTS\n"
                "#EXTINF:1.000000,\n#EXT-X-PROGRAM-DATE-TIME:2026-11-19T04:00:05.151+0000\nt1-4.ts\n"
                "#EXTINF:1.000000,\n#EXT-X-PROGRAM-DATE-TIME:2026-11-19T04:00:06.151+0000\nt1-5.ts\n"
                "#EXTINF:1.000000,\n#EXT-X-PROGRAM-DATE-TIME:2026-11-19T04:00:07.151+0000\nt1-6.ts\n")

    def test_the_first_pictures_timestamp(self) -> None:
        for pts in (0, 126_000, wall.WRAP - 1, 1 << 32):
            self.assertEqual(wall.first_pts(segment_with(pts)), pts)
        self.assertIsNone(wall.first_pts(b"not a stream" * 20))
        self.assertIsNone(wall.first_pts(segment_with(5)[:376]))  # tables only
        # With sound: the picture's for lining pieces up; the first sample's for the segment's time.
        self.assertEqual(wall.stamps(segment_with(9000, sound=7200)), {"video": 9000, "audio": 7200})
        self.assertEqual(wall.first_pts(segment_with(9000, sound=7200)), 9000)
        self.assertEqual(wall.earliest_pts(segment_with(9000, sound=7200)), 7200)
        self.assertEqual(wall.earliest_pts(segment_with(9000, sound=9900)), 9000)
        self.assertEqual(wall.earliest_pts(segment_with(100, sound=wall.WRAP - 900)), wall.WRAP - 900)  # just before the wrap

    def test_every_piece_gets_the_walls_time(self) -> None:
        pts = {"t1-4.ts": 9 * wall.TICKS, "t1-5.ts": 10 * wall.TICKS, "t1-6.ts": 11 * wall.TICKS}
        clock = wall.clock_from(self.PLAYLIST, pts.get)
        self.assertAlmostEqual(clock, wall._parse_time("2026-11-19T04:00:07.151+0000") - 11)
        out = wall.in_step(self.PLAYLIST, 1795060000.0, pts.get, now=1795060012.0)
        times = [line.split(":", 1)[1] for line in out.splitlines() if line.startswith("#EXT-X-PROGRAM-DATE-TIME")]
        self.assertEqual(times, [wall._format_time(1795060009.0), wall._format_time(1795060010.0), wall._format_time(1795060011.0)])
        self.assertTrue(times[0].endswith("Z"))
        self.assertIn("#EXT-X-SERVER-CONTROL:HOLD-BACK=4.000", out)
        self.assertIn("#EXT-X-PLEATVIEW-CLOCK:1795060000.000", out)
        self.assertEqual(out.count("#EXT-X-SERVER-CONTROL"), 1)
        self.assertEqual(wall.in_step(out, 1795060000.0, pts.get, now=1795060012.0), out)  # done twice: the same
        # A segment gone before it was read: its neighbour's time and its length.
        gone = wall.in_step(self.PLAYLIST, 1795060000.0, {"t1-5.ts": 10 * wall.TICKS}.get, now=1795060012.0)
        self.assertEqual([line.split(":", 1)[1] for line in gone.splitlines() if line.startswith("#EXT-X-PROGRAM")], times)

    def test_timestamps_wrap_every_day_or_so(self) -> None:
        clock = 1795000000.0
        later = clock + wall.WRAP / wall.TICKS + 30  # a day and a bit on: the timestamps came round
        self.assertAlmostEqual(wall.unwrapped(clock, 30 * wall.TICKS, now=later + 3), later)
        self.assertAlmostEqual(wall.unwrapped(clock, wall.WRAP - wall.TICKS, now=clock + wall.WRAP / wall.TICKS + 2),
                               clock + wall.WRAP / wall.TICKS - 1)


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg (the renderer's image has it)")
class RealPiecesTest(unittest.TestCase):
    def test_pieces_cut_from_one_picture_share_their_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            screen = server.parse_screen({**TWO, "width": 640, "height": 180,
                                          "tiles": [{"x": 0, "y": 0, "width": 320, "height": 180},
                                                    {"x": 320, "y": 0, "width": 320, "height": 180, "rotation": 270}]})
            args = session.ffmpeg_args("x264", screen, ":0", Path(folder), with_sound=False)
            at = args.index("-f")
            args[at:args.index("-i") + 2] = ["-re", "-f", "lavfi", "-i", "testsrc2=size=640x180:rate=30"]
            args[args.index("-loglevel") + 1] = "error"
            run = subprocess.Popen(["ffmpeg", "-t", "4.2", *args])
            run.wait(30)
            files = Path(folder)
            playlists = [(files / f"t{i}.m3u8").read_text() for i in (0, 1)]
            stamps = [[wall.first_pts((files / name).read_bytes()) for name, _, _ in wall.segments(p)] for p in playlists]
            self.assertEqual(stamps[0], stamps[1])
            self.assertGreaterEqual(len(stamps[0]), 3)

            def pts_of(name: str) -> int | None:
                return wall.first_pts((files / name).read_bytes())

            clock = wall.clock_from(playlists[0], pts_of)
            self.assertAlmostEqual(clock, time.time() - 4, delta=3)
            other = wall.piece_clock(clock, playlists[1], pts_of, lambda name: "t0-" + name.split("-", 1)[1])
            self.assertAlmostEqual(other, clock)
            times = [[line for line in wall.in_step(p, c, pts_of).splitlines() if line.startswith("#EXT-X-PROGRAM")]
                     for p, c in zip(playlists, (clock, other))]
            self.assertEqual(times[0], times[1])


class WallApiTest(AioHTTPTestCase):
    async def get_application(self):
        self.root = tempfile.TemporaryDirectory()
        patches = [mock.patch.object(session.Session, name, new=mock.AsyncMock()) for name in ("stop", "update")]
        patches.append(mock.patch.object(session.Session, "start", new=mock.Mock()))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.root.cleanup)
        return server.create_app(Path(self.root.name), "x264", max_sessions=3)

    async def test_a_wall_is_shown_and_its_pieces_served_in_step(self) -> None:
        response = await self.client.put("/api/sessions/lobby", json=TWO)
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {"playlists": ["/hls/lobby/t0.m3u8", "/hls/lobby/t1.m3u8"]})
        # It counts for two screens of its own: one more fits, not two.
        self.assertEqual((await self.client.put("/api/sessions/tv-1", json={"url": "http://a"})).status, 200)
        refused = await self.client.put("/api/sessions/tv-2", json={"url": "http://a"})
        self.assertEqual(refused.status, 429)
        self.assertEqual((await self.client.put("/api/sessions/lobby", json={**TWO, "width": 3840, "height": 2160})).status, 429)
        info = await (await self.client.get("/api/info")).json()
        self.assertEqual(info["sessions"]["lobby"]["screens"], 2)
        self.assertEqual((await self.client.post("/api/sessions/lobby/overlay", json={"title": "Hi"})).status, 400)

        folders = session.Folders(Path(self.root.name))
        folder = folders.stream(folders.name("lobby"))
        folder.mkdir(parents=True, exist_ok=True)
        for piece in (0, 1):
            (folder / f"t{piece}.m3u8").write_text(ClockTest.PLAYLIST.replace("t1-", f"t{piece}-").replace(
                "05.151", "05.151" if piece == 0 else "05.158"))  # each writer started a little apart
            for n in (4, 5, 6):  # the piece with the sound: its timestamps 22 ms on from the others', its sound 10 ms before its picture
                video = (5 + n) * wall.TICKS + (1980 if piece == 0 else 0)
                (folder / f"t{piece}-{n}.ts").write_bytes(segment_with(video, sound=video - 900 if piece == 0 else None))
        served = [await (await self.client.get(f"/hls/lobby/t{piece}.m3u8")).text() for piece in (0, 1)]
        times = [[wall._parse_time(line.split(":", 1)[1]) for line in text.splitlines() if line.startswith("#EXT-X-PROGRAM")] for text in served]
        self.assertEqual(len(times[1]), 3)
        for sound, picture in zip(*times):  # the same frame at the same moment: the sound's segment starts 10 ms sooner
            self.assertAlmostEqual(picture - sound, 0.010, places=3)
        self.assertIn("#EXT-X-PROGRAM-DATE-TIME:2026-11-19T04:00:05.141Z", served[0])
        clocks = [float(next(line for line in text.splitlines() if line.startswith("#EXT-X-PLEATVIEW-CLOCK:")).split(":")[1])
                  for text in served]
        self.assertAlmostEqual(clocks[1] - clocks[0], 0.022, places=3)  # each piece's own timestamps
        self.assertIn("HOLD-BACK", served[1])
        self.assertEqual((await self.client.get("/hls/lobby/t1-5.ts")).status, 200)
        # A wall has no index.m3u8, nor pieces it hasn't; a screen of its own has no pieces.
        self.assertEqual((await self.client.get("/hls/lobby/index.m3u8")).status, 404)
        self.assertEqual((await self.client.get("/hls/lobby/t2.m3u8")).status, 404)
        self.assertEqual((await self.client.get("/hls/tv-1/t0.m3u8")).status, 404)


class WallCallsTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_controller_elsewhere_gets_each_pieces_stream(self) -> None:
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        with mock.patch.object(session.Session, "start", new=mock.Mock()):
            renderer = server.Renderer(Path(root.name), "x264", max_sessions=4)
            calls = server.controller_calls(renderer, lambda: {}, lambda: 1080)
            shown = await calls["show"]({"key": "wall-3", **TWO})
            self.assertEqual(len(shown["streams"]), 2)
            self.assertTrue(shown["streams"][1].endswith("/t1.m3u8"))
            from pleatview_renderer.connect import Refused
            with self.assertRaises(Refused):
                await calls["overlay"]({"key": "wall-3", "title": "Hi"})
        self.assertIn("a wall of 2 screens", server.describe_call("show", {"key": "wall-3", **TWO}))


if __name__ == "__main__":
    unittest.main()
