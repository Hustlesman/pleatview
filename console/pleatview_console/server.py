"""The console's web server: the JSON API under /api and the web app's files.

Every tenant route is /api/t/<tenant>/..., and each one checks the user's membership of that
tenant before opening its database.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import re
from typing import Any, Awaitable, Callable

from aiohttp import web

from . import VERSION
from .auth import DUMMY_HASH, Throttle, check_new_password, check_password, hash_password
from .screens import Invalid, Screens, check_address, command_params
from .store import ROLES, TENANT_ID, Store
from .tvapi import CannotConnect, InvalidPin, PleatViewError, RequestRejected, TooManyAttempts

_LOGGER = logging.getLogger(__name__)

WEB = Path(__file__).with_name("web")
PACKAGES = Path("/app-packages.txt")
COOKIE = "pv_session"
NAME_MAX = 100
MAY = {
    "view": {"owner", "operator", "viewer"},
    "operate": {"owner", "operator"},
    "own": {"owner"},
}
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; manifest-src 'self'; worker-src 'self'; object-src 'none'; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".webmanifest": "application/manifest+json",
    ".png": "image/png",
    ".svg": "image/svg+xml",
}

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

STORE = web.AppKey("store", Store)
SCREENS = web.AppKey("screens", Screens)
THROTTLE = web.AppKey("throttle", Throttle)
HTTPS = web.AppKey("https", bool)


def fail(status: int, message: str, **extra: Any) -> web.HTTPException:
    cls = {400: web.HTTPBadRequest, 401: web.HTTPUnauthorized, 403: web.HTTPForbidden,
           404: web.HTTPNotFound, 409: web.HTTPConflict, 429: web.HTTPTooManyRequests,
           502: web.HTTPBadGateway}[status]
    return cls(text=json.dumps({"error": message, **extra}), content_type="application/json")


async def body(request: web.Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise fail(400, "The request isn't JSON") from None
    if not isinstance(data, dict):
        raise fail(400, "The request isn't a JSON object")
    return data


def name_field(data: dict[str, Any], key: str, label: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > NAME_MAX:
        raise fail(400, f"{label} is required, up to {NAME_MAX} characters")
    return value.strip()


def client_address(request: web.Request) -> str:
    return request.remote or "unknown"


# ---- Middleware ----------------------------------------------------------------------


@web.middleware
async def security(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Security headers on everything, and a check that changes come from our own page."""
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.path.startswith("/api/"):
        # A custom header can't be sent cross-site without a CORS preflight, which we never allow.
        if request.headers.get("X-PleatView") != "1":
            raise fail(403, "Missing the console's request header")
        origin = request.headers.get("Origin")
        if origin and origin.split("://", 1)[-1] != request.host:
            raise fail(403, "Request from another site")
    try:
        response = await handler(request)
    except web.HTTPException as err:
        _headers(err)
        raise
    if not response.prepared:  # the event stream sets its own before it starts
        _headers(response)
    return response


def _headers(response: web.StreamResponse) -> None:
    response.headers.setdefault("Content-Security-Policy", CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")


def current_user(request: web.Request) -> dict[str, Any] | None:
    token = request.cookies.get(COOKIE)
    return request.app[STORE].session_user(token) if token else None


def signed_in(request: web.Request) -> dict[str, Any]:
    user = current_user(request)
    if not user:
        raise fail(401, "Sign in first")
    return user


def member(request: web.Request, need: str) -> tuple[dict[str, Any], str, str]:
    """The user, the tenant in the address and the user's role there, or an error.

    A tenant that doesn't exist and one the user isn't a member of give the same answer, so
    nobody learns which tenants exist."""
    user = signed_in(request)
    tenant_id = request.match_info["tenant"]
    role = request.app[STORE].role(user["id"], tenant_id) if TENANT_ID.match(tenant_id) else None
    if role is None:
        raise fail(404, "No such tenant")
    if role not in MAY[need]:
        raise fail(403, "Your role doesn't allow that")
    return user, tenant_id, role


def set_session(request: web.Request, response: web.Response, user_id: int) -> None:
    token = request.app[STORE].add_session(user_id)
    response.set_cookie(
        COOKIE, token, httponly=True, samesite="Strict", path="/",
        secure=request.app[HTTPS] or request.secure,
    )


def me(store: Store, user: dict[str, Any]) -> dict[str, Any]:
    return {
        "user": {"id": user["id"], "username": user["username"], "name": user["name"], "admin": bool(user["admin"])},
        "tenants": store.memberships(user["id"]),
    }


# ---- Sign-in -------------------------------------------------------------------------


async def status(request: web.Request) -> web.Response:
    store = request.app[STORE]
    user = current_user(request)
    return web.json_response({
        "version": VERSION,
        "setup": store.user_count() == 0,
        **(me(store, user) if user else {"user": None}),
    })


async def setup(request: web.Request) -> web.Response:
    """First start: the first administrator, with a tenant and a site to begin with."""
    store = request.app[STORE]
    data = await body(request)
    if store.user_count() > 0:
        raise fail(409, "The console is already set up")
    username = name_field(data, "username", "A user name")
    name = name_field(data, "name", "Your name")
    tenant_name = name_field(data, "tenant", "An organisation name")
    site_name = name_field(data, "site", "A site name")
    password = data.get("password")
    if not isinstance(password, str) or (problem := check_new_password(password)):
        raise fail(400, problem or "A password is required")
    password_hash = await asyncio.to_thread(hash_password, password)
    if store.user_count() > 0:  # a second request raced this one
        raise fail(409, "The console is already set up")
    user_id = store.add_user(username, name, password_hash, admin=True)
    tenant_id = store.add_tenant(tenant_name)
    store.set_member(user_id, tenant_id, "owner")
    tenant = store.tenant(tenant_id)
    tenant.add_site(site_name)
    tenant.log("tenant_created", user=username, address=client_address(request))
    _LOGGER.info("Set up by %s", username)
    response = web.json_response(me(store, store.user(user_id)))
    set_session(request, response, user_id)
    return response


async def login(request: web.Request) -> web.Response:
    store, throttle = request.app[STORE], request.app[THROTTLE]
    data = await body(request)
    username, password = data.get("username"), data.get("password")
    if not isinstance(username, str) or not isinstance(password, str) or len(password) > 1024:
        raise fail(400, "User name and password are required")
    keys = (f"user:{username.lower()}", f"address:{client_address(request)}")
    if wait := throttle.wait(*keys):
        raise fail(429, f"Too many tries. Wait {wait} seconds.", retry_after=wait)
    user = store.user_by_name(username)
    ok = await asyncio.to_thread(check_password, password, user["password"] if user else DUMMY_HASH)
    if not user or not ok:
        throttle.failed(*keys)
        _LOGGER.warning("Failed sign-in for %r from %s", username[:100], client_address(request))
        raise fail(401, "Wrong user name or password")
    throttle.succeeded(*keys)
    store.prune_sessions()
    response = web.json_response(me(store, user))
    set_session(request, response, user["id"])
    return response


async def logout(request: web.Request) -> web.Response:
    if token := request.cookies.get(COOKIE):
        request.app[STORE].delete_session(token)
    response = web.json_response({})
    response.del_cookie(COOKIE, path="/")
    return response


async def change_password(request: web.Request) -> web.Response:
    store = request.app[STORE]
    user = signed_in(request)
    data = await body(request)
    current, new = data.get("current"), data.get("password")
    if not isinstance(current, str) or not isinstance(new, str):
        raise fail(400, "The current and the new password are required")
    if not await asyncio.to_thread(check_password, current, user["password"]):
        raise fail(403, "The current password is wrong")
    if problem := check_new_password(new):
        raise fail(400, problem)
    store.set_password(user["id"], await asyncio.to_thread(hash_password, new))
    response = web.json_response({})
    set_session(request, response, user["id"])  # every other session has ended
    return response


# ---- Tenants and sites -----------------------------------------------------------------


async def add_tenant(request: web.Request) -> web.Response:
    """Console administrators add tenants, and become the first owner (in the tenant's log)."""
    store = request.app[STORE]
    user = signed_in(request)
    if not user["admin"]:
        raise fail(403, "Only console administrators add tenants")
    data = await body(request)
    tenant_id = store.add_tenant(name_field(data, "name", "A name"))
    store.set_member(user["id"], tenant_id, "owner")
    tenant = store.tenant(tenant_id)
    tenant.add_site(name_field(data, "site", "A first site") if data.get("site") else "Main site")
    tenant.log("tenant_created", user=user["username"], address=client_address(request))
    return web.json_response({"id": tenant_id, **me(store, user)})


async def tenant_overview(request: web.Request) -> web.Response:
    store = request.app[STORE]
    _, tenant_id, role = member(request, "view")
    return web.json_response({
        "id": tenant_id,
        "name": store.tenant_name(tenant_id),
        "role": role,
        "sites": store.tenant(tenant_id).sites(),
    })


async def add_site(request: web.Request) -> web.Response:
    user, tenant_id, _ = member(request, "own")
    tenant = request.app[STORE].tenant(tenant_id)
    name = name_field(await body(request), "name", "A name")
    site_id = tenant.add_site(name)
    tenant.log("site_added", user=user["username"], target=f"site:{site_id}", detail={"name": name},
               address=client_address(request))
    return web.json_response({"id": site_id, "sites": tenant.sites()})


async def change_site(request: web.Request) -> web.Response:
    user, tenant_id, _ = member(request, "own")
    tenant = request.app[STORE].tenant(tenant_id)
    site_id = int(request.match_info["site"])
    if not tenant.site(site_id):
        raise fail(404, "No such site")
    if request.method == "DELETE":
        if not tenant.delete_site(site_id):
            raise fail(409, "Move or remove the site's screens first")
        tenant.log("site_removed", user=user["username"], target=f"site:{site_id}", address=client_address(request))
    else:
        name = name_field(await body(request), "name", "A name")
        tenant.rename_site(site_id, name)
        tenant.log("site_renamed", user=user["username"], target=f"site:{site_id}", detail={"name": name},
                   address=client_address(request))
    return web.json_response({"sites": tenant.sites()})


async def audit(request: web.Request) -> web.Response:
    _, tenant_id, _ = member(request, "view")
    before = request.query.get("before")
    if before is not None and not before.isdigit():
        raise fail(400, "before must be a number")
    entries = request.app[STORE].tenant(tenant_id).audit(int(before) if before else None)
    return web.json_response({"entries": entries})


# ---- Screens ---------------------------------------------------------------------------


async def list_screens(request: web.Request) -> web.Response:
    _, tenant_id, _ = member(request, "view")
    screens = request.app[SCREENS]
    return web.json_response({"screens": screens.list(tenant_id), "discovered": screens.discovered(tenant_id)})


async def pair_screen(request: web.Request) -> web.Response:
    user, tenant_id, _ = member(request, "operate")
    screens = request.app[SCREENS]
    data = await body(request)
    site_id, pin = data.get("site_id"), data.get("pin")
    if not isinstance(site_id, int) or isinstance(site_id, bool):
        raise fail(400, "Choose a site")
    if device_id := data.get("device_id"):
        found = screens.browser.found().get(device_id) if screens.browser else None
        if not found:
            raise fail(404, "That screen isn't on the network any more")
        host, port = found["host"], found["port"]
    else:
        try:
            host, port = check_address(data.get("host", ""), data.get("port", 8765))
        except Invalid as err:
            raise fail(400, str(err)) from None
    try:
        screen_id = await screens.pair(tenant_id, site_id, host, port, pin)
    except Invalid as err:
        raise fail(400, str(err)) from None
    except InvalidPin:
        raise fail(403, "Wrong PIN. Check the PIN on the screen; it changes after each pairing.") from None
    except TooManyAttempts as err:
        raise fail(429, "Too many wrong PINs. The screen shows a new one in a minute.",
                   retry_after=err.args[0] if err.args else 60) from None
    except CannotConnect:
        raise fail(502, "The screen can't be reached at that address") from None
    except PleatViewError as err:
        raise fail(502, f"The screen answered: {err}") from None
    tenant = request.app[STORE].tenant(tenant_id)
    tenant.log("screen_paired", user=user["username"], target=f"screen:{screen_id}",
               detail={"host": host, "port": port, "site_id": site_id}, address=client_address(request))
    return web.json_response({"id": screen_id})


def _screen(request: web.Request, tenant_id: str) -> dict[str, Any]:
    raw = request.match_info["screen"]
    screen = request.app[STORE].tenant(tenant_id).screen(int(raw)) if raw.isdigit() else None
    if not screen:
        raise fail(404, "No such screen")
    return screen


async def change_screen(request: web.Request) -> web.Response:
    user, tenant_id, _ = member(request, "operate")
    screen = _screen(request, tenant_id)
    tenant = request.app[STORE].tenant(tenant_id)
    data = await body(request)
    changes: dict[str, Any] = {}
    if "name" in data:
        changes["name"] = name_field(data, "name", "A name")
    if "site_id" in data:
        if not isinstance(data["site_id"], int) or not tenant.site(data["site_id"]):
            raise fail(400, "No such site")
        changes["site_id"] = data["site_id"]
    if not changes:
        raise fail(400, "Nothing to change")
    tenant.update_screen(screen["id"], **changes)
    tenant.log("screen_changed", user=user["username"], target=f"screen:{screen['id']}", detail=changes,
               address=client_address(request))
    request.app[SCREENS].publish(tenant_id, screen["id"])
    return web.json_response({})


async def remove_screen(request: web.Request) -> web.Response:
    user, tenant_id, _ = member(request, "operate")
    screen = _screen(request, tenant_id)
    await request.app[SCREENS].unpair(tenant_id, screen["id"])
    request.app[STORE].tenant(tenant_id).log(
        "screen_removed", user=user["username"], target=f"screen:{screen['id']}",
        detail={"name": screen["name"]}, address=client_address(request))
    return web.json_response({})


async def screen_command(request: web.Request) -> web.Response:
    user, tenant_id, _ = member(request, "operate")
    screen = _screen(request, tenant_id)
    data = await body(request)
    command = data.get("command")
    try:
        params = command_params(command if isinstance(command, str) else "", data)
    except Invalid as err:
        raise fail(400, str(err)) from None
    try:
        await request.app[SCREENS].command(tenant_id, screen["id"], command, params)
    except Invalid as err:
        raise fail(404, str(err)) from None
    except CannotConnect:
        raise fail(502, "The screen can't be reached") from None
    except RequestRejected as err:
        raise fail(400, f"The screen refused: {err}") from None
    except PleatViewError as err:
        raise fail(502, f"The screen answered: {err}") from None
    request.app[STORE].tenant(tenant_id).log(
        "command", user=user["username"], target=f"screen:{screen['id']}",
        detail={"command": command, **{k: v for k, v in params.items() if v is not None}},
        address=client_address(request))
    return web.json_response({})


async def events(request: web.Request) -> web.StreamResponse:
    """Server-sent events for the web app: a screen changed, was removed, or the network's
    list of screens changed."""
    _, tenant_id, _ = member(request, "view")
    screens = request.app[SCREENS]
    queue = screens.subscribe(tenant_id)
    response = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-store"})
    _headers(response)
    await response.prepare(request)
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=20)
            except asyncio.TimeoutError:
                await response.write(b": ping\n\n")
                continue
            await response.write(f"data: {json.dumps(event)}\n\n".encode())
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        screens.unsubscribe(tenant_id, queue)
    return response


async def packages(request: web.Request) -> web.Response:
    """Every installed package and version, for audits (administrators only)."""
    if not signed_in(request)["admin"]:
        raise fail(403, "Only console administrators")
    text = PACKAGES.read_text() if PACKAGES.exists() else ""
    return web.Response(text=text, content_type="text/plain")


# ---- The web app -----------------------------------------------------------------------

SAFE_FILE = re.compile(r"^[a-z0-9-]+\.(html|js|css|webmanifest|png|svg)$")


async def static(request: web.Request) -> web.Response:
    name = request.match_info.get("name") or "index.html"
    path = WEB / name
    if not SAFE_FILE.match(name) or not path.is_file():
        raise web.HTTPNotFound()
    headers = {"Cache-Control": "no-cache"}
    if name == "sw.js":
        headers["Service-Worker-Allowed"] = "/"
    return web.Response(body=path.read_bytes(), content_type=STATIC_TYPES[path.suffix].split(";")[0],
                        charset="utf-8" if "charset" in STATIC_TYPES[path.suffix] else None, headers=headers)


def create_app(store: Store, screens: Screens, *, https: bool) -> web.Application:
    app = web.Application(middlewares=[security], client_max_size=64 * 1024)
    app[STORE] = store
    app[SCREENS] = screens
    app[THROTTLE] = Throttle()
    app[HTTPS] = https
    app.router.add_get("/api/status", status)
    app.router.add_post("/api/setup", setup)
    app.router.add_post("/api/login", login)
    app.router.add_post("/api/logout", logout)
    app.router.add_post("/api/password", change_password)
    app.router.add_post("/api/tenants", add_tenant)
    app.router.add_get("/api/packages", packages)
    t = "/api/t/{tenant}"
    app.router.add_get(t, tenant_overview)
    app.router.add_post(t + "/sites", add_site)
    app.router.add_patch(t + "/sites/{site:\\d+}", change_site)
    app.router.add_delete(t + "/sites/{site:\\d+}", change_site)
    app.router.add_get(t + "/audit", audit)
    app.router.add_get(t + "/screens", list_screens)
    app.router.add_post(t + "/screens", pair_screen)
    app.router.add_patch(t + "/screens/{screen}", change_screen)
    app.router.add_delete(t + "/screens/{screen}", remove_screen)
    app.router.add_post(t + "/screens/{screen}/command", screen_command)
    app.router.add_get(t + "/events", events)
    app.router.add_get("/", static)
    app.router.add_get("/{name}", static)
    return app
