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
import re
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
# A stream that stops while every process runs (ffmpeg waiting on an input or the encoder) is
# started again: seconds for the first playlist, then without a new one, and between looks.
FIRST_SEGMENT_SECONDS = 30
STALL_SECONDS = 10 * SEGMENT_SECONDS
WATCH_SECONDS = 1.0
# Seconds a process gets to stop when asked, and again when killed.
STOP_SECONDS = 5
# Quality of the hardware encoder (lower is better): about 4 to 8 Mbit/s for a wall of videos at 1080p.
VAAPI_QP = 26
# The sound: AAC, which every TV plays.
AUDIO_ARGS = ["-c:a", "aac", "-b:a", "128k"]
_displays = itertools.count(10)
# The user that Xvfb and Chromium run as, when the renderer itself runs as root.
UNPRIVILEGED = os.environ.get("RENDER_USER", "render")


class Folders:
    """The folders a renderer keeps for its screens: screens/<name>/stream (the HLS stream, made
    again at each start) and screens/<name>/profile (Chromium's profile, kept). The names are
    made here (s1, s2, …) and kept in folders.json, so a screen keeps its profile across restarts
    and a key a request brings never becomes part of a path."""

    NAME = re.compile(r"s[1-9][0-9]{0,8}")
    LEFTOVER = re.compile(r"index\.m3u8(\.tmp)?|s\d{1,9}\.ts")

    def __init__(self, root: Path) -> None:
        self.root = root
        self.file = root / "folders.json"
        try:
            known = json.loads(self.file.read_text())
        except (OSError, ValueError):
            known = {}
        self.names: dict[str, str] = {
            key: name for key, name in (known.items() if isinstance(known, dict) else ())
            if isinstance(key, str) and isinstance(name, str) and self.NAME.fullmatch(name)
        }

    def name(self, key: str) -> str:
        """The key's folder name, made the first time it's asked for."""
        name = self.names.get(key)
        if name is None:
            taken = {int(n[1:]) for n in self.names.values()}
            name = f"s{next(n for n in itertools.count(1) if n not in taken)}"
            self.names[key] = name
            self._adopt(key, name)
            self.root.mkdir(parents=True, exist_ok=True)
            part = self.file.with_name(self.file.name + ".tmp")
            part.write_text(json.dumps(self.names))
            part.replace(self.file)
        return name

    def stream(self, name: str) -> Path:
        return self.root / "screens" / name / "stream"

    def profile(self, name: str) -> Path:
        return self.root / "screens" / name / "profile"

    def _adopt(self, key: str, name: str) -> None:
        """What an older renderer kept by key: its profile (profiles/<key>) becomes this name's,
        sign-ins and all, and its stream folder (<key>) goes. Found by listing, never by a path
        made from the key."""
        try:
            for entry in (self.root / "profiles").iterdir():
                if entry.name == key and entry.is_dir() and not entry.is_symlink():
                    target = self.profile(name)
                    if not target.exists():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        entry.rename(target)
                    break
        except OSError:
            pass
        try:
            for entry in self.root.iterdir():
                if entry.name == key and entry.name not in ("screens", "profiles") and entry.is_dir() and not entry.is_symlink():
                    for item in entry.iterdir():
                        if self.LEFTOVER.fullmatch(item.name) and item.is_file():
                            item.unlink()
                    entry.rmdir()  # only if nothing else was in it
                    break
        except OSError:
            pass


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
    # How the TV's screen is mounted, in degrees turned clockwise: the page is drawn upright for
    # it, and the picture turned back to the screen's own orientation (ROTATE).
    rotation: int = 0

    @property
    def page_size(self) -> tuple[int, int]:
        """The page's size: the screen's, the other way round when it hangs on its side."""
        return (self.height, self.width) if self.rotation in (90, 270) else (self.width, self.height)

    @property
    def ha_origin(self) -> str:
        parts = urlsplit(self.ha_url)
        return f"{parts.scheme}://{parts.netloc}" if parts.scheme in ("http", "https") and parts.netloc else ""

    @property
    def page(self) -> tuple[str, tuple[actions.Step, ...]]:
        """The address to open, and the steps to take on it once it loads (actions.split)."""
        return actions.split(self.url)

    def restart_needed(self, other: Screen) -> bool:
        """Size, scale and rotation are fixed for a Chromium window and ffmpeg; the rest changes in place."""
        return (self.width, self.height, self.scale, self.rotation) != (other.width, other.height, other.scale, other.rotation)


# ffmpeg filters that turn the upright page into the picture for a screen mounted this way:
# turned clockwise, the picture turns anticlockwise, and so on.
ROTATE = {90: "transpose=2", 180: "hflip,vflip", 270: "transpose=1"}


def bitrate(screen: Screen) -> int:
    """Bits per second: 6 Mbit/s for 1080p, scaled by area, at least 1.5 Mbit/s."""
    return max(1_500_000, int(6_000_000 * screen.width * screen.height / (1920 * 1080)))


def encoder_args(encoder: str, screen: Screen) -> list[str]:
    """ffmpeg's video encoding options: a keyframe every segment, so a TV can join at any."""
    gop = ["-g", str(FPS * SEGMENT_SECONDS)]
    turn = [ROTATE[screen.rotation]] if screen.rotation in ROTATE else []
    if encoder == "vaapi":
        # Constant quality: the free VA-API drivers have no bitrate control for H.264.
        return ["-vf", ",".join([*turn, "format=nv12", "hwupload"]), "-c:v", "h264_vaapi", "-bf", "0", *gop,
                "-rc_mode", "CQP", "-qp", str(VAAPI_QP)]
    rate = bitrate(screen)
    return [
        *(["-vf", ",".join(turn)] if turn else []),
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
        "-f", "x11grab", "-framerate", str(FPS), "-video_size", "{}x{}".format(*screen.page_size),
        "-draw_mouse", "0", "-i", display, *sound_in,
        *encoder_args(encoder, screen), *sound_out,
        "-f", "hls", "-hls_time", str(SEGMENT_SECONDS), "-hls_list_size", str(PLAYLIST_SEGMENTS),
        "-hls_flags", "delete_segments+independent_segments+omit_endlist+temp_file",
        "-hls_segment_filename", str(folder / "s%d.ts"), str(folder / "index.m3u8"),
    ]


def through_filter(proxy: int) -> list[str]:
    """Chromium's every connection through the filter (filter.py), which looks the names up
    (SOCKS5 passes names, not addresses): loopback through it too, no WebRTC around it, and none
    of Chromium's own calls home."""
    return [
        f"--proxy-server=socks5://127.0.0.1:{proxy}",
        "--proxy-bypass-list=<-loopback>",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--disable-quic",
        "--disable-background-networking",
        "--disable-component-update",
        "--no-pings",
    ]


def chromium_args(screen: Screen, profile: Path, port: int, alsa_device: str | None = None,
                  proxy: int | None = None) -> list[str]:
    return [
        # A controller's pages: through the filter, on the public internet and the places the
        # site lists only (docs/renderer-connect-out.md).
        *(through_filter(proxy) if proxy else []),
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
        f"--window-size={round(screen.page_size[0] / screen.scale)},{round(screen.page_size[1] / screen.scale)}",
        "--window-position=0,0",
        f"--force-device-scale-factor={screen.scale:g}",
        f"--user-data-dir={profile}",
        "--remote-debugging-address=127.0.0.1",
        f"--remote-debugging-port={port}",
        "about:blank",
    ]


class Session:
    """Renders one TV's screen until stopped; restarts what crashes."""

    def __init__(self, key: str, screen: Screen, folders: Folders, encoder: str, proxy: int | None = None) -> None:
        self.key = key
        self.proxy = proxy  # the filter's port, for a controller's pages
        self.secret: str | None = None  # a controller's session's stream address
        self.screen = screen
        self.encoder = encoder
        name = folders.name(key)
        self.folder = folders.stream(name)
        self.profile = folders.profile(name)
        self.display = next(_displays)
        self.port = 9300 + self.display
        self.last_access = time.monotonic()
        self._procs: dict[str, asyncio.subprocess.Process] = {}  # by program, in the order started
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
                why = await self._wait_for_exit()
                _LOGGER.warning("%s: %s; starting again", self.key, why)
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
        profile = self.profile
        profile.mkdir(parents=True, exist_ok=True)
        if UNPRIVILEGED and os.geteuid() == 0:
            shutil.chown(profile, UNPRIVILEGED, UNPRIVILEGED)
        # A profile left by a crash would show Chromium's "restore pages?" question.
        for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            (profile / name).unlink(missing_ok=True)
        screen = self.screen
        display = f":{self.display}"
        self._procs["Xvfb"] = (await _spawn(
            "Xvfb", display, "-screen", "0", "{}x{}x24".format(*screen.page_size), "-nolisten", "tcp", "-nocursor",
        ))
        await _wait_for(lambda: Path(f"/tmp/.X11-unix/X{self.display}").exists(), 10)
        env = {**os.environ, "DISPLAY": display}
        env.pop("PULSE_SERVER", None)  # Chromium would play there instead of into the stream
        self._sound = self._open_sound(profile / "sound")
        device = sound.alsa_device(self._sound.fifo) if self._sound else None
        self._procs["chromium"] = await _spawn("chromium", *chromium_args(screen, profile, self.port, device, self.proxy), env=env)
        self._cdp = await Cdp.connect(self.port)
        self._cdp.on_event = self._on_event
        await self._cdp.call("Page.enable")
        await self._install_script()
        await self._cdp.call("Page.navigate", url=screen.page[0])
        self._procs["ffmpeg"] = (await _spawn(
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
            if item["kind"] == "notify" and (item.get("tone") or item.get("level") == "alert"):
                await self._sound_alert(item)
        await self._draw_overlays()
        if self.overlays and (self._overlay_task is None or self._overlay_task.done()):
            self._overlay_task = asyncio.create_task(self._keep_overlays())

    async def _sound_alert(self, item: dict[str, Any]) -> None:
        """A warning's or alert's tone, once, when it arrives."""
        if not (self._cdp and self._ready.is_set()):
            return
        try:
            await self._cdp.call("Runtime.evaluate", expression=page.alert_script(item.get("tone") or "", item.get("level") == "alert"))
        except CdpError as err:
            _LOGGER.debug("%s: tone not played: %s", self.key, err)

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

    async def _wait_for_exit(self) -> str:
        """Until something stops: a process, Chromium's DevTools connection, the sound, or the
        stream itself, which no process ending shows. Says what, for the log."""
        waits: dict[asyncio.Future[Any], str] = {
            asyncio.ensure_future(proc.wait()): f"{name} stopped" for name, proc in self._procs.items()
        }
        if self._cdp:
            waits[asyncio.ensure_future(self._cdp.closed.wait())] = "Chromium's DevTools connection closed"
        watch = asyncio.ensure_future(self._watch_stream())
        waits[watch] = ""
        # Watched, not cancelled here: it's the sound itself, which stops with the rest.
        sound_task = self._sound.task if self._sound else None
        try:
            done, _ = await asyncio.wait([*waits, *([sound_task] if sound_task else [])], return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in waits:
                task.cancel()
        for task, why in waits.items():
            if task in done:
                return watch.result() if task is watch else why
        assert sound_task
        error = None if sound_task.cancelled() else sound_task.exception()
        return f"the sound stopped ({error!r})" if error else "the sound stopped"

    async def _watch_stream(self) -> str:
        """Returns when the playlist stops changing: ffmpeg can wait for its sound, the display or
        the encoder without stopping or saying so, and the TV then shows nothing."""
        seen: int | None = None
        since = time.monotonic()
        while True:
            await asyncio.sleep(WATCH_SECONDS)
            try:
                stamp: int | None = self.playlist.stat().st_mtime_ns
            except OSError:
                stamp = None
            now = time.monotonic()
            if stamp != seen:
                seen, since = stamp, now
            elif seen is None and now - since > FIRST_SEGMENT_SECONDS:
                return f"the stream didn't start in {FIRST_SEGMENT_SECONDS:g} s"
            elif seen is not None and now - since > STALL_SECONDS:
                return f"the stream stopped: no new picture in {STALL_SECONDS:g} s"

    async def _kill(self) -> None:
        if self._steps_task:
            self._steps_task.cancel()
            self._steps_task = None
        if self._cdp:
            await self._cdp.close()
            self._cdp = None
        self._script_id = None
        for name, proc in reversed(self._procs.items()):
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), STOP_SECONDS)
                except TimeoutError:
                    proc.kill()
                    try:
                        await asyncio.wait_for(proc.wait(), STOP_SECONDS)
                    except TimeoutError:
                        # Stuck in the kernel (a hung graphics driver): starting again beats waiting forever.
                        _LOGGER.warning("%s: %s (process %d) doesn't stop; going on without it", self.key, name, proc.pid)
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
