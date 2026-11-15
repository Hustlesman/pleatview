"""HDMI-CEC (cec.py): cec-ctl's account of messages read, the adapter on a TV's input found and a
playback device's address taken, and what the TV says turned into what the screen reports:
power, which input it shows, its remote's keys and a sound system's volume."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pleatview_light.cec import Cec, parse, physical  # noqa: E402

# As cec-ctl 1.30 prints them: the header, then the message on a line of its own.
RECEIVED = """Received from TV to Playback Device 1 (0 to 4):
REPORT_POWER_STATUS (0x90):
	pwr-state: standby (0x01)
	Sequence: 0 Rx Timestamp: 7.123s

Received from TV to all (0 to 15):
STANDBY (0x36)
	Sequence: 0 Rx Timestamp: 9.5s

Received from TV to Playback Device 1 (0 to 4): USER_CONTROL_PRESSED (0x44):
	ui-cmd: volume-up (0x41)

Received from Audio System to Playback Device 1 (5 to 4):
REPORT_AUDIO_STATUS (0x7a):
	aud-mute-status: on (0x01)
	aud-vol-status: 23 (0x17)

Transmit from Playback Device 1 to TV (4 to 0):
GIVE_DEVICE_POWER_STATUS (0x8f)
	Sequence: 6 Tx Timestamp: 70158.664734s
	Tx, Not Acknowledged (4), Max Retries
"""

ADAPTER = """Driver Info:
	Driver Name                : vc4_hdmi
	Physical Address           : {}
	Logical Address Mask       : 0x0000
"""


class Reading(unittest.TestCase):
    def test_messages_in_either_layout(self) -> None:
        messages = parse(RECEIVED.splitlines())
        self.assertEqual([(m["received"], m["from"], m["to"], m["opcode"]) for m in messages], [
            (True, 0, 4, "REPORT_POWER_STATUS"), (True, 0, 15, "STANDBY"), (True, 0, 4, "USER_CONTROL_PRESSED"),
            (True, 5, 4, "REPORT_AUDIO_STATUS"), (False, 4, 0, "GIVE_DEVICE_POWER_STATUS")])
        self.assertEqual(messages[0]["fields"]["pwr-state"], "standby")
        self.assertEqual(messages[2]["fields"]["ui-cmd"], "volume-up")
        self.assertEqual(messages[3]["fields"], {"aud-mute-status": "on", "aud-vol-status": "23"})

    def test_the_adapter_on_a_tv(self) -> None:
        self.assertEqual(physical(ADAPTER.format("1.0.0.0")), "1.0.0.0")
        self.assertIsNone(physical(ADAPTER.format("f.f.f.f")))  # nothing plugged in


class Following(unittest.IsolatedAsyncioTestCase):
    def make(self) -> tuple[Cec, list, list]:
        told: list = []
        ran: list = []

        async def run(*args: str, timeout: float = 6) -> tuple[int, str]:
            ran.append(args)
            if args == ("-d", "/dev/cec0"):
                return 0, ADAPTER.format("f.f.f.f")
            if args == ("-d", "/dev/cec1"):
                return 0, ADAPTER.format("1.0.0.0")
            return 0, ""
        cec = Cec(lambda kind, value: told.append((kind, value)), devices=[Path("/dev/cec0"), Path("/dev/cec1")], run=run)
        return cec, told, ran

    async def test_takes_an_address_on_the_tvs_input_and_never_the_tvs_input(self) -> None:
        cec, _, ran = self.make()
        cec._follow = cec._poll = lambda: asyncio.sleep(0)  # type: ignore[method-assign]
        self.assertTrue(await cec.start())
        self.assertEqual((cec.device, cec.address), (Path("/dev/cec1"), "1.0.0.0"))
        self.assertIn(("-d", "/dev/cec1", "--playback", "--osd-name", "PleatView"), ran)
        self.assertFalse(any("--active-source" in a for a in ran))  # it doesn't take the TV's input
        await cec.stop()

    async def test_what_the_tv_says(self) -> None:
        cec, told, ran = self.make()
        cec.device, cec.address = Path("/dev/cec1"), "1.0.0.0"
        for message in parse(RECEIVED.splitlines()):
            cec.on_message(message)
        self.assertEqual([k for k, _ in told], ["power", "power", "key", "audio"])
        self.assertEqual(told[0], ("power", "standby"))
        self.assertEqual((told[2][1]["key"], told[2][1]["from"]), ("volume-up", "tv_remote"))
        self.assertEqual(told[3], ("audio", {"muted": True, "volume": 23}))
        told.clear()
        # Another device takes the TV: this screen isn't seen. The TV asks for this input: it
        # says it's the active source, and only then.
        cec.on_message({"received": True, "from": 8, "to": 15, "opcode": "ACTIVE_SOURCE", "fields": {"phys-addr": "2.0.0.0"}})
        self.assertEqual(told, [("input", "other")])
        cec.on_message({"received": True, "from": 0, "to": 15, "opcode": "REQUEST_ACTIVE_SOURCE", "fields": {}})
        await asyncio.sleep(0)
        self.assertFalse(any("--active-source" in a for a in ran))  # not showing: not it
        cec.on_message({"received": True, "from": 0, "to": 15, "opcode": "SET_STREAM_PATH", "fields": {"phys-addr": "1.0.0.0"}})
        await asyncio.sleep(0)
        self.assertEqual(told[-1], ("input", "this"))
        self.assertIn(("-d", "/dev/cec1", "--active-source", "phys-addr=1.0.0.0"), ran)


class Reporting(unittest.TestCase):
    def test_the_screen_reports_what_the_tv_says(self) -> None:
        from test_light import make_screen
        screen, player, _ = make_screen()
        screen.state["display"]["connected"] = True
        screen.on_cec("power", "standby")
        screen.on_cec("input", "other")
        screen.on_cec("audio", {"muted": True, "volume": 23})
        screen.on_cec("key", {"key": "mute", "from": "tv_remote", "at": 1.0})
        self.assertEqual(screen.state["display"], {"connected": True, "since": None, "power": "standby", "input": "other",
                                                   "audio": {"muted": True, "volume": 23}})
        self.assertEqual(screen.state["last_input"], {"key": "mute", "from": "tv_remote", "at": 1.0})
        self.assertEqual(player.calls, [])  # it plays on, whatever the TV says


if __name__ == "__main__":
    unittest.main()
