"""HTTP API of the PleatView Renderer.

The PleatView integration says what each TV shows (``PUT /api/sessions/<key>``) and Home
Assistant passes the TV the stream (``GET /hls/<key>/index.m3u8``). A screen nobody has
fetched for a while stops, and starts again when its TV asks for it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
import re
import time
from typing import Any
from urllib.parse import urlsplit

from aiohttp import web

from .session import Screen, Session

_LOGGER = logging.getLogger(__name__)

VERSION = "1.0.16"
KEY = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
FILE = re.compile(r"^(index\.m3u8|s\d{1,9}\.ts)$")
KIOSK_MODES = ("full", "sidebar", "off")
# A screen whose TV hasn't fetched its stream for this long stops until it does again.
IDLE_SECONDS = 90
# How long a TV's first request waits for a starting screen's first segments.
START_WAIT_SECONDS = 8

def parse_screen(body: Any) -> Screen:
    """A screen from a request body; ValueError says what's wrong."""
    if not isinstance(body, dict):
        raise ValueError("expected a JSON object")
    url = body.get("url")
    if not isinstance(url, str) or urlsplit(url).scheme not in ("http", "https") or not urlsplit(url).netloc:
        raise ValueError("url must be an http or https address")
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
    # Even sizes: H.264 in 4:2:0 needs them.
    return Screen(url=url, width=width // 2 * 2, height=height // 2 * 2, scale=round(float(scale), 3),
                  kiosk=kiosk, ha_url=ha_url, token=token)


def create_app(root: Path, encoder: str, max_sessions: int = 4) -> web.Application:
    app = web.Application()
    sessions: dict[str, Session] = {}

    async def info(request: web.Request) -> web.Response:
        return web.json_response({
            "version": VERSION,
            "encoder": encoder,
            "sessions": {
                key: {"url": s.screen.url, "width": s.screen.width, "height": s.screen.height, "running": s.running}
                for key, s in sessions.items()
            },
        })

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
    app.router.add_get("/api/packages", packages)
    app.router.add_put("/api/sessions/{key}", put_session)
    app.router.add_delete("/api/sessions/{key}", delete_session)
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


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    env = settings()
    root = Path(env.get("DATA", "/tmp/pleatview-renderer"))
    root.mkdir(parents=True, exist_ok=True)
    encoder = asyncio.run(detect_encoder(env.get("ENCODER", "auto")))
    port = int(env.get("PORT", "8780"))
    _LOGGER.info("PleatView Renderer %s, encoding with %s", VERSION, encoder)
    _LOGGER.info("In PleatView's options, use the renderer address %s", advertised_address(env, port))
    app = create_app(root, encoder, max_sessions=int(env.get("MAX_SCREENS", "4")))
    web.run_app(app, host=env.get("HOST", "0.0.0.0"), port=port, print=None)
