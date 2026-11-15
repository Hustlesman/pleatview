"""HDMI-CEC, through cec-ctl (v4l-utils, from the distribution): whether the TV is on or in
standby, whether it shows this screen's input or another, the TV remote's keys that reach this
screen, and a sound system's volume and mute. What the TV tells is what a controller keeping
proof of play counts on: a TV in standby, or on another input, shows nobody this screen.

The screen takes a playback device's address with its name, and never takes the TV's input by
itself: it says it's the active source only when the TV asks for this input (Set Stream Path),
or asks who is and this input is the one showing. A TV that doesn't speak CEC (or has it off)
answers nothing: then nothing is known, and nothing changes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable, Iterable

_LOGGER = logging.getLogger(__name__)

CEC_CTL = "cec-ctl"
POLL_EVERY = 60  # seconds between asking the TV whether it's on
ANSWER_SECONDS = 6
TV = 0

HEADER = re.compile(r"^(Received from|Transmit(?:ted)? (?:from|by)) (.+?) to (.+?) \((\d+) to (\d+)\):\s*(.*)$")
OPCODE = re.compile(r"^([A-Z][A-Z0-9_]+)(?: \(0x[0-9a-fA-F]+\))?:?$")
FIELD = re.compile(r"^\s+([a-z][a-z0-9-]*): (.+?)(?: \(0x[0-9a-fA-F]+\))?$")
PHYSICAL = re.compile(r"Physical Address\s*:\s*([0-9a-f]\.[0-9a-f]\.[0-9a-f]\.[0-9a-f])")
POWER = {"on": "on", "standby": "standby", "to-on": "on", "to-standby": "standby",
         "in transition standby to on": "on", "in transition on to standby": "standby"}


def parse(lines: Iterable[str]) -> list[dict[str, Any]]:
    """cec-ctl's account of messages: who to whom, the message (its opcode) and its fields."""
    out: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw in lines:
        line = raw.rstrip("\n")
        header = HEADER.match(line.strip())
        if header:
            current = {"received": header.group(1).startswith("Received"), "from": int(header.group(4)),
                       "to": int(header.group(5)), "opcode": None, "fields": {}}
            out.append(current)
            rest = OPCODE.match(header.group(6).strip()) if header.group(6).strip() else None
            if rest:
                current["opcode"] = rest.group(1)
            continue
        if current is None:
            continue
        if current["opcode"] is None:
            opcode = OPCODE.match(line.strip())
            if opcode:
                current["opcode"] = opcode.group(1)
            continue
        field = FIELD.match(line)
        if field:
            current["fields"][field.group(1)] = field.group(2).strip()
    return [m for m in out if m["opcode"]]


def physical(text: str) -> str | None:
    found = PHYSICAL.search(text)
    return found.group(1) if found and found.group(1) != "f.f.f.f" else None


class Cec:
    """Follows the TV over HDMI-CEC and tells the screen what changes: `power` (on, standby),
    `input` (this, other), `key` (a remote's key that reached this screen) and `audio` (a sound
    system's volume and mute)."""

    def __init__(self, tell: Callable[[str, Any], None], *, name: str = "PleatView",
                 devices: Iterable[Path] | None = None, run=None) -> None:  # noqa: ANN001 - async (*args) -> (code, text)
        self.tell = tell
        self.name = name[:14]  # a CEC name is at most 14 characters
        self.devices = list(devices) if devices is not None else sorted(Path("/dev").glob("cec[0-9]*"))
        self.run = run or self._run
        self.device: Path | None = None
        self.address: str | None = None  # this screen's physical address on the TV's inputs
        self.input: str | None = None
        self._follower: asyncio.subprocess.Process | None = None
        self._tasks: list[asyncio.Task] = []

    @staticmethod
    async def _run(*args: str, timeout: float = ANSWER_SECONDS) -> tuple[int, str]:
        try:
            proc = await asyncio.create_subprocess_exec(CEC_CTL, *args, stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.STDOUT)
        except OSError:
            return 127, ""  # no cec-ctl here
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            return 124, ""
        return proc.returncode or 0, out.decode(errors="replace")

    async def start(self) -> bool:
        """Finds the adapter on a TV's input and takes a playback device's address on it;
        False where there's none (no CEC, nothing plugged in)."""
        for device in self.devices:
            code, text = await self.run("-d", str(device))
            if code == 0 and physical(text):
                self.device, self.address = device, physical(text)
                break
        if not self.device:
            return False
        await self.run("-d", str(self.device), "--playback", "--osd-name", self.name)
        _LOGGER.info("HDMI-CEC on %s, this input %s", self.device, self.address)
        self._tasks = [asyncio.create_task(self._follow()), asyncio.create_task(self._poll())]
        return True

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self._follower and self._follower.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._follower.kill()

    async def _poll(self) -> None:
        while True:
            code, text = await self.run("-d", str(self.device), "--to", str(TV), "--give-device-power-status")
            answered = [m for m in parse(text.splitlines()) if m["received"] and m["opcode"] == "REPORT_POWER_STATUS"]
            if answered:
                self.on_message(answered[-1])
            await asyncio.sleep(POLL_EVERY)

    async def _follow(self) -> None:
        """What the TV and the others on HDMI say, as it comes (cec-ctl --wait-for-msgs, which
        needs no more than the video group)."""
        while True:
            try:
                self._follower = await asyncio.create_subprocess_exec(
                    CEC_CTL, "-d", str(self.device), "--wait-for-msgs", stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL)
            except OSError:
                return
            block: list[str] = []
            assert self._follower.stdout
            async for raw in self._follower.stdout:
                line = raw.decode(errors="replace")
                if HEADER.match(line.strip()) and block:
                    for message in parse(block):
                        self.on_message(message)
                    block = []
                block.append(line)
                if not line.strip() and block:
                    for message in parse(block):
                        self.on_message(message)
                    block = []
            await self._follower.wait()
            await asyncio.sleep(5)  # it ended: again, in a moment

    def on_message(self, message: dict[str, Any]) -> None:
        if not message["received"]:
            return
        opcode, fields, sender = message["opcode"], message["fields"], message["from"]
        if opcode == "STANDBY" and sender == TV:
            self.tell("power", "standby")
        elif opcode == "REPORT_POWER_STATUS" and sender == TV:
            power = POWER.get(fields.get("pwr-state", ""))
            if power:
                self.tell("power", power)
        elif opcode in ("ACTIVE_SOURCE", "ROUTING_CHANGE", "ROUTING_INFORMATION", "SET_STREAM_PATH"):
            shown = fields.get("phys-addr") or fields.get("new-phys-addr")
            if shown:
                self._input("this" if shown == self.address else "other")
                if opcode == "SET_STREAM_PATH" and shown == self.address:
                    asyncio.ensure_future(self._active())  # the TV asks for this input
        elif opcode == "REQUEST_ACTIVE_SOURCE" and self.input == "this":
            asyncio.ensure_future(self._active())
        elif opcode == "USER_CONTROL_PRESSED":
            key = fields.get("ui-cmd")
            if key:
                self.tell("key", {"key": key.split(" (")[0].strip(), "from": "tv_remote", "at": round(time.time(), 3)})
        elif opcode == "REPORT_AUDIO_STATUS":
            muted = fields.get("aud-mute-status")
            volume = fields.get("aud-vol-status")
            self.tell("audio", {"muted": muted == "on" if muted else None,
                                "volume": int(volume) if volume and volume.isdigit() else None})

    def _input(self, which: str) -> None:
        if which != self.input:
            self.input = which
            self.tell("input", which)

    async def _active(self) -> None:
        if self.device and self.address:
            await self.run("-d", str(self.device), "--active-source", f"phys-addr={self.address}")
