"""One TV's screen on the renderer: an X display, Chromium on it, and ffmpeg encoding it and its
sound to HLS."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import itertools
import json
import logging
import os
from pathlib import Path
import shutil
import time
from typing import Any, Callable
from urllib.parse import urlsplit

import aiohttp

from . import actions, page, sound

_LOGGER = logging.getLogger(__name__)

# Seconds of video in each HLS segment, and how many segments the playlist keeps.
SEGMENT_SECONDS = 1
PLAYLIST_SEGMENTS = 6
# Frames per second of the stream: dashboards and live streams look smooth at 30.
FPS = 30
# Quality of the hardware encoder (lower is better): about 4 to 8 Mbit/s for a wall of videos at 1080p.
VAAPI_QP = 26
# The sound: AAC, which every TV plays.
AUDIO_ARGS = ["-c:a", "aac", "-b:a", "128k"]
_displays = itertools.count(10)
# The user that Xvfb and Chromium run as, when the renderer itself runs as root.
UNPRIVILEGED = os.environ.get("RENDER_USER", "render")


@dataclass(frozen=True)
class Screen:
    """What one TV shows: its page, size and sign-in."""

    url: str
    width: int = 1920
    height: int = 1080
    scale: float = 2.0  # CSS pixels to screen pixels, as on the TV (density × zoom)
    kiosk: str = "full"
    ha_url: str = ""
    token: str = field(default="", repr=False)

    @property
    def ha_origin(self) -> str:
        parts = urlsplit(self.ha_url)
        return f"{parts.scheme}://{parts.netloc}" if parts.scheme in ("http", "https") and parts.netloc else ""

    @property
    def page(self) -> tuple[str, tuple[actions.Step, ...]]:
        """The address to open, and the steps to take on it once it loads (actions.split)."""
        return actions.split(self.url)

    def restart_needed(self, other: Screen) -> bool:
        """Size and scale are fixed for a Chromium window; the rest changes in place."""
        return (self.width, self.height, self.scale) != (other.width, other.height, other.scale)


def bitrate(screen: Screen) -> int:
    """Bits per second: 6 Mbit/s for 1080p, scaled by area, at least 1.5 Mbit/s."""
    return max(1_500_000, int(6_000_000 * screen.width * screen.height / (1920 * 1080)))


def encoder_args(encoder: str, screen: Screen) -> list[str]:
    """ffmpeg's video encoding options: a keyframe every segment, so a TV can join at any."""
    gop = ["-g", str(FPS * SEGMENT_SECONDS)]
    if encoder == "vaapi":
        # Constant quality: the free VA-API drivers have no bitrate control for H.264.
        return ["-vf", "format=nv12,hwupload", "-c:v", "h264_vaapi", "-bf", "0", *gop, "-rc_mode", "CQP", "-qp", str(VAAPI_QP)]
    rate = bitrate(screen)
    return [
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency", "-pix_fmt", "yuv420p", *gop,
        "-keyint_min", str(FPS * SEGMENT_SECONDS), "-sc_threshold", "0",
        "-b:v", str(rate), "-maxrate", str(rate * 4 // 3), "-bufsize", str(rate * 2),
    ]


def ffmpeg_args(encoder: str, screen: Screen, display: str, folder: Path, with_sound: bool) -> list[str]:
    """The display, and the sound on standard input (sound.py), to HLS segments in folder."""
    sound_in = [*sound.FFMPEG_INPUT, "-i", "pipe:0"] if with_sound else []
    sound_out = ["-map", "0:v", "-map", "1:a", *AUDIO_ARGS] if with_sound else []
    return [
        "-loglevel", "error", "-nostdin",
        *(["-vaapi_device", "/dev/dri/renderD128"] if encoder == "vaapi" else []),
        # The picture waits for the sound that goes with it.
        *(["-itsoffset", str(sound.DELAY)] if with_sound else []),
        "-f", "x11grab", "-framerate", str(FPS), "-video_size", f"{screen.width}x{screen.height}",
        "-draw_mouse", "0", "-i", display, *sound_in,
        *encoder_args(encoder, screen), *sound_out,
        "-f", "hls", "-hls_time", str(SEGMENT_SECONDS), "-hls_list_size", str(PLAYLIST_SEGMENTS),
        "-hls_flags", "delete_segments+independent_segments+omit_endlist+temp_file",
        "-hls_segment_filename", str(folder / "s%d.ts"), str(folder / "index.m3u8"),
    ]


def chromium_args(screen: Screen, profile: Path, port: int, alsa_device: str | None = None) -> list[str]:
    return [
        # Chromium's own sandbox needs privileges a container doesn't have; it runs as an
        # unprivileged user instead (see _spawn).
        "--no-sandbox",
        "--kiosk",
        "--no-first-run",
        "--no-default-browser-check",
        "--noerrdialogs",
        "--disable-infobars",
        "--disable-session-crashed-bubble",
        "--disable-features=Translate,MediaRouter,DialMediaRouteProvider",
        "--hide-scrollbars",
        "--disable-dev-shm-usage",  # a Home Assistant add-on gets a small /dev/shm
        # The page's sound goes into the stream (sound.py), or nowhere.
        f"--alsa-output-device={alsa_device}" if alsa_device else "--mute-audio",
        "--autoplay-policy=no-user-gesture-required",
        "--password-store=basic",
        # WebGL (3D maps, Windy) drawn by Mesa on the processor (llvmpipe, through the display's
        # GLX): the display has no graphics chip, and Chromium otherwise leaves such pages blank.
        # Its list of unreliable drivers names every software one; the rest of the page stays
        # drawn as before, which costs half as much as drawing it all through the GL.
        "--use-gl=angle",
        "--use-angle=gl",
        "--ignore-gpu-blocklist",
        "--disable-gpu-compositing",
        "--disable-gpu-rasterization",
        # In Chromium's own pixels, which the scale factor enlarges to fill the display.
        f"--window-size={round(screen.width / screen.scale)},{round(screen.height / screen.scale)}",
        "--window-position=0,0",
        f"--force-device-scale-factor={screen.scale:g}",
        f"--user-data-dir={profile}",
        "--remote-debugging-address=127.0.0.1",
        f"--remote-debugging-port={port}",
        "about:blank",
    ]


class Session:
    """Renders one TV's screen until stopped; restarts what crashes."""

    def __init__(self, key: str, screen: Screen, root: Path, encoder: str) -> None:
        self.key = key
        self.screen = screen
        self.encoder = encoder
        self.folder = root / key
        self.display = next(_displays)
        self.port = 9300 + self.display
        self.last_access = time.monotonic()
        self._procs: list[asyncio.subprocess.Process] = []
        self._task: asyncio.Task[None] | None = None
        self._cdp: Cdp | None = None
        self._script_id: str | None = None
        self._ready = asyncio.Event()
        # Notifications and cameras on top of the page: kind -> (what, until).
        self.overlays: dict[str, tuple[dict[str, Any], float]] = {}
        self._overlay_task: asyncio.Task[None] | None = None
        self._steps_task: asyncio.Task[None] | None = None
        self._sound: sound.Sound | None = None

    @property
    def playlist(self) -> Path:
        return self.folder / "index.m3u8"

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if not self.running:
            self._ready.clear()
            self._task = asyncio.create_task(self._run(), name=f"render {self.key}")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self._kill()

    async def update(self, screen: Screen) -> None:
        """Shows a new page or sign-in; restarts for a new size."""
        old, self.screen = self.screen, screen
        if old.restart_needed(screen):
            await self.stop()
            self.start()
            return
        if self.running and self._ready.is_set() and self._cdp:
            try:
                if (old.ha_origin, old.token, old.kiosk) != (screen.ha_origin, screen.token, screen.kiosk):
                    await self._install_script()
                    await self._cdp.call("Page.navigate", url=screen.page[0])
                elif old.page[0] == screen.page[0] and old.page[1] != screen.page[1]:
                    await self._cdp.call("Page.reload")  # other steps for the same page: from the start
                elif old.url != screen.url:
                    await self._navigate(old.page[0], screen.page[0])
            except CdpError as err:
                _LOGGER.warning("%s: could not show %s (%s); starting again", self.key, screen.url, err)
                await self.stop()
                self.start()

    async def wait_ready(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
        except TimeoutError:
            return False
        return True

    # ---- internals ---------------------------------------------------------------

    async def _run(self) -> None:
        delay = 1
        while True:
            started = time.monotonic()
            try:
                await self._start_processes()
                self._ready.set()
                _LOGGER.info("%s: showing %s at %dx%d", self.key, self.screen.url, self.screen.width, self.screen.height)
                await self._wait_for_exit()
                _LOGGER.warning("%s: a process stopped; starting again", self.key)
            except (OSError, CdpError, TimeoutError) as err:
                _LOGGER.warning("%s: could not start (%s)", self.key, err)
            finally:
                self._ready.clear()
                await self._kill()
            delay = 1 if time.monotonic() - started > 60 else min(delay * 2, 30)
            await asyncio.sleep(delay)

    async def _start_processes(self) -> None:
        if self.folder.exists():
            shutil.rmtree(self.folder)
        self.folder.mkdir(parents=True)
        profile = self.folder.parent / "profiles" / self.key
        profile.mkdir(parents=True, exist_ok=True)
        if UNPRIVILEGED and os.geteuid() == 0:
            shutil.chown(profile, UNPRIVILEGED, UNPRIVILEGED)
        # A profile left by a crash would show Chromium's "restore pages?" question.
        for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            (profile / name).unlink(missing_ok=True)
        screen = self.screen
        display = f":{self.display}"
        self._procs.append(await _spawn(
            "Xvfb", display, "-screen", "0", f"{screen.width}x{screen.height}x24", "-nolisten", "tcp", "-nocursor",
        ))
        await _wait_for(lambda: Path(f"/tmp/.X11-unix/X{self.display}").exists(), 10)
        env = {**os.environ, "DISPLAY": display}
        env.pop("PULSE_SERVER", None)  # Chromium would play there instead of into the stream
        self._sound = self._open_sound(profile / "sound")
        device = sound.alsa_device(self._sound.fifo) if self._sound else None
        self._procs.append(await _spawn("chromium", *chromium_args(screen, profile, self.port, device), env=env))
        self._cdp = await Cdp.connect(self.port)
        self._cdp.on_event = self._on_event
        await self._cdp.call("Page.enable")
        await self._install_script()
        await self._cdp.call("Page.navigate", url=screen.page[0])
        self._procs.append(await _spawn(
            "ffmpeg", *ffmpeg_args(self.encoder, screen, display, self.folder, with_sound=self._sound is not None),
            stdin=self._sound.ffmpeg_input if self._sound else None,
        ))
        if self._sound:
            self._sound.start()

    def _open_sound(self, fifo: Path) -> sound.Sound | None:
        """The pipe Chromium plays into; without one, the stream is silent."""
        if not sound.alsa_device(fifo):
            _LOGGER.warning("%s: no sound: %s can't be passed to Chromium", self.key, fifo)
            return None
        result = sound.Sound(fifo)
        try:
            result.open(UNPRIVILEGED if UNPRIVILEGED and os.geteuid() == 0 else None)
        except OSError as err:
            _LOGGER.warning("%s: no sound (%s)", self.key, err)
            asyncio.ensure_future(result.stop())
            return None
        return result

    async def overlay(self, item: dict[str, Any] | None, kind: str = "") -> None:
        """Shows a notification or a camera on top of the page for its seconds (None clears)."""
        if item is None:
            if kind:
                self.overlays.pop(kind, None)
            else:
                self.overlays.clear()
        else:
            self.overlays[item["kind"]] = (item, time.monotonic() + item["duration"])
        await self._draw_overlays()
        if self.overlays and (self._overlay_task is None or self._overlay_task.done()):
            self._overlay_task = asyncio.create_task(self._keep_overlays())

    async def _draw_overlays(self) -> None:
        now = time.monotonic()
        self.overlays = {k: v for k, v in self.overlays.items() if v[1] > now}
        if not (self._cdp and self._ready.is_set()):
            return
        try:
            await self._cdp.call("Runtime.evaluate", expression=page.overlay_script([v[0] for v in self.overlays.values()]))
        except CdpError as err:
            _LOGGER.debug("%s: overlay not drawn: %s", self.key, err)

    async def _keep_overlays(self) -> None:
        """Draws them again after the page changes, and takes them away when their time is up."""
        while True:
            await asyncio.sleep(1)
            await self._draw_overlays()
            if not self.overlays:
                return

    def _on_event(self, method: str, params: dict[str, Any]) -> None:
        if method == "Page.loadEventFired":
            self._start_steps()

    def _start_steps(self) -> None:
        """Takes the page's steps (actions) from the start, after every load: a reload loses them."""
        if self._steps_task:
            self._steps_task.cancel()
            self._steps_task = None
        steps = self.screen.page[1]
        if steps and self._cdp:
            self._steps_task = asyncio.create_task(self._take_steps(self._cdp, steps))

    async def _take_steps(self, cdp: Cdp, steps: tuple[actions.Step, ...]) -> None:
        try:
            await actions.perform(cdp, steps, self.key)
            _LOGGER.info("%s: took the page's %d steps", self.key, len(steps))
        except CdpError as err:
            _LOGGER.info("%s: the page's steps stopped: %s", self.key, err)

    async def _install_script(self) -> None:
        assert self._cdp
        if self._script_id:
            await self._cdp.call("Page.removeScriptToEvaluateOnNewDocument", identifier=self._script_id)
        source = page.script(self.screen.ha_origin, self.screen.token, self.screen.kiosk)
        result = await self._cdp.call("Page.addScriptToEvaluateOnNewDocument", source=source)
        self._script_id = result.get("identifier")

    async def _navigate(self, old_url: str, url: str) -> None:
        """Within Home Assistant's frontend, switches dashboards in place, as the TV app does."""
        assert self._cdp
        origin = self.screen.ha_origin
        path = url[len(origin):] if origin and url.startswith(origin + "/") else None
        served = ("/local/", "/hacsfiles/", "/api/", "/auth/", "/static/", "/media/")
        if path and old_url.startswith(origin + "/") and not path.startswith(served):
            result = await self._cdp.call(
                "Runtime.evaluate",
                expression=f"!!(window.__pleatview && location.origin === {json.dumps(origin)}"
                f" && (window.__pleatview.navigate({json.dumps(path)}), true))",
                returnByValue=True,
            )
            if result.get("result", {}).get("value"):
                self._start_steps()  # no load in place
                return
        await self._cdp.call("Page.navigate", url=url)

    async def _wait_for_exit(self) -> None:
        waits = [asyncio.create_task(proc.wait()) for proc in self._procs]
        if self._cdp:
            waits.append(asyncio.create_task(self._cdp.closed.wait()))
        try:
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in waits:
                task.cancel()

    async def _kill(self) -> None:
        if self._steps_task:
            self._steps_task.cancel()
            self._steps_task = None
        if self._cdp:
            await self._cdp.close()
            self._cdp = None
        self._script_id = None
        for proc in reversed(self._procs):
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), 5)
                except TimeoutError:
                    proc.kill()
                    await proc.wait()
        self._procs.clear()
        if self._sound:
            await self._sound.stop()
            self._sound = None
        Path(f"/tmp/.X11-unix/X{self.display}").unlink(missing_ok=True)
        Path(f"/tmp/.X{self.display}-lock").unlink(missing_ok=True)


async def _spawn(program: str, *args: str, env: dict[str, str] | None = None, stdin: int | None = None) -> asyncio.subprocess.Process:
    command = [program, *args]
    if program in ("Xvfb", "chromium") and UNPRIVILEGED and os.geteuid() == 0:
        # What shows web pages runs without root; only ffmpeg keeps the GPU it encodes with.
        command = ["setpriv", f"--reuid={UNPRIVILEGED}", f"--regid={UNPRIVILEGED}", "--init-groups", "--", *command]
        env = {**(env or os.environ), "HOME": f"/home/{UNPRIVILEGED}"}
    return await asyncio.create_subprocess_exec(
        *command, env=env, stdin=asyncio.subprocess.DEVNULL if stdin is None else stdin,
        stdout=asyncio.subprocess.DEVNULL, stderr=None if program == "ffmpeg" else asyncio.subprocess.DEVNULL,
    )


async def _wait_for(check: Any, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() > deadline:
            raise TimeoutError("timed out waiting for the display")
        await asyncio.sleep(0.1)


class CdpError(Exception):
    """Chromium refused a command or went away."""


class Cdp:
    """A minimal client for the DevTools protocol of Chromium's one page."""

    def __init__(self, http: aiohttp.ClientSession, ws: aiohttp.ClientWebSocketResponse) -> None:
        self._http = http
        self._ws = ws
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.closed = asyncio.Event()
        # Called with each event's method and parameters (the domains enabled, e.g. Page).
        self.on_event: Callable[[str, dict[str, Any]], None] | None = None
        self._reader = asyncio.create_task(self._read())

    @classmethod
    async def connect(cls, port: int, timeout: float = 30) -> Cdp:
        http = aiohttp.ClientSession()
        deadline = time.monotonic() + timeout
        try:
            while True:
                try:
                    async with http.get(f"http://127.0.0.1:{port}/json/list") as response:
                        pages = [t for t in await response.json() if t.get("type") == "page"]
                    if pages:
                        ws = await http.ws_connect(pages[0]["webSocketDebuggerUrl"], max_msg_size=0)
                        return cls(http, ws)
                except (aiohttp.ClientError, ValueError):
                    pass
                if time.monotonic() > deadline:
                    raise CdpError("Chromium didn't start")
                await asyncio.sleep(0.3)
        except BaseException:
            await http.close()
            raise

    async def call(self, method: str, **params: Any) -> dict[str, Any]:
        if self.closed.is_set():
            raise CdpError("Chromium went away")
        message_id = next(self._ids)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        await self._ws.send_json({"id": message_id, "method": method, "params": params})
        try:
            return await asyncio.wait_for(future, 30)
        except TimeoutError as err:
            raise CdpError(f"{method} timed out") from err
        finally:
            self._pending.pop(message_id, None)

    async def close(self) -> None:
        self._reader.cancel()
        await self._ws.close()
        await self._http.close()
        self.closed.set()

    async def _read(self) -> None:
        try:
            async for message in self._ws:
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = json.loads(message.data)
                if "method" in data and "id" not in data:
                    if self.on_event:
                        self.on_event(data["method"], data.get("params", {}))
                    continue
                future = self._pending.get(data.get("id"))
                if future and not future.done():
                    if "error" in data:
                        future.set_exception(CdpError(data["error"].get("message", "error")))
                    else:
                        future.set_result(data.get("result", {}))
        finally:
            self.closed.set()
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(CdpError("Chromium went away"))
