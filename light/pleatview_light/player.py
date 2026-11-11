"""mpv, which draws everything this screen shows, controlled over its JSON IPC socket.

One mpv runs the whole time, full screen on the display (DRM, no desktop), with the board's
hardware video decoder. It plays the stream Home Assistant renders, media, and shows text
(the setup screen, notifications) on its on-screen display.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import shlex
from typing import Any, Callable

_LOGGER = logging.getLogger(__name__)

# Zero-copy hardware decoding, with each decoded frame put straight on its own display plane
# (DRM PRIME) and mpv's drawing (text, pictures) on a plane above it. On a Raspberry Pi Zero
# 2 W this plays a 1080p stream with almost no dropped frames, where copying frames dropped
# two in three; and drawing the decoder's tiled frames with the GPU shows only black.
DEFAULT_OPTIONS = ("--vo=gpu --gpu-context=drm --hwdec=v4l2m2m --gpu-hwdec-interop=drmprime-overlay "
                   "--drm-draw-plane=overlay --drm-drmprime-video-plane=primary")


class Player:
    def __init__(self, socket_path: Path, on_event: Callable[[dict[str, Any]], None]) -> None:
        self.socket_path = socket_path
        self.on_event = on_event
        self.process: asyncio.subprocess.Process | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next = 0
        self._ready = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        if self.process and self.process.returncode is None:
            self.process.terminate()
            await self.process.wait()

    async def _run(self) -> None:
        """Keeps mpv running: starts it again if it stops."""
        options = shlex.split(os.environ.get("PLEATVIEW_LIGHT_MPV_OPTIONS", DEFAULT_OPTIONS))
        while True:
            self.socket_path.unlink(missing_ok=True)
            self.process = await asyncio.create_subprocess_exec(
                "mpv", "--idle=yes", "--force-window=yes", "--fs", "--no-terminal", "--no-config",
                "--keep-open=no", "--cursor-autohide=always", "--image-display-duration=inf",
                "--osd-font-size=40", "--osd-align-x=center", "--osd-align-y=center",
                "--osd-border-style=background-box", "--osd-back-color=#E60F1417", "--osd-color=#EDF3F6",
                "--osd-margin-y=60",
                "--cache=yes", "--demuxer-max-bytes=16MiB", f"--input-ipc-server={self.socket_path}", *options,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            try:
                await self._connect()
                await self.process.wait()
            finally:
                self._ready.clear()
                for future in self._pending.values():
                    future.cancel()
                self._pending.clear()
            _LOGGER.warning("mpv stopped (%s); starting it again", self.process.returncode)
            self.on_event({"event": "restarted"})
            await asyncio.sleep(2)

    async def _connect(self) -> None:
        for _ in range(100):
            if self.socket_path.exists():
                break
            await asyncio.sleep(0.1)
        reader, self.writer = await asyncio.open_unix_connection(str(self.socket_path))
        asyncio.create_task(self._read(reader))
        self._ready.set()
        for name in ("pause", "time-pos", "duration", "idle-active"):
            await self.command("observe_property", 1, name)
        self.on_event({"event": "ready"})

    async def _read(self, reader: asyncio.StreamReader) -> None:
        async for line in reader:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if "request_id" in message and message["request_id"] in self._pending:
                self._pending.pop(message["request_id"]).set_result(message)
            elif "event" in message:
                self.on_event(message)

    async def command(self, *args: Any) -> dict[str, Any]:
        await asyncio.wait_for(self._ready.wait(), 30)
        self._next += 1
        future = asyncio.get_running_loop().create_future()
        self._pending[self._next] = future
        assert self.writer
        self.writer.write(json.dumps({"command": list(args), "request_id": self._next}).encode() + b"\n")
        await self.writer.drain()
        return await asyncio.wait_for(future, 10)

    # What the screen does

    async def play(self, url: str, *, loop: bool = False, volume: float | None = None,
                   start: float | None = None, live: bool = False) -> None:
        options = [f"loop-file={'inf' if loop else 'no'}"]
        if start:
            options.append(f"start={start}")
        if live:
            options += ["cache-pause=no", "demuxer-readahead-secs=2"]
        await self.command("loadfile", url, "replace", -1, ",".join(options))
        await self.command("set_property", "pause", False)
        if volume is not None:
            await self.command("set_property", "volume", round(volume * 100))

    async def blank(self) -> None:
        await self.command("stop")

    async def text(self, message: str, seconds: float = 3600) -> None:
        await self.command("show-text", message, int(seconds * 1000))

    async def clear_text(self) -> None:
        await self.command("show-text", "", 1)

    async def set(self, name: str, value: Any) -> None:
        await self.command("set_property", name, value)
