"""A screen's sound: what Chromium plays, passed to ffmpeg in real time, with silence between.

Chromium plays into a pipe of its own: ALSA's file plugin (/etc/asound.conf, ``pleatview``
device), whose writes wait while the pipe is full. Reading it at the pace of a clock makes
Chromium play at that pace. ffmpeg gets one sample stream that never stops, so the
video never waits for sound that a silent page doesn't make.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
from pathlib import Path
import re
import shutil
import termios
import time

_LOGGER = logging.getLogger(__name__)

RATE = 48000
CHANNELS = 2
FRAME = 2 * CHANNELS  # bytes: 16-bit samples
TICK = 0.02  # seconds between passes
# Chromium's sound read ahead of the clock, which evens out its writes; the rest of what it
# plays waits in its own buffers until the clock reaches it.
AHEAD = 0.04
# What waits for ffmpeg when it's slow, at most; beyond that the oldest is dropped.
BACKLOG = 0.25
# Two pages: what waits in the pipe delays the sound, but each pass must find all it needs
# there, or the gap is filled with silence and the sound falls behind.
PIPE_SIZE = 8192
F_SETPIPE_SZ = 1031
FFMPEG_INPUT = ["-f", "s16le", "-ar", str(RATE), "-ac", str(CHANNELS)]
# Seconds the sound reaches ffmpeg after the picture it goes with (what waits in the pipe and
# is read ahead, measured with a clip that flashes and beeps together: about 0.06); ffmpeg
# shifts the picture by as much, so the two meet.
DELAY = 0.05
# Characters ALSA takes unquoted in a device's arguments.
_SAFE_PATH = re.compile(r"^/[A-Za-z0-9_./-]+$")


def alsa_device(fifo: Path) -> str | None:
    """Chromium's --alsa-output-device for this pipe (None if its path can't be passed)."""
    return f"pleatview:PIPE={fifo}" if _SAFE_PATH.match(str(fifo)) else None


class Sound:
    """Reads one Chromium's pipe and writes ffmpeg's input at RATE, until stopped."""

    def __init__(self, fifo: Path) -> None:
        self.fifo = fifo
        self.ffmpeg_input = -1  # the read end, ffmpeg's stdin
        self._chromium = -1
        self._ffmpeg = -1
        self._task: asyncio.Task[None] | None = None

    def open(self, owner: str | None = None) -> None:
        """Makes the pipe Chromium plays into, and the one ffmpeg reads; before both start."""
        self.fifo.unlink(missing_ok=True)
        os.mkfifo(self.fifo, 0o600)
        if owner:
            shutil.chown(self.fifo, owner, owner)
        # Open before Chromium does, which would otherwise wait for a reader.
        self._chromium = os.open(self.fifo, os.O_RDONLY | os.O_NONBLOCK)
        fcntl.fcntl(self._chromium, F_SETPIPE_SZ, PIPE_SIZE)
        self.ffmpeg_input, self._ffmpeg = os.pipe()
        fcntl.fcntl(self._ffmpeg, F_SETPIPE_SZ, PIPE_SIZE)
        os.set_blocking(self._ffmpeg, False)

    def start(self) -> None:
        """Once ffmpeg has its end: ours is closed, so ffmpeg sees the end when we stop."""
        os.close(self.ffmpeg_input)
        self.ffmpeg_input = -1
        self._task = asyncio.create_task(self._run(), name=f"sound {self.fifo}")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        for fd in (self._chromium, self._ffmpeg, self.ffmpeg_input):
            if fd >= 0:
                os.close(fd)
        self._chromium = self._ffmpeg = self.ffmpeg_input = -1
        self.fifo.unlink(missing_ok=True)

    async def _run(self) -> None:
        held = bytearray()  # Chromium's sound, read ahead
        backlog = bytearray()  # for ffmpeg
        # The clock starts when ffmpeg reads: a first bit of silence tells when it does.
        backlog += bytes(int(RATE * TICK) * FRAME)
        await self._wait_for_reader(backlog)
        start = time.monotonic()
        sent = 0  # frames
        while True:
            await asyncio.sleep(TICK)
            due = int((time.monotonic() - start) * RATE) - sent
            if due <= 0:
                continue
            self._read(held, due * FRAME + int(RATE * AHEAD) * FRAME - len(held))
            take = min(len(held) - len(held) % FRAME, due * FRAME)
            backlog += held[:take]
            del held[:take]
            backlog += bytes(due * FRAME - take)  # silence where Chromium had none
            sent += due
            excess = len(backlog) - int(RATE * BACKLOG) * FRAME
            if excess > 0:
                del backlog[:-(-excess // FRAME) * FRAME]  # whole frames, so samples stay aligned
            try:
                written = os.write(self._ffmpeg, backlog)
            except BlockingIOError:
                continue
            except BrokenPipeError:
                return  # ffmpeg stopped; the session starts everything again
            del backlog[:written]

    async def _wait_for_reader(self, backlog: bytearray) -> None:
        written = os.write(self._ffmpeg, backlog)
        del backlog[:written]
        while _unread(self._ffmpeg) > 0:
            await asyncio.sleep(0.005)

    def _read(self, held: bytearray, wanted: int) -> None:
        while wanted > 0:
            try:
                data = os.read(self._chromium, wanted)
            except BlockingIOError:
                return
            if not data:
                return  # Chromium plays nothing, and has closed its end
            held += data
            wanted -= len(data)


def _unread(fd: int) -> int:
    """Bytes waiting in a pipe."""
    buf = bytearray(4)
    fcntl.ioctl(fd, termios.FIONREAD, buf)
    return int.from_bytes(buf, "little")
