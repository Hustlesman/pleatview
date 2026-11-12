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

VERSION = "1.0.17"
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


def create_app(root: Path, encoder: str, max_sessions: int = 4, *, pairing: Pairing | None = None,
               require_token: bool = False, identity: dict[str, str] | None = None) -> web.Application:
    sessions: dict[str, Session] = {}
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
                for key, s in sessions.items()
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
        session = sessions.get(key)
        if session is None:
            if len(sessions) >= max_sessions:
                raise web.HTTPTooManyRequests(text=f"this renderer shows at most {max_sessions} screens")
            session = sessions[key] = Session(key, screen, root, encoder)
            session.start()
        elif session.screen != screen:
            await session.update(screen)
        session.last_access = time.monotonic()
        session.start()
        return web.json_response({"playlist": f"/hls/{key}/index.m3u8"})

    async def put_overlay(request: web.Request) -> web.Response:
        session = sessions.get(request.match_info["key"])
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
        session = sessions.pop(request.match_info["key"], None)
        if session:
            await session.stop()
        return web.Response(status=204)

    async def hls(request: web.Request) -> web.StreamResponse:
        session = sessions.get(request.match_info["key"])
        name = request.match_info["name"]
        if session is None or not FILE.match(name):
            raise web.HTTPNotFound()
        session.last_access = time.monotonic()
        if not session.running:
            _LOGGER.info("%s: its TV is back; starting", session.key)
            session.start()
        path = session.folder / name
        deadline = time.monotonic() + START_WAIT_SECONDS
        while not path.exists() and name == "index.m3u8" and time.monotonic() < deadline:
            await asyncio.sleep(0.25)
        if not path.exists():
            raise web.HTTPNotFound()
        cache = "no-cache" if name.endswith(".m3u8") else "max-age=60"
        content_type = "application/vnd.apple.mpegurl" if name.endswith(".m3u8") else "video/mp2t"
        try:
            body = await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError as err:  # an old segment ffmpeg just deleted
            raise web.HTTPNotFound() from err
        return web.Response(body=body, content_type=content_type, headers={"Cache-Control": cache})

    async def idle(app: web.Application) -> Any:
        async def check() -> None:
            while True:
                await asyncio.sleep(15)
                for session in list(sessions.values()):
                    if session.running and time.monotonic() - session.last_access > IDLE_SECONDS:
                        _LOGGER.info("%s: nobody watches; stopping until its TV asks again", session.key)
                        await session.stop()

        task = asyncio.create_task(check())
        yield
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        for session in sessions.values():
            await session.stop()

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
        values.update({key.upper(): str(value) for key, value in json.loads(options.read_text()).items()})
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
        _LOGGER.info("To pair this renderer with Home Assistant or PleatView Console, use the PIN %s %s",
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
    # it's given one; Home Assistant and the console pin it when pairing. TLS=off turns it off.
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
    app = create_app(root, encoder, max_sessions=int(env.get("MAX_SCREENS", "4")), pairing=pairing,
                     require_token=require_token, identity=identity)
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
