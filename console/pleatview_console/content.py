"""What screens load from the console: playlist pages and media files.

Served on a listener of its own (plain HTTP on the local network, like protocol v1), apart
from the web app: no sign-in, no API, nothing but these. Each page and each media file is
reached through a random token in its address, which only the screens it's given to know.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from aiohttp import web

from .media import PLAYABLE_TYPES, folder
from .store import TENANT_ID, Store

WEB = Path(__file__).with_name("web")
TOKEN = re.compile(r"^[A-Za-z0-9_-]{20,40}$")
FILE = re.compile(r"^play\.(mp4|m4a|mp3|jpg|png|webp|gif)$")
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; media-src 'self'; "
       "connect-src 'self'; base-uri 'none'; form-action 'none'")
DEFAULT_SECONDS = 10

STORE = web.AppKey("store", Store)

PLAYER_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PleatView</title>
  <link rel="stylesheet" href="/play/player.css">
  <script src="/play/player.js" defer></script>
</head>
<body><main id="stage"></main></body>
</html>
"""


@web.middleware
async def headers(request: web.Request, handler) -> web.StreamResponse:
    try:
        response = await handler(request)
    except web.HTTPException as err:
        err.headers["Content-Security-Policy"] = CSP
        raise
    if not response.prepared:
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _tenant(request: web.Request):
    tenant_id, token = request.match_info["tenant"], request.match_info["token"]
    if not TENANT_ID.match(tenant_id) or not TOKEN.match(token):
        raise web.HTTPNotFound()
    try:
        return tenant_id, request.app[STORE].tenant(tenant_id), token
    except KeyError:
        raise web.HTTPNotFound() from None


def _page(request: web.Request) -> tuple[str, Any, dict[str, Any]]:
    tenant_id, tenant, token = _tenant(request)
    page = tenant.page_by_token(token)
    if not page or page["kind"] != "playlist":
        raise web.HTTPNotFound()
    return tenant_id, tenant, page


async def player(request: web.Request) -> web.Response:
    _page(request)
    return web.Response(text=PLAYER_PAGE, content_type="text/html", headers={"Cache-Control": "no-cache"})


async def items(request: web.Request) -> web.Response:
    """The playlist's media that are ready, in order, with their addresses."""
    tenant_id, tenant, page = _page(request)
    out = []
    for entry in page["items"]:
        item = tenant.media_item(entry.get("media_id", 0))
        if not item or item["status"] != "ready":
            continue
        out.append({
            "kind": item["kind"],
            "url": f"/play/{tenant_id}/m/{item['token']}/{item['playable']}",
            "seconds": entry.get("seconds") or DEFAULT_SECONDS,
        })
    return web.json_response({"items": out}, headers={"Cache-Control": "no-store"})


async def media_file(request: web.Request) -> web.StreamResponse:
    tenant_id, tenant, token = _tenant(request)
    name = request.match_info["file"]
    item = tenant.media_by_token(token)
    if not item or item["status"] != "ready" or item["playable"] != name or not FILE.match(name):
        raise web.HTTPNotFound()
    path = folder(request.app[STORE].data, tenant_id, item["id"]) / name
    if not path.is_file():
        raise web.HTTPNotFound()
    # A media file's address never changes content (a new upload gets a new token), so the
    # screen may keep it a week: a playlist then plays on while the console can't be reached.
    return web.FileResponse(path, headers={
        "Content-Type": PLAYABLE_TYPES[name.rsplit(".", 1)[1]],
        "Cache-Control": "private, max-age=604800, immutable",
    })


# The player's own files: paths from this table, never from the request.
ASSETS = {
    "player.js": (WEB / "player.js", "text/javascript"),
    "player.css": (WEB / "player.css", "text/css"),
}


async def asset(request: web.Request) -> web.Response:
    found = ASSETS.get(request.match_info["name"])
    if found is None:
        raise web.HTTPNotFound()
    path, kind = found
    return web.Response(body=path.read_bytes(), content_type=kind, charset="utf-8",
                        headers={"Cache-Control": "no-cache"})


STREAM_FILE = re.compile(r"^(index\.m3u8|s\d{1,9}\.ts)$")
RENDERING = web.AppKey("rendering", object)


async def rendered(request: web.Request) -> web.Response:
    """A rendered screen's stream, passed on from its renderer (rendering.py)."""
    name, secret = request.match_info["file"], request.match_info["secret"]
    rendering = request.app.get(RENDERING)
    if rendering is None or not STREAM_FILE.match(name) or not re.fullmatch(r"[0-9a-f]{32}", secret):
        raise web.HTTPNotFound()
    status, body, content_type, cache = await rendering.fetch(secret, name)
    if status != 200:
        raise web.HTTPNotFound() if status == 404 else web.HTTPBadGateway()
    return web.Response(body=body, content_type=content_type, headers={"Cache-Control": cache})


def create_content_app(store: Store, rendering: object | None = None) -> web.Application:
    app = web.Application(middlewares=[headers], client_max_size=1024)
    app[STORE] = store
    if rendering is not None:
        app[RENDERING] = rendering
    app.router.add_get("/play/{name:player\\.(js|css)}", asset)
    app.router.add_get("/play/{tenant}/p/{token}", player)
    app.router.add_get("/play/{tenant}/p/{token}/items", items)
    app.router.add_get("/play/{tenant}/m/{token}/{file}", media_file)
    app.router.add_get("/play/{tenant}/r/{secret}/{file}", rendered)
    return app
