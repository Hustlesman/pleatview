"""Home Assistant at the site, shared with the controller this renderer connects out to, when the
site turns it on (docs/renderer-connect-out.md#home-assistant-at-the-site).

The site chooses what the controller gets: the entities it lists (cameras by default), their
states, their pictures and live streams, its to-do lists and calendars; and, only if it allows
that too, the list of its PleatView screens and their move to the controller. Home Assistant's
token is the site's: it stays in this process. The controller never gets it, nor anything signed
with it, and pages this renderer draws never see it (it isn't in their environment).

A camera's live stream is fetched here, with the token, and made into this renderer's own
stream (HLS, H.264), served like a screen's: to the site's screens on the streams port, to the
controller through the connection. ffmpeg only reads what this process gives it on its standard
input, so the token isn't on its command line either; it runs without root, like Chromium.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import re
import secrets
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urljoin, urlsplit

import aiohttp

_LOGGER = logging.getLogger(__name__)

ENTITY = re.compile(r"^[a-z0-9_]{1,64}\.[a-z0-9_]{1,128}$")
# What the site lists: entity ids, with * and ? standing for any letters (camera.*, sensor.kitchen_*).
PATTERN = re.compile(r"^[a-z0-9_*?]{1,64}\.[a-z0-9_*?]{1,128}$")
DEFAULT_SHARED = ("camera.*",)
SCREEN_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
TIMEOUT = aiohttp.ClientTimeout(total=15)
LARGEST_ANSWER = 8 * 1024 * 1024
LARGEST_PICTURE = 3 * 1024 * 1024  # base64 of it fits a 4 MB answer
LARGEST_SEGMENT = 16 * 1024 * 1024
# Live streams of cameras at once, and how long one nobody fetches keeps going.
RELAYS_MOST = 4
# The picture stream of a camera without a video stream, as video: this often, at most this wide.
MJPEG_FPS = 10
MJPEG_WIDTH = 1920
UNPRIVILEGED = os.environ.get("RENDER_USER", "render")
TRUE = ("1", "true", "yes", "on")


class Unavailable(Exception):
    """Home Assistant didn't answer as it should. The message is the renderer's own words."""


@dataclass(frozen=True)
class Shared:
    """What the site shares: entities matching these patterns, and its screens or not."""

    patterns: tuple[str, ...] = ()
    screens: bool = False

    @classmethod
    def parse(cls, env: dict[str, str]) -> Shared:
        """From the options; ValueError says what's wrong (and then nothing is shared)."""
        patterns: tuple[str, ...] = ()
        if (env.get("SHARE_HOME_ASSISTANT") or "").strip().lower() in TRUE:
            given = [p.strip().lower() for p in (env.get("SHARED_ENTITIES") or "").split(",") if p.strip()]
            wrong = [p for p in given if not PATTERN.fullmatch(p)]
            if wrong:
                raise ValueError(f"shared_entities lists entity ids such as camera.front_door or camera.*, not {wrong[0][:60]}")
            patterns = tuple(given) or DEFAULT_SHARED
        return cls(patterns, (env.get("SHARE_SCREENS") or "").strip().lower() in TRUE)

    @property
    def anything(self) -> bool:
        return bool(self.patterns) or self.screens

    def allows(self, entity_id: Any, domain: str | None = None) -> bool:
        return (isinstance(entity_id, str) and bool(ENTITY.fullmatch(entity_id))
                and (domain is None or entity_id.split(".", 1)[0] == domain)
                and any(fnmatch.fnmatchcase(entity_id, p) for p in self.patterns))

    def describe(self) -> dict[str, Any]:
        """What the controller is told: what it may ask for."""
        return {"shared": list(self.patterns), "screens": self.screens}


@dataclass(frozen=True)
class Access:
    """Where Home Assistant is, and the token: given (HA_URL, HA_TOKEN), or the Supervisor's in
    the Home Assistant OS app."""

    root: str  # http(s)://host[:port], or http://supervisor/core through the Supervisor
    websocket: str
    token: str

    @classmethod
    def find(cls, env: dict[str, str]) -> Access | None:
        url, token = (env.get("HA_URL") or "").strip().rstrip("/"), (env.get("HA_TOKEN") or "").strip()
        if url and token:
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https") or not parts.hostname or parts.path or parts.query:
                raise ValueError("HA_URL is Home Assistant's address, such as http://192.168.1.10:8123")
            return cls(url, ("wss" if parts.scheme == "https" else "ws") + url[len(parts.scheme):] + "/api/websocket", token)
        supervisor = (env.get("SUPERVISOR_TOKEN") or "").strip()
        if supervisor:
            return cls("http://supervisor/core", "ws://supervisor/core/websocket", supervisor)
        return None


def printable(text: Any, most: int = 200) -> str:
    return re.sub(r"[^\x20-\x7e]", "?", str(text))[:most]


class HomeAssistant:
    """Home Assistant's API, asked only for what the site shares."""

    def __init__(self, access: Access, shared: Shared) -> None:
        self.access, self.shared = access, shared
        self._http: aiohttp.ClientSession | None = None

    @property
    def http(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(trust_env=False)
        return self._http

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access.token}"}

    async def close(self) -> None:
        if self._http is not None:
            await self._http.close()

    def own(self, url: str) -> bool:
        """An address on Home Assistant itself: nothing with the token goes anywhere else."""
        mine, theirs = urlsplit(self.access.root), urlsplit(url)
        return (theirs.scheme, theirs.netloc) == (mine.scheme, mine.netloc) and theirs.path.startswith(mine.path + "/")

    async def fetch(self, url: str, most: int = LARGEST_ANSWER) -> tuple[bytes, str]:
        """A file from Home Assistant: its bytes and type."""
        if not self.own(url):
            raise Unavailable("Home Assistant pointed somewhere else")
        try:
            async with self.http.get(url, headers=self.headers, timeout=TIMEOUT, allow_redirects=False) as response:
                if response.status in (401, 403):
                    raise Unavailable("Home Assistant refused this renderer's token")
                if response.status != 200:
                    raise Unavailable(f"Home Assistant answered HTTP {response.status}")
                body = await response.content.read(most + 1)
                if len(body) > most:
                    raise Unavailable("Home Assistant's answer is too large")
                return body, response.content_type
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            raise Unavailable("Home Assistant can't be reached") from None

    async def _get(self, path: str) -> Any:
        body, _ = await self.fetch(f"{self.access.root}/api{path}")
        try:
            return json.loads(body)
        except ValueError:
            raise Unavailable("Home Assistant didn't answer with JSON") from None

    async def _service(self, domain: str, service: str, data: dict[str, Any]) -> Any:
        """A service with a response (?return_response): what it answers."""
        url = f"{self.access.root}/api/services/{domain}/{service}?return_response"
        try:
            async with self.http.post(url, json=data, headers=self.headers, timeout=TIMEOUT, allow_redirects=False) as response:
                text = await response.content.read(LARGEST_ANSWER)
                try:
                    answer = json.loads(text)
                except ValueError:
                    answer = None
                if response.status in (401, 403):
                    raise Unavailable("Home Assistant refused this renderer's token (the services need an administrator's)")
                if response.status == 400 and isinstance(answer, dict) and "service" in printable(answer.get("message", ""), 300).lower() \
                        and "not found" in printable(answer.get("message", ""), 300).lower():
                    raise Unavailable("Home Assistant's PleatView integration is older than this renderer: update it")
                if response.status != 200 or not isinstance(answer, dict):
                    said = printable(answer.get("message", ""), 300) if isinstance(answer, dict) else ""
                    raise Unavailable(f"Home Assistant answered HTTP {response.status}" + (f": {said}" if said else ""))
                return answer.get("service_response")
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            raise Unavailable("Home Assistant can't be reached") from None

    async def _ws(self, message: dict[str, Any]) -> Any:
        """One command over Home Assistant's websocket API."""
        try:
            ws = await asyncio.wait_for(self.http.ws_connect(self.access.websocket, heartbeat=None,
                                                             max_msg_size=LARGEST_ANSWER), 10)
            try:
                hello = await ws.receive_json(timeout=10)
                if not isinstance(hello, dict) or hello.get("type") != "auth_required":
                    raise Unavailable("That isn't Home Assistant's websocket")
                await ws.send_json({"type": "auth", "access_token": self.access.token})
                if (await ws.receive_json(timeout=10)).get("type") != "auth_ok":
                    raise Unavailable("Home Assistant refused this renderer's token")
                await ws.send_json({"id": 1, **message})
                while True:
                    reply = await ws.receive_json(timeout=15)
                    if isinstance(reply, dict) and reply.get("id") == 1:
                        if not reply.get("success"):
                            error = reply.get("error") if isinstance(reply.get("error"), dict) else {}
                            raise Unavailable(printable(error.get("message") or "Home Assistant refused it", 200))
                        return reply.get("result")
            finally:
                await ws.close()
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError, TypeError):
            raise Unavailable("Home Assistant's websocket can't be reached") from None

    def check(self, entity_id: Any, domain: str | None = None) -> str:
        if not self.shared.allows(entity_id, domain):
            raise Unavailable(f"this site doesn't share {printable(entity_id, 80) if isinstance(entity_id, str) else 'that'}"
                              + (f" (a {domain})" if domain else ""))
        return entity_id

    # What the controller may ask

    async def config(self) -> dict[str, Any]:
        config = await self._get("/config")
        if not isinstance(config, dict):
            raise Unavailable("Home Assistant didn't say who it is")
        return {"name": printable(config.get("location_name") or "Home Assistant", 100), "version": printable(config.get("version") or "", 40)}

    async def entities(self) -> list[dict[str, Any]]:
        """The shared entities: id, kind, name and state. No attributes beyond the name: those
        can hold addresses and tokens (a camera's picture address does)."""
        states = await self._get("/states")
        out = []
        for state in states if isinstance(states, list) else []:
            entity_id = state.get("entity_id") if isinstance(state, dict) else None
            if self.shared.allows(entity_id):
                attributes = state.get("attributes") if isinstance(state.get("attributes"), dict) else {}
                out.append({"entity_id": entity_id, "domain": entity_id.split(".", 1)[0],
                            "name": printable(attributes.get("friendly_name") or entity_id, 200),
                            "state": printable(state.get("state"), 255) if state.get("state") is not None else None})
        return out

    async def picture(self, entity_id: Any) -> tuple[bytes, str]:
        entity = self.check(entity_id, "camera")
        body, kind = await self.fetch(f"{self.access.root}/api/camera_proxy/{entity}", LARGEST_PICTURE)
        if not kind.startswith("image/"):
            raise Unavailable("Home Assistant didn't send a picture")
        return body, kind

    async def todo(self, entity_id: Any) -> list[dict[str, Any]]:
        entity = self.check(entity_id, "todo")
        result = await self._ws({"type": "todo/item/list", "entity_id": entity})
        items = result.get("items") if isinstance(result, dict) else None
        if not isinstance(items, list):
            raise Unavailable("Home Assistant didn't send the list's items")
        return [{"summary": printable(i.get("summary"), 200), "status": i.get("status") if i.get("status") in ("needs_action", "completed") else None,
                 "due": printable(i["due"], 40) if isinstance(i.get("due"), str) else None}
                for i in items[:500] if isinstance(i, dict) and i.get("summary")]

    async def calendar(self, entity_id: Any, start: Any, end: Any) -> list[dict[str, Any]]:
        entity = self.check(entity_id, "calendar")
        times = [t for t in (start, end) if isinstance(t, str) and re.fullmatch(r"[0-9T:.+\-Z ]{10,40}", t)]
        if len(times) != 2:
            raise Unavailable("start and end are times (ISO 8601)")
        events = await self._get(f"/calendars/{quote(entity)}?" + urlencode({"start": times[0], "end": times[1]}))
        keep = ("summary", "start", "end", "location", "description", "uid", "recurrence_id")
        return [{k: e[k] for k in keep if k in e} for e in (events if isinstance(events, list) else [])[:2000]
                if isinstance(e, dict) and isinstance(e.get("start"), dict) and isinstance(e.get("end"), dict)]

    async def stream_url(self, entity_id: str) -> str | None:
        """A camera's HLS stream in Home Assistant, or None for a camera without one."""
        try:
            result = await self._ws({"type": "camera/stream", "entity_id": entity_id, "format": "hls"})
        except Unavailable:
            return None
        url = result.get("url") if isinstance(result, dict) else None
        if not isinstance(url, str) or not url.startswith("/api/"):
            return None
        return self.access.root + url

    def picture_stream(self, entity_id: str) -> str:
        return f"{self.access.root}/api/camera_proxy_stream/{entity_id}"

    async def screens(self) -> list[dict[str, Any]]:
        if not self.shared.screens:
            raise Unavailable("this site doesn't pass its screens on")
        found = await self._service("pleatview", "list_screens", {})
        screens = found.get("screens") if isinstance(found, dict) else None
        if not isinstance(screens, list):
            raise Unavailable("Home Assistant's PleatView integration didn't list its screens")
        keys = ("id", "name", "manufacturer", "model", "app_version", "online", "connects_out")
        return [{k: s.get(k) for k in keys} for s in screens[:500] if isinstance(s, dict) and isinstance(s.get("id"), str)]

    async def enrol(self, screen: Any, url: str, token: Any, fingerprint: str | None) -> None:
        if not self.shared.screens:
            raise Unavailable("this site doesn't pass its screens on")
        if not isinstance(screen, str) or not SCREEN_ID.fullmatch(screen):
            raise Unavailable("screen is a screen's id, as ha_screens gives it")
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_\-]{16,200}", token):
            raise Unavailable("token is the one-time token for the screen")
        await self._service("pleatview", "enrol_with", {"screen": screen, "url": url, "token": token,
                                                       **({"fingerprint": fingerprint} if fingerprint else {})})


# A camera's live stream, made here


def media_playlist(text: str) -> tuple[list[tuple[int, int, str]], str | None, float]:
    """An HLS media playlist's whole segments: (number, discontinuity, address), its init
    segment's address (fMP4) and its target duration. Parts (LL-HLS) are left out."""
    sequence, discontinuity, target, init = 0, 0, 2.0, None
    segments: list[tuple[int, int, str]] = []
    waiting = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            sequence = int(line.split(":", 1)[1]) if line.split(":", 1)[1].isdigit() else 0
        elif line.startswith("#EXT-X-DISCONTINUITY-SEQUENCE:"):
            discontinuity = int(line.split(":", 1)[1]) if line.split(":", 1)[1].isdigit() else 0
        elif line == "#EXT-X-DISCONTINUITY":
            discontinuity += 1
        elif line.startswith("#EXT-X-TARGETDURATION:"):
            try:
                target = float(line.split(":", 1)[1])
            except ValueError:
                pass
        elif line.startswith("#EXT-X-MAP:"):
            found = re.search(r'URI="([^"]+)"', line)
            init = found.group(1) if found else None
        elif line.startswith("#EXTINF"):
            waiting = True
        elif line and not line.startswith("#") and waiting:
            segments.append((sequence, discontinuity, line))
            sequence += 1
            waiting = False
    return segments, init, target


def relay_ffmpeg(kind: str, folder: Path, hls_args: list[str]) -> list[str]:
    """ffmpeg's options: Home Assistant's video copied as it is (HLS, fragmented MP4), or a
    picture stream (MJPEG) encoded as H.264; either way into this renderer's own HLS."""
    if kind == "hls":
        source = ["-f", "mp4", "-i", "pipe:0", "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy"]
    else:
        source = ["-use_wallclock_as_timestamps", "1", "-f", "mpjpeg", "-i", "pipe:0", "-an",
                  "-vf", f"fps={MJPEG_FPS},scale='min({MJPEG_WIDTH},iw)':-2,format=yuv420p",
                  "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
                  "-g", str(MJPEG_FPS * 2), "-keyint_min", str(MJPEG_FPS * 2), "-sc_threshold", "0",
                  "-b:v", "2000000", "-maxrate", "2600000", "-bufsize", "4000000"]
    return ["-loglevel", "error", "-hide_banner", *source, *hls_args]


class Relay:
    """One camera's live stream, as long as someone fetches it."""

    def __init__(self, key: str, entity: str, folder: Path, ha: HomeAssistant, hls_args: list[str]) -> None:
        self.key, self.entity, self.folder, self.ha, self.hls_args = key, entity, folder, ha, hls_args
        self.secret = secrets.token_hex(16)
        self.last_access = time.monotonic()
        self.kind: str | None = None
        self._task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if not self.running:
            self._task = asyncio.create_task(self._run(), name=f"relay {self.key}")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - stopping, whatever it was doing
                pass
            self._task = None

    async def _run(self) -> None:
        delay = 1
        while True:
            started = time.monotonic()
            try:
                why = await self._once()
                _LOGGER.info("%s: %s; starting again", self.key, why)
            except Unavailable as err:
                _LOGGER.info("%s: %s", self.key, err)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - the stream comes back, whatever stopped it
                _LOGGER.warning("%s: the camera's stream stopped (%s)", self.key, type(err).__name__)
            delay = 1 if time.monotonic() - started > 60 else min(delay * 2, 30)
            await asyncio.sleep(delay)

    async def _once(self) -> str:
        if self.folder.exists():
            shutil.rmtree(self.folder)
        self.folder.mkdir(parents=True)
        command = ["ffmpeg"]
        if UNPRIVILEGED and os.geteuid() == 0:
            shutil.chown(self.folder, UNPRIVILEGED, UNPRIVILEGED)
            command = ["setpriv", f"--reuid={UNPRIVILEGED}", f"--regid={UNPRIVILEGED}", "--init-groups", "--", "ffmpeg"]
        source = await self.ha.stream_url(self.entity)
        self.kind = "hls" if source else "mjpeg"
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL", "TZ")}
        proc = await asyncio.create_subprocess_exec(
            *command, *relay_ffmpeg(self.kind, self.folder, self.hls_args), env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=None)
        feeding = asyncio.create_task(self._feed_hls(source, proc.stdin) if source else self._feed_pictures(proc.stdin))
        ended = asyncio.create_task(proc.wait())
        try:
            done, _ = await asyncio.wait({feeding, ended}, return_when=asyncio.FIRST_COMPLETED)
            if feeding in done:
                return feeding.result() or "the camera's stream ended"
            return "ffmpeg stopped"
        finally:
            feeding.cancel()
            if proc.returncode is None:
                proc.kill()
            await asyncio.gather(feeding, ended, return_exceptions=True)

    async def _write(self, stdin: asyncio.StreamWriter, data: bytes) -> None:
        stdin.write(data)
        await stdin.drain()

    async def _feed_hls(self, url: str, stdin: asyncio.StreamWriter) -> str:
        master, _ = await self.ha.fetch(url)
        text = master.decode(errors="replace")
        variant = next((line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")), None)
        playlist = urljoin(url, variant) if variant and "#EXTINF" not in text else url
        written: int | None = None
        era: int | None = None
        init: str | None = None
        while True:
            body, _ = await self.ha.fetch(playlist)
            segments, map_uri, target = media_playlist(body.decode(errors="replace"))
            if written is None:
                segments = segments[-2:]  # live: from the newest
            for number, discontinuity, uri in segments:
                if written is not None and number <= written:
                    continue
                if era is not None and discontinuity != era:
                    return "the camera's stream started again"
                if init is None and map_uri:
                    init = map_uri
                    data, _ = await self.ha.fetch(urljoin(playlist, map_uri), LARGEST_SEGMENT)
                    await self._write(stdin, data)
                data, _ = await self.ha.fetch(urljoin(playlist, uri), LARGEST_SEGMENT)
                await self._write(stdin, data)
                written, era = number, discontinuity
            await asyncio.sleep(max(0.5, min(target, 6) / 2))

    async def _feed_pictures(self, stdin: asyncio.StreamWriter) -> str:
        url = self.ha.picture_stream(self.entity)
        try:
            async with self.ha.http.get(url, headers=self.ha.headers, allow_redirects=False,
                                        timeout=aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=30)) as response:
                if response.status != 200:
                    raise Unavailable(f"Home Assistant answered HTTP {response.status} for the camera's picture stream")
                async for chunk in response.content.iter_chunked(64 * 1024):
                    await self._write(stdin, chunk)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            raise Unavailable("Home Assistant's picture stream stopped") from None
        return "the camera's picture stream ended"
