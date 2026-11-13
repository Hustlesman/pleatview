"""Upkeep in PleatView Light (docs/protocol.md, "Upkeep"): its checks, its log without
secrets, and restarting and updating through the helper's folder."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pleatview_light import upkeep  # noqa: E402
from pleatview_light.api import Api, host_allowed  # noqa: E402
from pleatview_light.config import Config  # noqa: E402
from pleatview_light.pairing import Pairing  # noqa: E402
from pleatview_light.screen import Screen  # noqa: E402
from test_light import FakePlayer  # noqa: E402


class Log(unittest.TestCase):
    def test_queries_and_bearer_tokens_are_left_out(self) -> None:
        log = upkeep.RecentLog(size=2)
        logger = logging.getLogger("upkeep-test")
        logger.addHandler(log)
        logger.setLevel(logging.INFO)
        logger.info("Playing http://ha.local:8123/api/hls/abc/master.m3u8?authSig=SECRET and more")
        logger.warning("Sent Authorization: Bearer SECRET2 to https://x.test/a?b=c#d")
        logger.info("third")
        self.assertEqual(len(log.lines), 2)  # the oldest went
        first = log.lines[0]
        self.assertNotIn("SECRET", first)
        self.assertIn("WARNING Sent Authorization: Bearer … to https://x.test/a?…", first)
        self.assertEqual(upkeep.redact("Playing http://ha.local:8123/api/hls/abc/master.m3u8?authSig=SECRET now"),
                         "Playing http://ha.local:8123/api/hls/abc/master.m3u8?… now")
        self.assertEqual(upkeep.redact("rtsp://cam/stream?user=a&password=b"), "rtsp://cam/stream?…")

    def test_versions(self) -> None:
        self.assertTrue(upkeep.newer("1.0.20", "1.0.19"))
        self.assertTrue(upkeep.newer("1.1.0", "1.0.99"))
        self.assertFalse(upkeep.newer("1.0.19", "1.0.19"))
        self.assertFalse(upkeep.newer("1.0.9", "1.0.10"))


class HostNames(unittest.TestCase):
    def test_only_the_screens_own_names(self) -> None:
        for host in ("", "192.168.1.20", "192.168.1.20:8765", "[fe80::1]:8765", "[::1]", "localhost:8765", "pizero",
                     "pizero.local", "tv.lan:8765", "tv.home.arpa", "tv.internal", "PIZERO.LOCAL."):
            self.assertTrue(host_allowed(host), host)
        for host in ("rebind.evil.example", "rebind.evil.example:8765", "192.168.1.20.nip.io", "localhost.evil.example"):
            self.assertFalse(host_allowed(host), host)


class Diagnostics(unittest.IsolatedAsyncioTestCase):
    async def test_what_it_reports(self) -> None:
        log = upkeep.RecentLog()
        log.lines.append("a line")
        with tempfile.TemporaryDirectory() as tmp:
            report = await upkeep.diagnostics(home="http://127.0.0.1:9/", log=log, data=Path(tmp))
        for key in ("uptime", "app_uptime", "os", "kernel", "load", "processors", "memory", "storage",
                    "temperature", "power_warning", "throttled", "network", "gateway", "checks", "log"):
            self.assertIn(key, report)
        self.assertGreater(report["storage"]["total"], 0)
        self.assertEqual(report["log"], ["a line"])
        checks = {c["check"]: c for c in report["checks"]}
        self.assertTrue(checks["dns"]["ok"])
        self.assertFalse(checks["home"]["ok"])  # nothing listens on port 9
        self.assertEqual(checks["home"]["target"], "127.0.0.1:9")
        json.dumps(report)

    def test_the_boards_throttled_bits(self) -> None:
        self.assertEqual(upkeep._power(0x0), (False, False))
        self.assertEqual(upkeep._power(0xE0000), (False, True))  # slowed down by heat since it started
        self.assertEqual(upkeep._power(0x50005), (True, True))
        self.assertEqual(upkeep._power(0x10000), (True, False))

    async def test_no_home_check_without_a_home_page(self) -> None:
        checks = await upkeep.checks(None)
        self.assertNotIn("home", [c["check"] for c in checks])


class Helper(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run = Path(self.tmp.name)
        self.helper = upkeep.Helper(self.run)

    def test_ready_only_with_the_requests_folder(self) -> None:
        self.assertFalse(self.helper.ready)
        (self.run / "requests").mkdir()
        self.assertTrue(self.helper.ready)
        self.helper.update("1.0.20")
        self.helper.restart_device()
        self.helper.restart_device()  # asking twice is the same request
        self.assertEqual(sorted(os.listdir(self.run / "requests")), ["restart", "update-1.0.20"])
        with self.assertRaises(ValueError):
            self.helper.update("../../etc")

    def test_status_is_read_carefully(self) -> None:
        self.assertIsNone(self.helper.status())
        (self.run / "status.json").write_text('{"version": "1.0.20", "state": "failed", "error": "no release 1.0.20", "at": 5}')
        self.assertEqual(self.helper.status(), {"version": "1.0.20", "state": "failed", "error": "no release 1.0.20", "at": 5})
        (self.run / "status.json").write_text('{"version": "<b>", "state": "installed"}')
        self.assertIsNone(self.helper.status())
        (self.run / "status.json").write_text("not json")
        self.assertIsNone(self.helper.status())


class Commands(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.run = Path(self.tmp.name)
        self.restarted: list[bool] = []
        self.screen = Screen(config=Config(), save_config=lambda _: None, player=FakePlayer(),  # type: ignore[arg-type]
                             setup=lambda: {"name": "Zero", "address": "10.0.0.5", "port": "8765", "pin": "123456"},
                             version="1.0.19", size=(1280, 720), helper=upkeep.Helper(self.run),
                             restart_app=lambda: self.restarted.append(True))

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def test_restart_the_app(self) -> None:
        await self.screen.command({"command": "restart", "what": "app"})
        await asyncio.sleep(1.2)
        self.assertEqual(self.restarted, [True])
        with self.assertRaises(ValueError):
            await self.screen.command({"command": "restart", "what": "everything"})

    async def test_without_the_helper_device_and_update_are_refused(self) -> None:
        for command in ({"command": "restart", "what": "device"}, {"command": "update", "version": "1.0.20"}):
            with self.assertRaisesRegex(ValueError, "not_supported"):
                await self.screen.command(command)

    async def test_with_the_helper(self) -> None:
        (self.run / "requests").mkdir()
        await self.screen.command({"command": "restart", "what": "device"})
        with self.assertRaisesRegex(ValueError, "only newer"):
            await self.screen.command({"command": "update", "version": "1.0.19"})
        with self.assertRaisesRegex(ValueError, "like 1.0.20"):
            await self.screen.command({"command": "update", "version": "latest"})
        await self.screen.command({"command": "update", "version": "1.0.20"})
        self.assertEqual(sorted(os.listdir(self.run / "requests")), ["restart", "update-1.0.20"])
        self.assertEqual(self.screen.state["update"]["state"], "waiting")

    async def test_diagnostics_route(self) -> None:
        pairing = Pairing(lambda: None, lambda _: None)
        api = Api(self.screen, pairing, lambda: {"id": "abc", "name": "Zero"})

        async def report() -> dict:
            return {"uptime": 5}
        api.diagnostics = report
        self.assertEqual((await api.route("GET", "/api/diagnostics", {}, None))[0], 401)
        self.assertEqual(await api.route("GET", "/api/diagnostics", {}, None, trusted=True), (200, {"uptime": 5}))


if __name__ == "__main__":
    unittest.main()
