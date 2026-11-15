"""HTTP API of the PleatView Renderer.

The PleatView integration says what each TV shows (``PUT /api/sessions/<key>``) and Home
Assistant passes the TV the stream (``GET /hls/<key>/index.m3u8``). A screen nobody has
fetched for a while stops, and starts again when its TV asks for it.

A renderer reachable only by its Home Assistant (the Home Assistant OS app, or `docker run`
on 127.0.0.1) answers anyone who reaches it, as before. Once a client pairs with the PIN in
the log, or when it listens on the network, every request needs that client's token
(protocol v1's pairing: docs/protocol.md).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import time
from typing import Any
from urllib.parse import urlsplit

from aiohttp import web

from . import actions
from .mdns import Responder, local_address
from .pairing import Pairing
from .session import Screen, Session

_LOGGER = logging.getLogger(__name__)

VERSION = "1.0.19"
KEY = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
FILE = re.compile(r"^(index\.m3u8|s\d{1,9}\.ts)$")
KIOSK_MODES = ("full", "sidebar", "off")
# A screen whose TV hasn't fetched its stream for this long stops until it does again.
IDLE_SECONDS = 90
# How long a TV's first request waits for a starting screen's first segments.
START_WAIT_SECONDS = 8
SERVICE = "_pleatview-renderer._tcp.local."
OPEN_PATHS = {"/api/info", "/api/pair", "/api/verify"}


def load() -> dict[str, float]:
    """How busy this machine is: the load per core, and the share of memory in use."""
    try:
        cpu = os.getloadavg()[0] / (os.cpu_count() or 1)
        meminfo = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines() if ":" in line)
        total = int(meminfo["MemTotal"].split()[0])
        available = int(meminfo["MemAvailable"].split()[0])
        return {"cpu": round(cpu, 2), "memory": round(1 - available / total, 2)}
    except (OSError, KeyError, ValueError, ZeroDivisionError):
        return {}

def parse_screen(body: Any) -> Screen:
    """A screen from a request body; ValueError says what's wrong."""
    if not isinstance(body, dict):
        raise ValueError("expected a JSON object")
    url = body.get("url")
    if not isinstance(url, str) or urlsplit(url).scheme not in ("http", "https") or not urlsplit(url).netloc:
        raise ValueError("url must be an http or https address")
    try:
        actions.split(url)
    except ValueError as err:
        raise ValueError(f"the page's steps (#{actions.PARAM}…): {err}") from None
    width, height = body.get("width", 1920), body.get("height", 1080)
    if not all(isinstance(v, int) and 160 <= v <= 3840 for v in (width, height)):
        raise ValueError("width and height must be whole pixels from 160 to 3840")
    scale = body.get("scale", 2.0)
    if not isinstance(scale, (int, float)) or not 0.25 <= scale <= 8:
        raise ValueError("scale must be from 0.25 to 8")
    kiosk = body.get("kiosk", "full")
    if kiosk not in KIOSK_MODES:
        raise ValueError(f"kiosk must be one of {', '.join(KIOSK_MODES)}")
    ha_url, token = body.get("ha_url", ""), body.get("token", "")
    if not isinstance(ha_url, str) or not isinstance(token, str):
        raise ValueError("ha_url and token must be text")
    rotation = body.get("rotation", 0)
    if rotation not in (0, 90, 180, 270) or isinstance(rotation, bool):
        raise ValueError("rotation must be 0, 90, 180 or 270 (degrees the screen is turned clockwise)")
    # Even sizes: H.264 in 4:2:0 needs them.
    return Screen(url=url, width=width // 2 * 2, height=height // 2 * 2, scale=round(float(scale), 3),
                  kiosk=kiosk, ha_url=ha_url, token=token, rotation=rotation)


POSITIONS = ("top-left", "top-right", "bottom-left", "bottom-right", "center")


# Notifications: a notice is a card; a warning adds a short tone; an alert a stronger one,
# and pauses what the page plays. The tones are tones.js's, played into the page (and so
# into the stream).
LEVELS = ("notice", "warning", "alert")
TONES = ("chime", "ding", "beep", "alarm", "siren", "bell")


def parse_overlay(body: Any) -> dict[str, Any] | None:
    """A notification or camera to draw on top of a screen's page; None to clear."""
    if not isinstance(body, dict):
        raise ValueError("expected a JSON object")
    kind = body.get("kind")
    if kind == "clear":
        return None

    def text(name: str, required: bool = False) -> str:
        value = body.get(name) or ""
        if not isinstance(value, str) or len(value) > 1000 or (required and not value):
            raise ValueError(f"{name} must be text of up to 1000 characters")
        return value

    def url(name: str, required: bool) -> str:
        value = body.get(name) or ""
        if not value and not required:
            return ""
        if not isinstance(value, str) or urlsplit(value).scheme not in ("http", "https"):
            raise ValueError(f"{name} must be an http or https address")
        return value

    level = body.get("level") or "notice"
    if kind == "notify" and level not in LEVELS:
        level = "notice"  # a level from a newer Home Assistant: still a card
    tone = body.get("tone") or ""
    if not isinstance(tone, str) or tone not in TONES or level == "notice":
        tone = ""
    duration = body.get("duration") or (10 if kind == "notify" and level != "alert" else 30 if kind == "notify" else 60)
    if not isinstance(duration, (int, float)) or not 1 <= duration <= 3600:
        raise ValueError("duration must be from 1 to 3600 seconds")
    position = body.get("position") or ("top-right" if kind == "notify" else "bottom-right")
    if position not in POSITIONS:
        raise ValueError(f"position must be one of {', '.join(POSITIONS)}")
    if kind == "notify":
        return {"kind": kind, "message": text("message", True), "title": text("title"),
                "image_url": url("image_url", False), "position": position, "duration": float(duration),
                "level": level, "tone": tone}
    if kind == "image":
        size = body.get("size") or 30
        if not isinstance(size, (int, float)) or not 10 <= size <= 100:
            raise ValueError("size must be from 10 to 100 (percent of the width)")
        if body.get("mode") == "fullscreen":
            size, position = 100, "center"
        return {"kind": kind, "url": url("url", True), "size": size, "position": position, "duration": float(duration)}
    raise ValueError("kind must be notify, image or clear")


class Full(Exception):
    """No room for another screen."""


# A controller elsewhere names its screens with keys of its own, kept apart from the local
# API's: its sessions are "~<key>", which no local key can be (KEY has no "~").
CONTROLLER_KEY = re.compile(r"^[A-Za-z0-9_-]{1,48}$")
LARGEST_SEGMENT = 4 * 1024 * 1024


class Renderer:
    """The screens this renderer draws: for the controller that reaches it on its network (Home
    Assistant, over protocol v1) and for one it connects out to (docs/renderer-connect-out.md),
    whose pages go through the filter and whose streams have secret addresses."""

    def __init__(self, root: Path, encoder: str, max_sessions: int = 4) -> None:
        self.root, self.encoder, self.max_sessions = root, encoder, max_sessions
        self.sessions: dict[str, Session] = {}
        self.streams: dict[str, str] = {}  # a controller's session's stream secret -> its key

    async def show(self, key: str, screen: Screen, proxy: int | None = None) -> Session:
        session = self.sessions.get(key)
        if session is None:
            if len(self.sessions) >= self.max_sessions:
                raise Full(f"this renderer shows at most {self.max_sessions} screens")
            session = self.sessions[key] = Session(key, screen, self.root, self.encoder, proxy=proxy)
            if key.startswith("~"):
                session.secret = secrets.token_hex(16)
                self.streams[session.secret] = key
            session.start()
        elif session.screen != screen:
            await session.update(screen)
        session.last_access = time.monotonic()
        session.start()
        return session

    async def stop(self, key: str) -> None:
        session = self.sessions.pop(key, None)
        if session:
            self.streams.pop(getattr(session, "secret", ""), None)
            await session.stop()

    async def file(self, session: Session, name: str) -> tuple[bytes, str, str]:
        """A stream's playlist or segment: its bytes, type and caching; FileNotFoundError if
        there's none (yet, or any more)."""
        if not FILE.match(name):
            raise FileNotFoundError(name)
        session.last_access = time.monotonic()
        if not session.running:
            _LOGGER.info("%s: its TV is back; starting", session.key)
            session.start()
        path = session.folder / name
        deadline = time.monotonic() + START_WAIT_SECONDS
        while not path.exists() and name == "index.m3u8" and time.monotonic() < deadline:
            await asyncio.sleep(0.25)
        body = await asyncio.to_thread(path.read_bytes)  # FileNotFoundError: an old segment ffmpeg just deleted
        cache = "no-cache" if name.endswith(".m3u8") else "max-age=60"
        content_type = "application/vnd.apple.mpegurl" if name.endswith(".m3u8") else "video/mp2t"
        return body, content_type, cache

    async def stop_idle(self) -> None:
        for session in list(self.sessions.values()):
            if session.running and time.monotonic() - session.last_access > IDLE_SECONDS:
                _LOGGER.info("%s: nobody watches; stopping until its TV asks again", session.key)
                await session.stop()

    async def close(self) -> None:
        for session in self.sessions.values():
            await session.stop()


def controller_calls(renderer: Renderer, info: Any, proxy: Any) -> dict[str, Any]:
    """What a controller elsewhere may ask of this renderer, each call checked
    (docs/renderer-connect-out.md#calls). `proxy()` is the filter's port."""
    from .connect import Refused

    def key_of(params: dict[str, Any]) -> str:
        key = params.get("key")
        if not isinstance(key, str) or not CONTROLLER_KEY.match(key):
            raise Refused("key is 1 to 48 of A-Z a-z 0-9 _ -")
        return f"~{key}"

    def existing(params: dict[str, Any]) -> Session:
        session = renderer.sessions.get(key_of(params))
        if session is None:
            raise Refused("no such screen")
        return session

    async def show(params: dict[str, Any]) -> dict[str, Any]:
        key = key_of(params)
        if params.get("ha_url") or params.get("token"):
            raise Refused("a controller elsewhere doesn't send Home Assistant's address or token")
        port = proxy()
        if not port:
            raise Refused("the filter for pages isn't running")
        try:
            screen = parse_screen({k: v for k, v in params.items() if k != "key"})
            session = await renderer.show(key, screen, proxy=port)
        except ValueError as err:
            raise Refused(str(err)) from None
        except Full as err:
            raise Refused(str(err)) from None
        return {"stream": f"/stream/{session.secret}/index.m3u8"}

    async def overlay(params: dict[str, Any]) -> dict[str, Any]:
        session = existing(params)
        body = {k: v for k, v in params.items() if k != "key"}
        try:
            item = parse_overlay(body)
        except ValueError as err:
            raise Refused(str(err)) from None
        await session.overlay(item, kind=str(body.get("clear") or "") if item is None else "")
        return {}

    async def stop(params: dict[str, Any]) -> dict[str, Any]:
        await renderer.stop(key_of(params))
        return {}

    async def segment(params: dict[str, Any]) -> dict[str, Any]:
        session = existing(params)
        name = params.get("name")
        if not isinstance(name, str) or not FILE.match(name):
            raise Refused("name is index.m3u8 or s<n>.ts")
        try:
            body, content_type, _ = await renderer.file(session, name)
        except FileNotFoundError:
            raise Refused("not there (yet, or any more)") from None
        if len(body) > LARGEST_SEGMENT:
            raise Refused("larger than 4 MB")
        return {"type": content_type, "data": base64.b64encode(body).decode()}

    async def about(params: dict[str, Any]) -> dict[str, Any]:
        return info()

    return {"info": about, "show": show, "overlay": overlay, "stop": stop, "segment": segment}


def describe_call(method: str, params: dict[str, Any]) -> str | None:
    """A controller's call as the renderer's log tells it: what changes what it shows."""
    from .connect import printable

    key = printable(params.get("key", ""), 48)
    if method == "show":
        return (f"show {key}: {printable(params.get('url', ''), 200)} "
                f"({printable(params.get('width', 1920), 6)}×{printable(params.get('height', 1080), 6)})")
    if method == "overlay":
        return f"draw {printable(params.get('kind', '?'), 20)} on {key}"
    if method == "stop":
        return f"stop {key}"
    return None


def create_stream_app(renderer: Renderer) -> web.Application:
    """Where the site's screens fetch the streams of a controller's sessions: at secret
    addresses, and nothing else."""

    @web.middleware
    async def plain(request: web.Request, handler):
        response = await handler(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    app = web.Application(middlewares=[plain])

    async def stream(request: web.Request) -> web.Response:
        key = renderer.streams.get(request.match_info["secret"])
        session = renderer.sessions.get(key) if key else None
        if session is None:
            raise web.HTTPNotFound()
        try:
            body, content_type, cache = await renderer.file(session, request.match_info["name"])
        except FileNotFoundError:
            raise web.HTTPNotFound() from None
        return web.Response(body=body, content_type=content_type, headers={"Cache-Control": cache})

    app.router.add_get(r"/stream/{secret:[0-9a-f]{32}}/{name}", stream)
    return app


def create_app(root: Path, encoder: str, max_sessions: int = 4, *, pairing: Pairing | None = None,
               require_token: bool = False, identity: dict[str, str] | None = None,
               renderer: Renderer | None = None) -> web.Application:
    renderer = renderer or Renderer(root, encoder, max_sessions)
    sessions = renderer.sessions
    max_sessions = renderer.max_sessions
    identity = identity or {}

    def authorized(request: web.Request) -> bool:
        token = re.sub(r"^Bearer\s+", "", request.headers.get("Authorization", ""), flags=re.I) or None
        return pairing is not None and pairing.valid(token)

    def open_to_all() -> bool:
        return not require_token and not (pairing and pairing.paired)

    @web.middleware
    async def guard(request: web.Request, handler):
        if request.path not in OPEN_PATHS and not open_to_all() and not authorized(request):
            raise web.HTTPUnauthorized(text=json.dumps({"error": "unauthorized"}), content_type="application/json")
        return await handler(request)

    app = web.Application(middlewares=[guard])

    async def info(request: web.Request) -> web.Response:
        public = {
            "version": VERSION,
            "encoder": encoder,
            **identity,
            "paired": bool(pairing and pairing.paired),
            "max_screens": max_sessions,
        }
        if open_to_all() or authorized(request):
            public["load"] = load()
            public["sessions"] = {
                key: {"url": s.screen.url, "width": s.screen.width, "height": s.screen.height, "running": s.running}
                for key, s in sessions.items() if not key.startswith("~")  # a controller elsewhere's are its own
            }
        return web.json_response(public)

    async def pair(request: web.Request) -> web.Response:
        if pairing is None:
            raise web.HTTPNotFound()
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text="expected a JSON object")
        result = pairing.pair(body.get("pin"), body.get("client"))
        if result["result"] == "paired":
            return web.json_response({"token": result["token"]})
        if result["result"] == "locked_out":
            return web.json_response({"error": "too_many_attempts", "retry_after": result["retry_after"]}, status=429)
        return web.json_response({"error": "invalid_pin"}, status=403)

    async def verify(request: web.Request) -> web.Response:
        try:
            nonce = (await request.json()).get("nonce")
        except (ValueError, AttributeError):
            nonce = None
        if pairing is None or not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-fA-F]{32,128}", nonce):
            raise web.HTTPBadRequest(text="nonce must be 32 to 128 hex digits")
        return web.json_response({"proofs": pairing.prove(nonce)})

    async def unpair(request: web.Request) -> web.Response:
        token = re.sub(r"^Bearer\s+", "", request.headers.get("Authorization", ""), flags=re.I)
        if pairing and token:
            pairing.revoke(token)
        return web.json_response({})

    async def put_session(request: web.Request) -> web.Response:
        key = request.match_info["key"]
        if not KEY.match(key):
            raise web.HTTPNotFound()
        try:
            screen = parse_screen(await request.json())
        except ValueError as err:
            raise web.HTTPBadRequest(text=str(err)) from err
        try:
            await renderer.show(key, screen)
        except Full as err:
            raise web.HTTPTooManyRequests(text=str(err)) from None
        return web.json_response({"playlist": f"/hls/{key}/index.m3u8"})

    def local(request: web.Request) -> Session | None:
        """A session of the local API's (never a controller elsewhere's)."""
        key = request.match_info["key"]
        return sessions.get(key) if KEY.match(key) else None

    async def put_overlay(request: web.Request) -> web.Response:
        session = local(request)
        if session is None:
            raise web.HTTPNotFound()
        try:
            body = await request.json()
            item = parse_overlay(body)
        except ValueError as err:
            raise web.HTTPBadRequest(text=str(err)) from err
        await session.overlay(item, kind=str(body.get("clear") or "") if item is None else "")
        return web.json_response({})

    async def delete_session(request: web.Request) -> web.Response:
        if local(request):
            await renderer.stop(request.match_info["key"])
        return web.Response(status=204)

    async def hls(request: web.Request) -> web.StreamResponse:
        session = local(request)
        if session is None:
            raise web.HTTPNotFound()
        try:
            body, content_type, cache = await renderer.file(session, request.match_info["name"])
        except FileNotFoundError:
            raise web.HTTPNotFound() from None
        return web.Response(body=body, content_type=content_type, headers={"Cache-Control": cache})

    async def idle(app: web.Application) -> Any:
        async def check() -> None:
            while True:
                await asyncio.sleep(15)
                await renderer.stop_idle()

        task = asyncio.create_task(check())
        yield
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await renderer.close()

    app.cleanup_ctx.append(idle)
    async def packages(request: web.Request) -> web.Response:
        path = Path("/app-packages.txt")
        return web.Response(text=path.read_text() if path.exists() else "", content_type="text/plain")

    app.router.add_get("/api/info", info)
    app.router.add_post("/api/pair", pair)
    app.router.add_post("/api/verify", verify)
    app.router.add_post("/api/unpair", unpair)
    app.router.add_get("/api/packages", packages)
    app.router.add_put("/api/sessions/{key}", put_session)
    app.router.add_delete("/api/sessions/{key}", delete_session)
    app.router.add_post("/api/sessions/{key}/overlay", put_overlay)
    app.router.add_get("/hls/{key}/{name}", hls)
    return app


async def detect_encoder(choice: str) -> str:
    """vaapi when the host's GPU encodes H.264 (Intel Quick Sync, AMD), else x264 on the processor."""
    if choice in ("vaapi", "x264"):
        return choice
    if not Path("/dev/dri/renderD128").exists():
        return "x264"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-loglevel", "error", "-vaapi_device", "/dev/dri/renderD128", "-f", "lavfi",
        "-i", "testsrc=size=320x240:rate=30", "-frames:v", "5", "-vf", "format=nv12,hwupload",
        "-c:v", "h264_vaapi", "-f", "null", "-",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    return "vaapi" if await proc.wait() == 0 else "x264"


def advertised_address(env: dict[str, str], port: int) -> str:
    """The address to put in PleatView's options: the one it listens on, or its host name."""
    host = env.get("HOST", "0.0.0.0")
    if host in ("", "0.0.0.0", "::"):
        host = env.get("HOSTNAME", "<this machine>")
    return f"http://{host}:{port}"


def settings() -> dict[str, str]:
    """Environment variables, or the add-on's options when running as a Home Assistant add-on."""
    values = dict(os.environ)
    options = Path("/data/options.json")
    if options.exists():
        values.update({key.upper(): ",".join(map(str, value)) if isinstance(value, list) else "" if value is None else str(value)
                       for key, value in json.loads(options.read_text()).items()})
    return values


def is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def state_folder(env: dict[str, str]) -> Path:
    """Where the renderer keeps its identity and pairings: /data in the app and in a volume."""
    folder = Path(env.get("STATE", "/data" if Path("/data").is_dir() else "/tmp/pleatview-renderer-state"))
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def write_private(path: Path, text: str) -> None:
    fd = os.open(path.with_suffix(".tmp"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as out:
        out.write(text)
    os.replace(path.with_suffix(".tmp"), path)


def make_certificate(cert: Path, key: Path) -> None:
    """A self-signed certificate for this machine's name and address, once."""
    if cert.exists() and key.exists():
        return
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    private = ec.generate_private_key(ec.SECP256R1())
    host = socket.gethostname()
    names: list[x509.GeneralName] = [x509.DNSName(host), x509.DNSName("localhost")]
    for address in {local_address(), "127.0.0.1"}:
        names.append(x509.IPAddress(ipaddress.ip_address(address)))
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"PleatView Renderer on {host}"[:64])])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(private.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(private, hashes.SHA256())
    )
    write_private(key, private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                             serialization.NoEncryption()).decode())
    write_private(cert, certificate.public_bytes(serialization.Encoding.PEM).decode())


def certificate_fingerprint(cert: Path) -> str:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes

    return x509.load_pem_x509_certificate(cert.read_bytes()).fingerprint(hashes.SHA256()).hex(":").upper()


def connect_out(app: web.Application, env: dict[str, str], renderer: Renderer, state: Path,
                identity: dict[str, str], encoder: str, host: str) -> None:
    """A controller elsewhere (docs/renderer-connect-out.md): the connection out, the filter its
    pages go through, and the listener the site's screens fetch their streams from."""
    from .connect import Link
    from .filter import Filter, Places

    stream_port = int(env.get("STREAM_PORT", "8781"))
    try:
        places = Places.parse(env.get("SITE_PAGES", ""))
        page_filter = Filter(places)
        # The app can't see its machine's address on the site's network: it's given, or the
        # controller passes the streams on itself.
        given = (env.get("STREAM_URL") or "").strip().rstrip("/")
        if given and (urlsplit(given).scheme not in ("http", "https") or not urlsplit(given).hostname or urlsplit(given).path):
            raise ValueError("stream_url is http://<this machine's address>:<port>")
        addon = Path("/data/options.json").exists()
        streams = [given] if given else [] if addon else [f"http://{local_address()}:{stream_port}"]

        def about() -> dict[str, Any]:
            return {**identity, "version": VERSION, "encoder": encoder, "max_screens": renderer.max_sessions,
                    "load": load(), "streams": streams,
                    "sessions": {key[1:]: {"url": s.screen.url, "width": s.screen.width, "height": s.screen.height,
                                           "running": s.running}
                                 for key, s in renderer.sessions.items() if key.startswith("~")}}

        async def removed() -> None:
            for key in [k for k in renderer.sessions if k.startswith("~")]:
                await renderer.stop(key)

        link = Link(state, env["CONTROLLER"], env.get("CONTROLLER_FINGERPRINT"),
                    calls=controller_calls(renderer, about, lambda: page_filter.port), info=about,
                    write=write_private, describe=describe_call, on_removed=removed)
    except ValueError as err:
        _LOGGER.error("Not connecting out: %s", err)
        return

    async def run(app: web.Application) -> Any:
        await page_filter.start()
        streams_runner = web.AppRunner(create_stream_app(renderer), access_log=None)
        await streams_runner.setup()
        await web.TCPSite(streams_runner, host, stream_port).start()
        _LOGGER.info("Connecting out to the controller at %s. Pages from it reach the public internet%s.",
                     link.url, f" and {', '.join(sorted(f'{h}:{p}' for h, p in places.hosts) + [str(n) for n in places.networks])}"
                     if places else " only")
        if streams:
            _LOGGER.info("The site's screens fetch their streams at %s", streams[0])
        else:
            _LOGGER.info("The site's screens get their streams through the controller. To have them fetched here, "
                         "set the streams address to this machine's address, such as http://192.168.1.10:%d", stream_port)
        link.start()
        yield
        await link.stop()
        await streams_runner.cleanup()
        await page_filter.stop()

    app.cleanup_ctx.append(run)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    env = settings()
    root = Path(env.get("DATA", "/tmp/pleatview-renderer"))
    root.mkdir(parents=True, exist_ok=True)
    encoder = asyncio.run(detect_encoder(env.get("ENCODER", "auto")))
    port = int(env.get("PORT", "8780"))
    host = env.get("HOST", "0.0.0.0")
    addon = Path("/data/options.json").exists()
    state = state_folder(env)
    ident_file = state / "renderer.json"
    identity = json.loads(ident_file.read_text()) if ident_file.exists() else {}
    if not identity.get("id"):
        identity = {"id": secrets.token_hex(16)}
        write_private(ident_file, json.dumps(identity))
    identity = {"id": identity["id"], "name": env.get("NAME") or socket.gethostname()}

    def pin_changed() -> None:
        _LOGGER.info("To pair this renderer with Home Assistant, use the PIN %s %s",
                     pairing.pin[:3], pairing.pin[3:])

    tokens = state / "tokens.json"
    pairing = Pairing(lambda: tokens.read_text() if tokens.exists() else None,
                      lambda text: write_private(tokens, text), on_changed=pin_changed)
    # On the network (not the Home Assistant OS app, not 127.0.0.1), only paired clients get in.
    require = env.get("REQUIRE_TOKEN", "auto")
    require_token = require == "1" or (require == "auto" and not addon and not is_loopback(host))
    context = None
    cert, key = env.get("TLS_CERT"), env.get("TLS_KEY")
    # On the network the renderer serves HTTPS, with a certificate it makes for itself unless
    # it's given one; the controller pins it when pairing. TLS=off turns it off.
    if not (cert and key) and require_token and env.get("TLS", "auto") != "off":
        cert, key = str(state / "cert.pem"), str(state / "key.pem")
        make_certificate(Path(cert), Path(key))
    if cert and key:
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(cert, key)
        _LOGGER.info("Certificate fingerprint (SHA-256): %s", certificate_fingerprint(Path(cert)))
    _LOGGER.info("PleatView Renderer %s, encoding with %s", VERSION, encoder)
    scheme = "https" if context else "http"
    _LOGGER.info("In PleatView's options, use the renderer address %s",
                 advertised_address(env, port).replace("http://", f"{scheme}://", 1))
    pin_changed()
    renderer = Renderer(root, encoder, int(env.get("MAX_SCREENS", "4")))
    app = create_app(root, encoder, pairing=pairing, require_token=require_token, identity=identity, renderer=renderer)
    if (env.get("CONTROLLER") or "").strip():
        connect_out(app, env, renderer, state, identity, encoder, host)
    if not addon and not is_loopback(host) and env.get("ANNOUNCE", "1") != "0":
        responder = Responder(lambda: {**identity, "version": VERSION, "scheme": scheme}, port,
                              service=SERVICE, keys=("id", "name", "version", "scheme"))

        async def announce(app: web.Application):
            try:
                await responder.start()
            except OSError as err:
                _LOGGER.warning("Can't announce over mDNS: %s", err)
            yield
            responder.stop()

        app.cleanup_ctx.append(announce)
        _LOGGER.info("Announcing itself on the network at %s:%d", local_address(), port)
    web.run_app(app, host=host, port=port, ssl_context=context, print=None)
