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
import secrets
from typing import Any, Awaitable, Callable

import aiohttp
from aiohttp import web

from . import VERSION, rendering, schedule, totp
from .auth import DUMMY_HASH, Throttle, check_new_password, check_password, hash_password
from .mdns import RENDERER_SERVICE
from .media import Library, folder
from .screens import Invalid, Screens, check_address, command_params, web_url
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
LIBRARY = web.AppKey("library", Library)
MEDIA_MAX = web.AppKey("media_max", int)
THROTTLE = web.AppKey("throttle", Throttle)
HTTPS = web.AppKey("https", bool)


def fail(status: int, message: str, **extra: Any) -> web.HTTPException:
    cls = {400: web.HTTPBadRequest, 401: web.HTTPUnauthorized, 403: web.HTTPForbidden,
           404: web.HTTPNotFound, 409: web.HTTPConflict, 413: web.HTTPRequestEntityTooLarge,
           429: web.HTTPTooManyRequests,
           502: web.HTTPBadGateway}[status]
    if status == 413:
        return web.HTTPRequestEntityTooLarge(0, 0, text=json.dumps({"error": message, **extra}),
                                             content_type="application/json")
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


def signed_in(request: web.Request, *, restricted_ok: bool = False) -> dict[str, Any]:
    """The signed-in user. Until a one-time password is changed, or two-factor sign-in is set
    up where the console requires it, only the account's own pages answer."""
    user = current_user(request)
    if not user:
        raise fail(401, "Sign in first")
    if not restricted_ok:
        if user["must_change"]:
            raise fail(403, "Choose your own password first", action="change_password")
        if request.app[STORE].setting("require_2fa", False) and not user["totp"]:
            raise fail(403, "Set up two-factor sign-in first", action="two_factor")
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
        "user": {
            "id": user["id"], "username": user["username"], "name": user["name"],
            "admin": bool(user["admin"]), "two_factor": bool(user["totp"]),
            "must_change": bool(user["must_change"]),
        },
        "require_2fa": bool(store.setting("require_2fa", False)),
        "tenants": store.memberships(user["id"]),
    }


async def confirm_password(request: web.Request, user: dict[str, Any], password: Any) -> None:
    """Asks for the password again before a change to the account's security, as slowly as
    the sign-in page lets anyone guess."""
    throttle, key = request.app[THROTTLE], f"user:{user['username'].lower()}"
    if wait := throttle.wait(key):
        raise fail(429, f"Too many tries. Wait {wait} seconds.", retry_after=wait)
    if not isinstance(password, str) or not await asyncio.to_thread(check_password, password, user["password"]):
        throttle.failed(key)
        raise fail(403, "The password is wrong")
    throttle.succeeded(key)


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
    tenant.add_site(site_name, _timezone(data))
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
    if user["totp"]:
        code = data.get("code")
        if not isinstance(code, str) or not code.strip():
            # The password was right: ask for the second factor, without counting a failure.
            raise fail(401, "Enter the 6-digit code from your authenticator app, or a recovery code.",
                       two_factor=True)
        step = totp.verify(store.totp_secret(user), code, user["totp_step"])
        if step is not None:
            store.set_totp_step(user["id"], step)
        elif not store.use_recovery(user["id"], totp.recovery_hash(code)):
            throttle.failed(*keys)
            raise fail(401, "Wrong code. Codes change every 30 seconds; each works once.", two_factor=True)
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
    user = signed_in(request, restricted_ok=True)
    data = await body(request)
    current, new = data.get("current"), data.get("password")
    if not isinstance(current, str) or not isinstance(new, str):
        raise fail(400, "The current and the new password are required")
    await confirm_password(request, user, current)
    if problem := check_new_password(new):
        raise fail(400, problem)
    if new == current:
        raise fail(400, "Choose a password other than the current one.")
    store.set_password(user["id"], await asyncio.to_thread(hash_password, new))
    response = web.json_response(me(store, store.user(user["id"])))
    set_session(request, response, user["id"])  # every other session has ended
    return response


# ---- Two-factor sign-in ----------------------------------------------------------------


async def account(request: web.Request) -> web.Response:
    store = request.app[STORE]
    user = signed_in(request, restricted_ok=True)
    return web.json_response({**me(store, user), "recovery_left": store.recovery_left(user["id"])})


async def two_factor_setup(request: web.Request) -> web.Response:
    """A new secret to put in an authenticator app; it counts once a code from it is entered."""
    store = request.app[STORE]
    user = signed_in(request, restricted_ok=True)
    await confirm_password(request, user, (await body(request)).get("password"))
    secret = totp.new_secret()
    store.set_totp_pending(user["id"], secret)
    return web.json_response({"secret": secret, "uri": totp.uri(secret, user["username"])})


async def two_factor_enable(request: web.Request) -> web.Response:
    store = request.app[STORE]
    user = signed_in(request, restricted_ok=True)
    pending = store.totp_secret(user, pending=True)
    if not pending:
        raise fail(409, "Start setting up two-factor sign-in first")
    code = (await body(request)).get("code")
    step = totp.verify(pending, code, 0) if isinstance(code, str) else None
    if step is None:
        raise fail(400, "That code doesn't match. Check the phone's clock, and enter the newest code.")
    codes = totp.new_recovery_codes()
    store.enable_totp(user["id"], step, [totp.recovery_hash(c) for c in codes])
    _audit_everywhere(store, user, "two_factor_enabled", request)
    return web.json_response({"recovery_codes": codes})


async def two_factor_disable(request: web.Request) -> web.Response:
    store = request.app[STORE]
    user = signed_in(request, restricted_ok=True)
    if store.setting("require_2fa", False):
        raise fail(409, "This console requires two-factor sign-in")
    await confirm_password(request, user, (await body(request)).get("password"))
    store.disable_totp(user["id"])
    _audit_everywhere(store, user, "two_factor_disabled", request)
    return web.json_response({})


async def recovery_codes(request: web.Request) -> web.Response:
    store = request.app[STORE]
    user = signed_in(request, restricted_ok=True)
    if not user["totp"]:
        raise fail(409, "Two-factor sign-in is off")
    await confirm_password(request, user, (await body(request)).get("password"))
    codes = totp.new_recovery_codes()
    store.replace_recovery(user["id"], [totp.recovery_hash(c) for c in codes])
    return web.json_response({"recovery_codes": codes})


def _audit_everywhere(store: Store, user: dict[str, Any], action: str, request: web.Request,
                      target: str | None = None) -> None:
    """A change to an account's security, in the log of every tenant the account belongs to."""
    for membership in store.memberships(user["id"]):
        store.tenant(membership["id"]).log(action, user=user["username"], target=target,
                                           address=client_address(request))


# ---- Console settings (administrators) -----------------------------------------------


async def settings(request: web.Request) -> web.Response:
    store = request.app[STORE]
    user = signed_in(request)
    if not user["admin"]:
        raise fail(403, "Only console administrators")
    if request.method == "POST":
        data = await body(request)
        require = data.get("require_2fa")
        if not isinstance(require, bool):
            raise fail(400, "require_2fa must be true or false")
        if require and not user["totp"]:
            raise fail(409, "Set up two-factor sign-in for yourself first")
        store.set_setting("require_2fa", require)
        _LOGGER.info("%s set require_2fa to %s", user["username"], require)
    return web.json_response({"require_2fa": bool(store.setting("require_2fa", False))})


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
    tenant.add_site(name_field(data, "site", "A first site") if data.get("site") else "Main site", _timezone(data))
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


def _timezone(data: dict[str, Any]) -> str:
    """The site's time zone (the browser sends its own); UTC when there's none."""
    zone = data.get("timezone") or "UTC"
    if not schedule.valid_zone(zone):
        raise fail(400, "Unknown time zone")
    return zone


async def add_site(request: web.Request) -> web.Response:
    user, tenant_id, _ = member(request, "own")
    tenant = request.app[STORE].tenant(tenant_id)
    data = await body(request)
    name = name_field(data, "name", "A name")
    site_id = tenant.add_site(name, _timezone(data))
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
        tenant.forget_target(f"site:{site_id}")
        tenant.log("site_removed", user=user["username"], target=f"site:{site_id}", address=client_address(request))
    else:
        data = await body(request)
        name = name_field(data, "name", "A name")
        zone = _timezone(data) if data.get("timezone") else None
        tenant.rename_site(site_id, name, zone)
        tenant.log("site_renamed", user=user["username"], target=f"site:{site_id}",
                   detail={"name": name, **({"timezone": zone} if zone else {})}, address=client_address(request))
        await request.app[SCREENS].refresh(tenant_id, [x["id"] for x in tenant.screens() if x["site_id"] == site_id])
    return web.json_response({"sites": tenant.sites()})


# ---- Members --------------------------------------------------------------------------


def _member_view(row: dict[str, Any], tenant_id: str) -> dict[str, Any]:
    return {
        "id": row["id"], "username": row["username"], "name": row["name"], "role": row["role"],
        "two_factor": bool(row["two_factor"]), "admin": bool(row["admin"]),
        # Only this tenant's own accounts: an owner can't take over an account that also
        # opens another tenant, or a console administrator's.
        "managed": not row["admin"] and row["tenants"] == 1,
    }


async def list_members(request: web.Request) -> web.Response:
    _, tenant_id, _ = member(request, "own")
    rows = request.app[STORE].members(tenant_id)
    return web.json_response({"members": [_member_view(r, tenant_id) for r in rows]})


def _role(data: dict[str, Any]) -> str:
    role = data.get("role")
    if role not in ROLES:
        raise fail(400, "Choose a role: owner, operator or viewer")
    return role


def _one_time_password() -> str:
    return secrets.token_urlsafe(12)


async def add_member(request: web.Request) -> web.Response:
    """A new account for this tenant, with a one-time password to pass on. Console
    administrators can also add an existing account by its user name."""
    user, tenant_id, _ = member(request, "own")
    store = request.app[STORE]
    data = await body(request)
    username = name_field(data, "username", "A user name")
    role = _role(data)
    existing = store.user_by_name(username)
    password = None
    if existing:
        if not user["admin"]:
            raise fail(409, "That user name is taken. Choose another.")
        if store.role(existing["id"], tenant_id):
            raise fail(409, "Already a member")
        user_id = existing["id"]
    else:
        name = name_field(data, "name", "A name")
        password = _one_time_password()
        user_id = store.add_user(username, name, await asyncio.to_thread(hash_password, password), must_change=True)
    store.set_member(user_id, tenant_id, role)
    store.tenant(tenant_id).log("member_added", user=user["username"], target=f"user:{username}",
                                detail={"role": role}, address=client_address(request))
    return web.json_response({"id": user_id, "password": password})


def _target_member(request: web.Request, tenant_id: str) -> dict[str, Any]:
    raw = request.match_info["user"]
    rows = {r["id"]: r for r in request.app[STORE].members(tenant_id)}
    target = rows.get(int(raw)) if raw.isdigit() else None
    if not target:
        raise fail(404, "No such member")
    return target


def _keep_an_owner(store: Store, tenant_id: str, target: dict[str, Any]) -> None:
    owners = [m for m in store.members(tenant_id) if m["role"] == "owner"]
    if target["role"] == "owner" and len(owners) == 1:
        raise fail(409, "An organisation needs at least one owner")


async def change_member(request: web.Request) -> web.Response:
    user, tenant_id, _ = member(request, "own")
    store = request.app[STORE]
    target = _target_member(request, tenant_id)
    role = _role(await body(request))
    if role != "owner":
        _keep_an_owner(store, tenant_id, target)
    store.set_member(target["id"], tenant_id, role)
    store.tenant(tenant_id).log("member_changed", user=user["username"], target=f"user:{target['username']}",
                                detail={"role": role}, address=client_address(request))
    return web.json_response({})


async def remove_member(request: web.Request) -> web.Response:
    user, tenant_id, _ = member(request, "own")
    store = request.app[STORE]
    target = _target_member(request, tenant_id)
    _keep_an_owner(store, tenant_id, target)
    store.remove_member(target["id"], tenant_id)
    if not target["admin"] and not store.memberships(target["id"]):
        store.delete_user(target["id"])  # an account with nothing left to open goes
    store.tenant(tenant_id).log("member_removed", user=user["username"], target=f"user:{target['username']}",
                                address=client_address(request))
    return web.json_response({})


async def reset_member(request: web.Request) -> web.Response:
    """A new one-time password, or two-factor sign-in turned off, for a member who lost theirs."""
    user, tenant_id, _ = member(request, "own")
    store = request.app[STORE]
    target = _target_member(request, tenant_id)
    if not (user["admin"] or _member_view(target, tenant_id)["managed"]):
        raise fail(403, "This account also opens other organisations; a console administrator can reset it")
    what = (await body(request)).get("reset")
    tenant = store.tenant(tenant_id)
    if what == "password":
        password = _one_time_password()
        store.set_password(target["id"], await asyncio.to_thread(hash_password, password), must_change=True)
        tenant.log("member_password_reset", user=user["username"], target=f"user:{target['username']}",
                   address=client_address(request))
        return web.json_response({"password": password})
    if what == "two_factor":
        store.disable_totp(target["id"])
        tenant.log("member_two_factor_reset", user=user["username"], target=f"user:{target['username']}",
                   address=client_address(request))
        return web.json_response({})
    raise fail(400, "reset must be password or two_factor")


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
        site_id = data["site_id"]
        if not isinstance(site_id, int) or isinstance(site_id, bool) or not tenant.site(site_id):
            raise fail(400, "No such site")
        changes["site_id"] = data["site_id"]
    config = dict(screen["config"])
    home_page: int | None | bool = False
    if "home_page" in data:
        home_page = data["home_page"]
        if home_page is not None and (not isinstance(home_page, int) or isinstance(home_page, bool)
                                      or not tenant.page(home_page)):
            raise fail(400, "No such page")
        if home_page is not None:
            config.pop("home_dashboard", None)
    if "home" in data and home_page in (False, None):
        home = data["home"]
        if home in (None, ""):
            config["home_dashboard"] = ""  # the screen goes back to its setup screen
        else:
            try:
                config["home_dashboard"] = web_url(home, "The home page")
            except Invalid as err:
                raise fail(400, str(err)) from None
    if "zoom" in data:
        zoom = data["zoom"]
        if zoom in (None, ""):
            config["zoom"] = 100
        elif isinstance(zoom, int) and not isinstance(zoom, bool) and 25 <= zoom <= 400:
            config["zoom"] = zoom
        else:
            raise fail(400, "Zoom is a percentage from 25 to 400")
    if "renderer_id" in data:
        renderer_id = data["renderer_id"]
        if renderer_id is not None and (not isinstance(renderer_id, int) or not tenant.renderer(renderer_id)):
            raise fail(400, "No such renderer")
        if renderer_id != screen["renderer_id"]:
            tenant.set_screen_renderer(screen["id"], renderer_id)
            changes["renderer_id"] = renderer_id
            await request.app[SCREENS].refresh(tenant_id, [screen["id"]])
    if config != screen["config"] or (home_page is not False and home_page != screen["home_page"]):
        try:
            await request.app[SCREENS].configure(tenant_id, screen["id"], config, home_page)
        except Invalid as err:
            raise fail(409, str(err)) from None
        changes["config"] = {**config, **({"home_page": home_page} if home_page is not False else {})}
    if not changes:
        raise fail(400, "Nothing to change")
    if set(changes) - {"config", "renderer_id"}:
        tenant.update_screen(screen["id"], **{k: v for k, v in changes.items() if k not in ("config", "renderer_id")})
    tenant.log("screen_changed", user=user["username"], target=f"screen:{screen['id']}", detail=changes,
               address=client_address(request))
    request.app[SCREENS].publish(tenant_id, screen["id"])
    return web.json_response({})


async def remove_screen(request: web.Request) -> web.Response:
    user, tenant_id, _ = member(request, "operate")
    screen = _screen(request, tenant_id)
    await request.app[SCREENS].unpair(tenant_id, screen["id"])
    request.app[STORE].tenant(tenant_id).forget_target(f"screen:{screen['id']}")
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
                # The stream ends with the session or the membership, like any other request.
                try:
                    member(request, "view")
                except web.HTTPException:
                    break
                await response.write(b": ping\n\n")
                continue
            await response.write(f"data: {json.dumps(event)}\n\n".encode())
    except ConnectionResetError:
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


# ---- Groups ---------------------------------------------------------------------------


def _number_id(request: web.Request, key: str) -> int:
    raw = request.match_info[key]
    if not raw.isdigit():
        raise fail(404, "Not found")
    return int(raw)


async def list_groups(request: web.Request) -> web.Response:
    _, tenant_id, _ = member(request, "view")
    return web.json_response({"groups": request.app[STORE].tenant(tenant_id).groups()})


async def save_group(request: web.Request) -> web.Response:
    user, tenant_id, _ = member(request, "operate")
    tenant = request.app[STORE].tenant(tenant_id)
    group_id = _number_id(request, "group") if "group" in request.match_info else None
    if group_id is not None and not tenant.group(group_id):
        raise fail(404, "No such group")
    data = await body(request)
    name = name_field(data, "name", "A name")
    screen_ids = data.get("screens", [])
    known = {s["id"] for s in tenant.screens()}
    if not isinstance(screen_ids, list) or not all(isinstance(i, int) and i in known for i in screen_ids):
        raise fail(400, "Choose screens of this organisation")
    group_id = tenant.save_group(group_id, name, screen_ids)
    tenant.log("group_saved", user=user["username"], target=f"group:{group_id}",
               detail={"name": name, "screens": len(screen_ids)}, address=client_address(request))
    return web.json_response({"id": group_id, "groups": tenant.groups()})


async def delete_group(request: web.Request) -> web.Response:
    user, tenant_id, _ = member(request, "operate")
    tenant = request.app[STORE].tenant(tenant_id)
    group_id = _number_id(request, "group")
    group = tenant.group(group_id)
    if not group:
        raise fail(404, "No such group")
    tenant.delete_group(group_id)
    tenant.forget_target(f"group:{group_id}")
    tenant.log("group_removed", user=user["username"], target=f"group:{group_id}", detail={"name": group["name"]},
               address=client_address(request))
    return web.json_response({"groups": tenant.groups()})


async def group_action(request: web.Request) -> web.Response:
    """A command, or a home page, for every screen in the group at once."""
    user, tenant_id, _ = member(request, "operate")
    tenant = request.app[STORE].tenant(tenant_id)
    screens = request.app[SCREENS]
    group = tenant.group(_number_id(request, "group"))
    if not group:
        raise fail(404, "No such group")
    data = await body(request)
    failed: list[str] = []
    names = {s["id"]: s["name"] for s in tenant.screens()}
    if "home_page" in data:
        page_id = data["home_page"]
        if page_id is not None and (not isinstance(page_id, int) or not tenant.page(page_id)):
            raise fail(400, "No such page")
        for screen_id in group["screens"]:
            screen = tenant.screen(screen_id)
            config = {k: v for k, v in screen["config"].items() if page_id is None or k != "home_dashboard"}
            try:
                await screens.configure(tenant_id, screen_id, config, page_id)
            except (Invalid, PleatViewError):
                failed.append(names[screen_id])
        detail: dict[str, Any] = {"home_page": page_id}
    else:
        command = data.get("command")
        try:
            params = command_params(command if isinstance(command, str) else "", data)
        except Invalid as err:
            raise fail(400, str(err)) from None

        async def send(screen_id: int) -> None:
            try:
                await screens.command(tenant_id, screen_id, command, params)
            except (Invalid, PleatViewError):
                failed.append(names[screen_id])
        await asyncio.gather(*(send(i) for i in group["screens"]))
        detail = {"command": command, **{k: v for k, v in params.items() if v is not None}}
    tenant.log("group_action", user=user["username"], target=f"group:{group['id']}",
               detail={**detail, "failed": len(failed)}, address=client_address(request))
    return web.json_response({"done": len(group["screens"]) - len(failed), "failed": failed})


# ---- Pages ----------------------------------------------------------------------------


def _page_view(request: web.Request, tenant_id: str, page: dict[str, Any], used: dict[int, int]) -> dict[str, Any]:
    return {
        "id": page["id"], "name": page["name"], "kind": page["kind"], "url": page["url"],
        "items": page["items"], "address": request.app[SCREENS].page_url(tenant_id, page),
        "screens": used.get(page["id"], 0),
    }


async def list_pages(request: web.Request) -> web.Response:
    _, tenant_id, _ = member(request, "view")
    tenant = request.app[STORE].tenant(tenant_id)
    used: dict[int, int] = {}
    for screen in tenant.screens():
        if screen["home_page"]:
            used[screen["home_page"]] = used.get(screen["home_page"], 0) + 1
    return web.json_response({"pages": [_page_view(request, tenant_id, p, used) for p in tenant.pages()]})


async def save_page(request: web.Request) -> web.Response:
    user, tenant_id, _ = member(request, "operate")
    tenant = request.app[STORE].tenant(tenant_id)
    page_id = _number_id(request, "page") if "page" in request.match_info else None
    if page_id is not None and not tenant.page(page_id):
        raise fail(404, "No such page")
    data = await body(request)
    name = name_field(data, "name", "A name")
    kind = data.get("kind")
    url, items = None, []
    if kind == "web":
        try:
            url = web_url(data.get("url"), "The address")
        except Invalid as err:
            raise fail(400, str(err)) from None
    elif kind == "playlist":
        raw = data.get("items")
        if not isinstance(raw, list) or len(raw) > 500:
            raise fail(400, "A playlist is a list of up to 500 media")
        for entry in raw:
            media_id = entry.get("media_id") if isinstance(entry, dict) else None
            seconds = entry.get("seconds") if isinstance(entry, dict) else None
            if not isinstance(media_id, int) or not tenant.media_item(media_id):
                raise fail(400, "Choose media from the library")
            if seconds is not None and (not isinstance(seconds, int) or not 1 <= seconds <= 86400):
                raise fail(400, "Seconds on screen go from 1 to 86400")
            items.append({"media_id": media_id, **({"seconds": seconds} if seconds else {})})
    else:
        raise fail(400, "A page is a web page or a playlist")
    page_id = tenant.save_page(page_id, name=name, kind=kind, url=url, items=items)
    tenant.log("page_saved", user=user["username"], target=f"page:{page_id}", detail={"name": name, "kind": kind},
               address=client_address(request))
    await request.app[SCREENS].refresh(tenant_id, tenant.screens_with_page(page_id))
    return web.json_response({"id": page_id})


async def delete_page(request: web.Request) -> web.Response:
    user, tenant_id, _ = member(request, "operate")
    tenant = request.app[STORE].tenant(tenant_id)
    page_id = _number_id(request, "page")
    page = tenant.page(page_id)
    if not page:
        raise fail(404, "No such page")
    affected = tenant.screens_with_page(page_id)
    for screen_id in affected:  # they keep showing it, now as their own home page
        screen = tenant.screen(screen_id)
        tenant.update_screen(screen_id, config={**screen["config"],
                                                "home_dashboard": request.app[SCREENS].page_url(tenant_id, page)
                                                if page["kind"] == "web" else ""})
    tenant.delete_page(page_id)
    tenant.log("page_removed", user=user["username"], target=f"page:{page_id}", detail={"name": page["name"]},
               address=client_address(request))
    await request.app[SCREENS].refresh(tenant_id, affected)
    return web.json_response({})


# ---- Media library -------------------------------------------------------------------


def _media_view(request: web.Request, tenant_id: str, item: dict[str, Any]) -> dict[str, Any]:
    return {
        **{k: item[k] for k in ("id", "name", "size", "status", "progress", "kind", "duration", "summary", "error")},
        "address": request.app[SCREENS].media_url(tenant_id, item),
        "thumbnail": item["status"] == "ready" and item["kind"] in ("video", "image"),
    }


async def list_media(request: web.Request) -> web.Response:
    _, tenant_id, _ = member(request, "view")
    tenant = request.app[STORE].tenant(tenant_id)
    return web.json_response({"media": [_media_view(request, tenant_id, m) for m in tenant.media()]})


async def upload_media(request: web.Request) -> web.Response:
    """The file itself is the request's body, streamed to disk, never held in memory."""
    user, tenant_id, _ = member(request, "operate")
    store, library = request.app[STORE], request.app[LIBRARY]
    tenant = store.tenant(tenant_id)
    name = request.query.get("name", "").strip()[:200] or "Untitled"
    limit = request.app[MEDIA_MAX]
    if request.content_length is not None and request.content_length > limit:
        raise fail(413, f"Files up to {limit // (1024 * 1024)} MB")
    media_id, _ = tenant.add_media(name, request.content_length or 0)
    where = folder(store.data, tenant_id, media_id)
    where.mkdir(parents=True, exist_ok=True)
    size = 0
    try:
        with (where / "original").open("wb") as out:
            async for chunk in request.content.iter_chunked(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    raise fail(413, f"Files up to {limit // (1024 * 1024)} MB")
                await asyncio.to_thread(out.write, chunk)
    except BaseException:
        library.remove(tenant_id, media_id)
        raise
    if size == 0:
        library.remove(tenant_id, media_id)
        raise fail(400, "The file is empty")
    tenant.update_media(media_id, size=size)
    tenant.log("media_added", user=user["username"], target=f"media:{media_id}", detail={"name": name, "size": size},
               address=client_address(request))
    library.add(tenant_id, media_id)
    return web.json_response({"id": media_id})


def _media(request: web.Request, tenant_id: str) -> dict[str, Any]:
    item = request.app[STORE].tenant(tenant_id).media_item(_number_id(request, "media"))
    if not item:
        raise fail(404, "No such media")
    return item


async def change_media(request: web.Request) -> web.Response:
    user, tenant_id, _ = member(request, "operate")
    tenant = request.app[STORE].tenant(tenant_id)
    item = _media(request, tenant_id)
    if request.method == "DELETE":
        affected = []
        for page in tenant.pages():
            kept = [e for e in page["items"] if e.get("media_id") != item["id"]]
            if len(kept) != len(page["items"]):
                tenant.save_page(page["id"], name=page["name"], kind=page["kind"], url=page["url"], items=kept)
                affected += tenant.screens_with_page(page["id"])
        request.app[LIBRARY].remove(tenant_id, item["id"])
        tenant.log("media_removed", user=user["username"], target=f"media:{item['id']}", detail={"name": item["name"]},
                   address=client_address(request))
        await request.app[SCREENS].refresh(tenant_id, affected)
        return web.json_response({})
    name = name_field(await body(request), "name", "A name")
    tenant.update_media(item["id"], name=name)
    return web.json_response({})


async def media_thumbnail(request: web.Request) -> web.StreamResponse:
    _, tenant_id, _ = member(request, "view")
    item = _media(request, tenant_id)
    path = folder(request.app[STORE].data, tenant_id, item["id"]) / "thumb.jpg"
    if not path.is_file():
        raise fail(404, "No picture")
    return web.FileResponse(path, headers={"Content-Type": "image/jpeg", "Cache-Control": "private, max-age=600"})


# ---- Renderers -------------------------------------------------------------------------


async def list_renderers(request: web.Request) -> web.Response:
    _, tenant_id, _ = member(request, "view")
    screens = request.app[SCREENS]
    tenant = request.app[STORE].tenant(tenant_id)
    out = []
    for row in tenant.renderers():
        target = rendering.Target(f"{row['scheme']}://{row['host']}:{row['port']}",
                                  request.app[STORE].sealer(tenant_id).open(row["token"], f"renderer:{row['device_id']}"),
                                  row["fingerprint"])
        try:
            live = await rendering.info(screens.session, target.base_url, target.fingerprint, target.token)
        except Exception:  # noqa: BLE001 - one that doesn't answer is shown as such
            live = None
        out.append({"id": row["id"], "name": row["name"], "address": target.base_url,
                    "online": live is not None, "screens": len((live or {}).get("sessions") or {}),
                    "max_screens": (live or {}).get("max_screens"), "load": (live or {}).get("load")})
    return web.json_response({"renderers": out, "discovered": screens.discovered_renderers(tenant_id)})


async def pair_renderer(request: web.Request) -> web.Response:
    user, tenant_id, _ = member(request, "operate")
    store, screens = request.app[STORE], request.app[SCREENS]
    tenant = store.tenant(tenant_id)
    data = await body(request)
    name = name_field(data, "name", "A name")
    pin = data.get("pin")
    if not (isinstance(pin, str) and pin.strip().isdigit() and len(pin.strip()) == 6):
        raise fail(400, "The PIN is the 6 digits in the renderer's log")
    if device_id := data.get("device_id"):
        found = screens.browser.found(RENDERER_SERVICE).get(device_id) if screens.browser else None
        if not found:
            raise fail(404, "That renderer isn't on the network any more")
        host, port, scheme = found["host"], found["port"], found.get("scheme") or "http"
    else:
        try:
            host, port = check_address(data.get("host", ""), data.get("port", 8780))
        except Invalid as err:
            raise fail(400, str(err)) from None
        scheme = "https" if data.get("https") else "http"
    url = f"{scheme}://{host}:{port}"
    try:
        fingerprint = await rendering.fetch_certificate(screens.session, url) if scheme == "https" else None
        live = await rendering.info(screens.session, url, fingerprint)
        if tenant.renderer_by_device(str(live.get("id"))):
            raise fail(409, "That renderer is already paired")
        token = await rendering.pair(screens.session, url, pin.strip(), fingerprint)
    except ValueError as err:
        raise fail(400, str(err)) from None
    except (aiohttp.ClientError, TimeoutError):
        raise fail(502, "The renderer can't be reached at that address") from None
    device = str(live.get("id") or url)
    renderer_id = tenant.add_renderer(name=name, device_id=device, host=host, port=port, scheme=scheme,
                                      token=store.sealer(tenant_id).seal(token, f"renderer:{device}"),
                                      fingerprint=fingerprint)
    tenant.log("renderer_paired", user=user["username"], target=f"renderer:{renderer_id}",
               detail={"name": name, "address": url}, address=client_address(request))
    return web.json_response({"id": renderer_id})


async def remove_renderer(request: web.Request) -> web.Response:
    user, tenant_id, _ = member(request, "operate")
    store, screens = request.app[STORE], request.app[SCREENS]
    tenant = store.tenant(tenant_id)
    row = tenant.renderer(_number_id(request, "renderer"))
    if not row:
        raise fail(404, "No such renderer")
    affected = [s["id"] for s in tenant.screens() if s["renderer_id"] == row["id"]]
    for screen_id in affected:
        await screens.rendering.stop(tenant_id, screen_id)
    token = store.sealer(tenant_id).open(row["token"], f"renderer:{row['device_id']}")
    target = rendering.Target(f"{row['scheme']}://{row['host']}:{row['port']}", token, row["fingerprint"])
    try:
        async with screens.session.post(f"{target.base_url}/api/unpair", json={}, timeout=rendering.TIMEOUT,
                                        **target.kwargs()):
            pass
    except (aiohttp.ClientError, TimeoutError):
        pass
    tenant.delete_renderer(row["id"])
    tenant.log("renderer_removed", user=user["username"], target=f"renderer:{row['id']}",
               detail={"name": row["name"]}, address=client_address(request))
    await screens.refresh(tenant_id, affected)
    return web.json_response({})


# ---- Schedules ------------------------------------------------------------------------


async def list_schedules(request: web.Request) -> web.Response:
    _, tenant_id, _ = member(request, "view")
    return web.json_response({"schedules": request.app[STORE].tenant(tenant_id).schedules()})


async def save_schedule(request: web.Request) -> web.Response:
    user, tenant_id, _ = member(request, "operate")
    tenant = request.app[STORE].tenant(tenant_id)
    schedule_id = _number_id(request, "schedule") if "schedule" in request.match_info else None
    if schedule_id is not None and not tenant.schedule(schedule_id):
        raise fail(404, "No such schedule")
    data = await body(request)
    name = name_field(data, "name", "A name")
    target = data.get("target")
    kind, _, raw_id = target.partition(":") if isinstance(target, str) else ("", "", "")
    exists = {"screen": tenant.screen, "group": tenant.group, "site": tenant.site}.get(kind)
    if not exists or not raw_id.isdigit() or not exists(int(raw_id)):
        raise fail(400, "Choose a screen, a group or a site")
    page_id = data.get("page_id")
    if not isinstance(page_id, int) or isinstance(page_id, bool) or not tenant.page(page_id):
        raise fail(400, "Choose a page")
    days = data.get("days")
    if not isinstance(days, str) or not days or not set(days) <= set("1234567"):
        raise fail(400, "Choose at least one day")
    start, end = data.get("start"), data.get("end")
    if not all(isinstance(t, str) and schedule.TIME.match(t) for t in (start, end)):
        raise fail(400, "Times look like 08:00")
    schedule_id = tenant.save_schedule(schedule_id, name=name, target=target, page_id=page_id,
                                       days="".join(sorted(set(days))), start=start, end=end)
    tenant.log("schedule_saved", user=user["username"], target=f"schedule:{schedule_id}",
               detail={"name": name, "on": target, "days": days, "from": start, "to": end},
               address=client_address(request))
    await request.app[SCREENS].refresh(tenant_id)
    return web.json_response({"id": schedule_id})


async def delete_schedule(request: web.Request) -> web.Response:
    user, tenant_id, _ = member(request, "operate")
    tenant = request.app[STORE].tenant(tenant_id)
    entry = tenant.schedule(_number_id(request, "schedule"))
    if not entry:
        raise fail(404, "No such schedule")
    tenant.delete_schedule(entry["id"])
    tenant.log("schedule_removed", user=user["username"], target=f"schedule:{entry['id']}",
               detail={"name": entry["name"]}, address=client_address(request))
    await request.app[SCREENS].refresh(tenant_id)
    return web.json_response({})


# ---- Organisations ---------------------------------------------------------------------


async def rename_tenant(request: web.Request) -> web.Response:
    user, tenant_id, _ = member(request, "own")
    store = request.app[STORE]
    name = name_field(await body(request), "name", "A name")
    store.rename_tenant(tenant_id, name)
    store.tenant(tenant_id).log("tenant_renamed", user=user["username"], detail={"name": name},
                                address=client_address(request))
    return web.json_response(me(store, user))


async def export_tenant(request: web.Request) -> web.Response:
    """Everything the organisation set up, as JSON, without secrets (no tokens, no passwords).
    Media files stay on the console; the export lists them."""
    user, tenant_id, _ = member(request, "own")
    store = request.app[STORE]
    tenant = store.tenant(tenant_id)
    data = {
        "pleatview_console_export": 1,
        "version": VERSION,
        "organisation": {"id": tenant_id, "name": store.tenant_name(tenant_id)},
        "members": [{k: m[k] for k in ("username", "name", "role")} for m in store.members(tenant_id)],
        "sites": tenant.sites(),
        "screens": [{k: s[k] for k in ("id", "site_id", "device_id", "name", "host", "port", "info", "config", "home_page")}
                    for s in tenant.screens()],
        "groups": tenant.groups(),
        "pages": [{k: p[k] for k in ("id", "name", "kind", "url", "items")} for p in tenant.pages()],
        "media": [{k: m[k] for k in ("id", "name", "size", "kind", "duration", "summary", "status")} for m in tenant.media()],
        "schedules": tenant.schedules(),
        "activity": tenant.audit(limit=100000),
    }
    tenant.log("tenant_exported", user=user["username"], address=client_address(request))
    return web.json_response(data, headers={
        "Content-Disposition": f'attachment; filename="pleatview-console-{tenant_id}.json"'})


async def all_tenants(request: web.Request) -> web.Response:
    """For console administrators: every organisation's name and size, not its content."""
    store = request.app[STORE]
    user = signed_in(request)
    if not user["admin"]:
        raise fail(403, "Only console administrators")
    out = []
    for t in store.tenants():
        out.append({"id": t["id"], "name": t["name"], "members": len(store.members(t["id"])),
                    "screens": len(store.tenant(t["id"]).screens())})
    return web.json_response({"tenants": out})


async def delete_tenant(request: web.Request) -> web.Response:
    """Unpairs the organisation's screens, then deletes it with its key, data and media."""
    store, screens = request.app[STORE], request.app[SCREENS]
    user = signed_in(request)
    if not user["admin"]:
        raise fail(403, "Only console administrators")
    tenant_id = request.match_info["tenant"]
    name = store.tenant_name(tenant_id) if TENANT_ID.match(tenant_id) else None
    if name is None:
        raise fail(404, "No such organisation")
    if (await body(request)).get("confirm") != name:
        raise fail(400, "Type the organisation's name to confirm")
    for screen in store.tenant(tenant_id).screens():
        await screens.unpair(tenant_id, screen["id"])
    members = [m["id"] for m in store.members(tenant_id)]
    store.delete_tenant(tenant_id)
    for member_id in members:  # accounts left with nothing to open go too
        account = store.user(member_id)
        if account and not account["admin"] and not store.memberships(member_id):
            store.delete_user(member_id)
    _LOGGER.warning("%s deleted organisation %s (%s)", user["username"], name, tenant_id)
    return web.json_response(me(store, user))


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


def create_app(store: Store, screens: Screens, *, https: bool, library: Library | None = None,
               media_max: int = 4096 * 1024 * 1024) -> web.Application:
    app = web.Application(middlewares=[security], client_max_size=64 * 1024)
    app[STORE] = store
    app[SCREENS] = screens
    app[LIBRARY] = library or Library(store)
    app[MEDIA_MAX] = media_max
    app[THROTTLE] = Throttle()
    app[HTTPS] = https
    app.router.add_get("/api/status", status)
    app.router.add_post("/api/setup", setup)
    app.router.add_post("/api/login", login)
    app.router.add_post("/api/logout", logout)
    app.router.add_post("/api/password", change_password)
    app.router.add_post("/api/tenants", add_tenant)
    app.router.add_get("/api/packages", packages)
    app.router.add_get("/api/account", account)
    app.router.add_post("/api/2fa/setup", two_factor_setup)
    app.router.add_post("/api/2fa/enable", two_factor_enable)
    app.router.add_post("/api/2fa/disable", two_factor_disable)
    app.router.add_post("/api/2fa/recovery", recovery_codes)
    app.router.add_get("/api/settings", settings)
    app.router.add_post("/api/settings", settings)
    t = "/api/t/{tenant}"
    app.router.add_get(t, tenant_overview)
    app.router.add_patch(t, rename_tenant)
    app.router.add_get(t + "/export", export_tenant)
    app.router.add_get(t + "/renderers", list_renderers)
    app.router.add_post(t + "/renderers", pair_renderer)
    app.router.add_delete(t + "/renderers/{renderer}", remove_renderer)
    app.router.add_get(t + "/schedules", list_schedules)
    app.router.add_post(t + "/schedules", save_schedule)
    app.router.add_patch(t + "/schedules/{schedule}", save_schedule)
    app.router.add_delete(t + "/schedules/{schedule}", delete_schedule)
    app.router.add_get("/api/tenants", all_tenants)
    app.router.add_delete("/api/tenants/{tenant}", delete_tenant)
    app.router.add_post(t + "/sites", add_site)
    app.router.add_patch(t + "/sites/{site:\\d+}", change_site)
    app.router.add_delete(t + "/sites/{site:\\d+}", change_site)
    app.router.add_get(t + "/audit", audit)
    app.router.add_get(t + "/members", list_members)
    app.router.add_post(t + "/members", add_member)
    app.router.add_patch(t + "/members/{user}", change_member)
    app.router.add_delete(t + "/members/{user}", remove_member)
    app.router.add_post(t + "/members/{user}/reset", reset_member)
    app.router.add_get(t + "/screens", list_screens)
    app.router.add_post(t + "/screens", pair_screen)
    app.router.add_patch(t + "/screens/{screen}", change_screen)
    app.router.add_delete(t + "/screens/{screen}", remove_screen)
    app.router.add_post(t + "/screens/{screen}/command", screen_command)
    app.router.add_get(t + "/events", events)
    app.router.add_get(t + "/groups", list_groups)
    app.router.add_post(t + "/groups", save_group)
    app.router.add_patch(t + "/groups/{group}", save_group)
    app.router.add_delete(t + "/groups/{group}", delete_group)
    app.router.add_post(t + "/groups/{group}/action", group_action)
    app.router.add_get(t + "/pages", list_pages)
    app.router.add_post(t + "/pages", save_page)
    app.router.add_patch(t + "/pages/{page}", save_page)
    app.router.add_delete(t + "/pages/{page}", delete_page)
    app.router.add_get(t + "/media", list_media)
    app.router.add_post(t + "/media", upload_media)
    app.router.add_patch(t + "/media/{media}", change_media)
    app.router.add_delete(t + "/media/{media}", change_media)
    app.router.add_get(t + "/media/{media}/thumb.jpg", media_thumbnail)
    app.router.add_get("/", static)
    app.router.add_get("/{name}", static)
    return app
