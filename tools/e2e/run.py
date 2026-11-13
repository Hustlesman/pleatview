#!/usr/bin/env python3
"""End-to-end test: PleatView on an Android TV emulator or a real TV against a real Home Assistant.

The emulator runs in Docker and needs /dev/kvm. By default a throwaway Home Assistant
also runs in Docker, listening only on 127.0.0.1.

    tools/e2e/run.py                         # throwaway Home Assistant stable, fresh debug build
    tools/e2e/run.py --ha-version 2025.1.4   # any Home Assistant image tag (beta, dev, ...)
    tools/e2e/run.py --apk path/to/app.apk   # test a specific APK (release builds skip the DevTools check)
    tools/e2e/run.py --form-factor tablet    # a Pixel Tablet emulator (or phone: a Pixel 8) instead of a TV
    tools/e2e/run.py --keep                  # leave the containers running afterwards
    tools/e2e/run.py --only levels           # after pairing, only the checks whose name contains "levels"
    tools/e2e/run.py --screens clip.mp4      # then take the website and store pictures (out/<time>/screens),
                                             # with clip.mp4 in picture-in-picture

    # An existing Home Assistant on this machine (host networking), with the demo
    # integration and an admin long-lived token; the pairing is removed afterwards:
    tools/e2e/run.py --ha-url http://192.168.88.250:8123 --ha-token-file ~/.config/pleatview/ha-test-token

    # A real TV on the network instead of the emulator, with ADB debugging (Fire TV) or USB
    # debugging (Google TV) on. PleatView is reinstalled first, so the run starts at setup.
    # The first time, the TV asks to allow this computer:
    tools/e2e/run.py --device 192.168.88.68 --ha-url ... --ha-token-file ...

Screenshots, logs and a summary go to tools/e2e/out/<time>/. Exit status 0 means every
check passed. docs/test-plan.md lists what is covered.
"""

from __future__ import annotations

import argparse
import base64
import functools
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = "com.hustlesman.pleatview"
HA_PORT = 18123  # the throwaway Home Assistant
TV_PORT = 18765  # adb-forwarded to the app's control port 8765
MEDIA_PORT = 18080  # serves the test web page, and the video when using an existing Home Assistant
DEVTOOLS_PORT = 19222
HA_CONTAINER = "pleatview-e2e-ha"
TV_CONTAINER = "pleatview-e2e-tv"
TV_IMAGE = "pleatview-e2e-emulator"
# Emulator image, its Docker build arguments and the AVD to start, per --form-factor.
MOBILE = ("pleatview-e2e-emulator-mobile",
          ["--build-arg", "SYSTEM_IMAGE=system-images;android-36;google_apis;x86_64",
           "--build-arg", "DEVICES=phone:pixel_8 tablet:pixel_tablet"])
FORM_FACTORS = {"tv": (TV_IMAGE, [], "tv"), "phone": (*MOBILE, "phone"), "tablet": (*MOBILE, "tablet")}
# The Pixel Tablet's 2560×1600 is slow to draw in software (System UI stops responding):
# use a typical 10-inch tablet's 1920×1200 at the same 1280×800 dp.
# The phone is 360 dp wide, like most Samsung phones and narrower than the Pixel 8 it runs as.
SCREEN_SIZES = {"phone": ("1080x2340", "480"), "tablet": ("1920x1200", "240")}
EMULATOR_HOST = "10.0.2.2"  # how the emulator reaches this machine
SCREENS_TV_NAME = "Living Room TV"  # what the emulator calls itself in --screens runs
# The use-case pictures' cameras: stills from the --screens video, at these seconds.
SCREENS_CAMERAS = {"Front door": 32, "Driveway": 44, "Back fence": 60, "Gate": 100}


def camera_file(name: str) -> str:
    return f"cam-{name.lower().replace(' ', '-')}.jpg"
# With --device, adb runs in its own container, on a port the emulator's adb doesn't use.
# Its key stays on this machine, so the TV only asks to allow it once.
ADB_CONTAINER = "pleatview-e2e-adb"
ADB_SERVER_PORT = 5038
DONATE_URL = "https://pleatview.hustlesman.com/donate"
ADB_KEYS = Path("~/.config/pleatview/adb").expanduser()
# The emulator's console and adb ports, clear of 5554/5555: this machine's network may already
# have another emulator, or a phone emulator's adb port published by Docker, which the adb
# server would pick up as a second device.
EMULATOR_PORT = 5580
ADB_COMMAND = ["docker", "exec", TV_CONTAINER, "adb", "-s", f"emulator-{EMULATOR_PORT}"]  # main() points this at the TV for --device

HA_CONFIG = f"""\
homeassistant:
  name: PleatView E2E
  internal_url: "http://{EMULATOR_HOST}:{HA_PORT}"
frontend:
config:
api:
demo:
media_source:
http:
  server_host: 127.0.0.1
  server_port: {HA_PORT}
logger:
  default: warning
  logs:
    custom_components.pleatview: debug
"""

TEST_PAGE = """<!doctype html>
<html><head><title>PleatView E2E</title><meta name="viewport" content="width=device-width"></head>
<body style="background:#123;color:#fff;font:48px sans-serif;margin:64px">PleatView end-to-end web page</body></html>
"""

TEST_GIF = ("ffmpeg -loglevel error -y -f lavfi -i testsrc2=duration=2:size=320x180:rate=10 -loop 0")

TEST_VIDEO = ("ffmpeg -loglevel error -y -f lavfi -i testsrc2=duration=30:size=640x360:rate=25 "
              "-c:v libx264 -pix_fmt yuv420p -preset ultrafast")

# Evaluates JavaScript in the dashboard WebView over the Chrome DevTools protocol. Runs
# with the Python from the Home Assistant image, which has aiohttp.
CDP_SCRIPT = """
import asyncio, json, sys
import aiohttp

async def main(port, expression):
    async with aiohttp.ClientSession() as s:
        pages = await (await s.get(f"http://127.0.0.1:{port}/json")).json()
        page = next(p for p in pages if p["type"] == "page" and p["url"].startswith("http"))
        async with s.ws_connect(page["webSocketDebuggerUrl"], max_msg_size=0) as ws:
            await ws.send_json({"id": 1, "method": "Runtime.evaluate",
                                "params": {"expression": expression, "returnByValue": True}})
            async for msg in ws:
                data = json.loads(msg.data)
                if data.get("id") == 1:
                    print(json.dumps(data["result"].get("result", {}).get("value")))
                    return

# Gives up by itself (a frozen page never answers), so no container outlives its caller.
asyncio.run(asyncio.wait_for(main(int(sys.argv[1]), sys.stdin.read()), float(sys.argv[2])))
"""

# Sends one Home Assistant websocket command; reads the token and the command from stdin.
WS_SCRIPT = """
import asyncio, json, sys
import aiohttp

async def main(url):
    token, message = sys.stdin.read().split("\\n", 1)
    async with aiohttp.ClientSession() as s, s.ws_connect(url + "/api/websocket") as ws:
        await ws.receive_json()
        await ws.send_json({"type": "auth", "access_token": token})
        await ws.receive_json()
        await ws.send_json({"id": 1, **json.loads(message)})
        print(json.dumps(await ws.receive_json()))

asyncio.run(main(sys.argv[1]))
"""

KIOSK_PROBE = """
(() => {
  const main = document.querySelector("home-assistant").shadowRoot.querySelector("home-assistant-main");
  const resolver = main.shadowRoot.querySelector("partial-panel-resolver");
  const panel = [...resolver.children].find((el) => el.localName.startsWith("ha-panel-"));
  const root = panel && panel.shadowRoot && panel.shadowRoot.querySelector("hui-root");
  const header = root && root.shadowRoot.querySelector(".header");
  return {
    panelLeft: Math.round(resolver.getBoundingClientRect().left),
    header: header ? getComputedStyle(header).display : "none",
  };
})()
"""


def run(*cmd: str, check: bool = True, input: str | None = None, timeout: float | None = 600) -> str:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, input=input, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        if check:
            raise RuntimeError(f"`{' '.join(cmd[:4])} …` timed out after {timeout:.0f}s") from None
        return ""
    if check and result.returncode != 0:
        raise RuntimeError(f"`{' '.join(cmd[:4])} …` failed: {f'{result.stderr} {result.stdout}'.strip()[:300]}")
    return result.stdout


def adb(*args: str, check: bool = True, timeout: float = 120) -> str:
    return run(*ADB_COMMAND, *args, check=check, timeout=timeout)


def wait_for(description: str, probe, timeout: float = 60, interval: float = 2):
    """Poll probe() until it returns something truthy."""
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            value = probe()
            if value:
                return value
        except Exception as err:  # noqa: BLE001 - keep polling through transient errors
            last_error = err
        time.sleep(interval)
    raise TimeoutError(f"timed out after {timeout:.0f}s waiting for {description}"
                       + (f" (last error: {last_error})" if last_error else ""))


def http(method: str, url: str, body=None, token: str | None = None, form: bool = False):
    headers, data = {}, None
    if body is not None:
        if form:
            data = urllib.parse.urlencode(body).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            text = response.read().decode()
            return response.status, json.loads(text) if text else None
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode()


def http_text(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as err:
        return err.code, ""


class HomeAssistant:
    """Just enough of the REST API to onboard, add PleatView and call services."""

    def __init__(self, url: str, token: str = "") -> None:
        self.url = url.rstrip("/")
        self.access = token
        self.refresh = ""

    def onboard(self) -> None:
        client_id = self.url + "/"
        status, users = http("POST", f"{self.url}/api/onboarding/users", {
            "client_id": client_id, "name": "Owner", "username": "owner",
            "password": "pleatview-e2e-only", "language": "en"})
        if status != 200:
            raise RuntimeError(f"onboarding failed: {status} {users}")
        status, tokens = http("POST", f"{self.url}/auth/token", {
            "grant_type": "authorization_code", "code": users["auth_code"], "client_id": client_id}, form=True)
        self.access, self.refresh = tokens["access_token"], tokens["refresh_token"]
        for step, body in (("core_config", {}), ("analytics", {}),
                           ("integration", {"client_id": client_id, "redirect_uri": client_id + "?auth_callback=1"})):
            http("POST", f"{self.url}/api/onboarding/{step}", body, self.access)

    def api(self, method: str, path: str, body=None):
        status, result = http(method, self.url + path, body, self.access)
        if status == 401 and self.refresh:
            _, tokens = http("POST", f"{self.url}/auth/token", {
                "grant_type": "refresh_token", "refresh_token": self.refresh, "client_id": self.url + "/"}, form=True)
            self.access = tokens["access_token"]
            status, result = http(method, self.url + path, body, self.access)
        return status, result

    def state(self, entity_id: str) -> dict:
        status, result = self.api("GET", f"/api/states/{entity_id}")
        if status != 200:
            raise RuntimeError(f"no state for {entity_id}: {status}")
        return result

    def call(self, domain: str, service: str, data: dict) -> None:
        status, result = self.api("POST", f"/api/services/{domain}/{service}", data)
        if status != 200:
            raise RuntimeError(f"{domain}.{service} failed: {status} {str(result)[:200]}")


class Report:
    def __init__(self, out: Path) -> None:
        self.out = out
        self.title = ""
        self.results: list[tuple[str, str, str]] = []

    def check(self, name: str, fn) -> None:
        try:
            detail = fn() or ""
            self.results.append(("PASS", name, detail))
        except SkipCheck as skip:
            self.results.append(("SKIP", name, str(skip)))
        except Exception as err:  # noqa: BLE001 - report and carry on with the next check
            self.results.append(("FAIL", name, str(err)))
            # What was on screen, for working out why.
            self.screenshot(f"failed-{sum(1 for s, _, _ in self.results if s == 'FAIL')}")
        status, _, detail = self.results[-1]
        print(f"  {status}  {name}" + (f"  ({detail})" if detail else ""), flush=True)

    def skip(self, name: str, reason: str) -> None:
        self.results.append(("SKIP", name, reason))
        print(f"  SKIP  {name}  ({reason})", flush=True)

    def screenshot(self, name: str) -> None:
        png = subprocess.run([*ADB_COMMAND, "exec-out", "screencap", "-p"], capture_output=True, check=False).stdout
        (self.out / f"{name}.png").write_bytes(png)

    def count(self, status: str) -> int:
        return sum(1 for s, _, _ in self.results if s == status)


class SkipCheck(Exception):
    """A check that doesn't apply to this run."""


class Run:
    """Everything one test run needs to know."""

    def __init__(self, args) -> None:
        self.args = args
        self.external = bool(args.ha_url)
        self.ha = HomeAssistant(args.ha_url or f"http://127.0.0.1:{HA_PORT}")
        if self.external:
            self.ha.access = Path(args.ha_token_file).expanduser().read_text().strip()
        # Home Assistant's image doubles as a toolbox: ffmpeg and Python with aiohttp.
        self.tools_image = f"ghcr.io/home-assistant/home-assistant:{'stable' if self.external else args.ha_version}"
        self.emulator_image, self.emulator_build_args, self.avd = FORM_FACTORS[args.form_factor]
        # A real TV is reached directly; the emulator through an adb forward.
        self.device = args.device
        tv_host = self.device.rsplit(":", 1)[0] if self.device else ""
        self.tv_address = (tv_host, 8765) if self.device else ("127.0.0.1", TV_PORT)
        self.tv_api = f"http://{self.tv_address[0]}:{self.tv_address[1]}"
        # The test video: Home Assistant's /local in a throwaway Home Assistant, otherwise a
        # server on this machine's loopback. A real TV reaches it through adb reverse, so no
        # firewall gets in the way.
        self.video_url = (f"http://127.0.0.1:{MEDIA_PORT}/e2e.mp4" if self.device
                          else f"http://{EMULATOR_HOST}:{MEDIA_PORT}/e2e.mp4" if self.external
                          else f"http://{EMULATOR_HOST}:{HA_PORT}/local/e2e.mp4")
        # A web page on another origin than Home Assistant, shown as a Webpage dashboard.
        self.site = f"http://{'127.0.0.1' if self.device else EMULATOR_HOST}:{MEDIA_PORT}"
        self.page_url = f"{self.site}/e2e.html"
        self.entry_id = ""
        self.media_server: ThreadingHTTPServer | None = None


# The test site's sign-in, for its pages behind a password prompt and a login form.
SITE_USER, SITE_PASSWORD = "pleatview", "e2e-only-password"
LOGIN_FORM = """<!doctype html><html><head><title>Sign in</title></head><body style="font:32px sans-serif;margin:64px">
<form method="post" action="/form/login"><p>Test site sign-in</p>
<input name="username" autocomplete="username"><br><input name="password" type="password"><br>
<button type="submit">Sign in</button></form></body></html>
"""


# A page for page steps (#pleatview=…): a cookie banner drawn late, a pop-up whose close
# button has no text (only CSS finds it), a map to drag and a long page to scroll. It tells
# the test site what happened, and only for real input (isTrusted), as a hand would make.
STEPS_PAGE = """<!doctype html><html><head><meta name="viewport" content="width=device-width"><title>Steps</title>
<style>body{font:28px sans-serif;margin:0;height:4000px;background:#123;color:#fff}
#map{position:fixed;left:10%;top:55%;width:80%;height:30%;background:#264;touch-action:none}
#consent{position:fixed;left:10%;right:10%;top:20%;background:#fff;color:#000;padding:24px;z-index:2}
#consent button{font:inherit;margin-right:16px}
#promo{position:fixed;right:4%;top:4%;background:#fc0;color:#000;padding:12px;z-index:3}
.close{display:inline-block;width:48px;height:48px;background:#000;margin-left:12px;vertical-align:middle;cursor:pointer}</style>
</head><body><p style="margin:24px">Page steps</p><div id="map">Map</div><script>
const tell = (what) => fetch('/steps/event?' + encodeURIComponent(what));
const note = (what) => fetch('/steps/note?' + encodeURIComponent(what));
setTimeout(() => {
  document.body.insertAdjacentHTML('beforeend',
    '<div id="consent"><p>This site uses cookies.</p><button>Accept all</button><button>Reject all</button></div>' +
    '<div id="promo">Sale!<span class="close" title=""></span></div>');
  for (const b of document.querySelectorAll('#consent button')) b.onclick = (e) => {
    if (e.isTrusted) tell('consent ' + b.textContent);
    document.getElementById('consent').remove();
  };
  document.querySelector('#promo .close').onclick = (e) => {
    if (e.isTrusted) tell('promo closed');
    document.getElementById('promo').remove();
  };
}, 1500);
let from = null;
const map = document.getElementById('map');
map.onpointerdown = (e) => { note(`down ${e.pointerType} ${e.button} ${e.isTrusted}`); if (e.isTrusted) from = e.clientX; };
map.onpointerup = (e) => { note(`up ${e.pointerType} ${e.button} ${Math.round(e.clientX - (from ?? e.clientX))}`); if (from !== null && e.clientX - from > 100) tell('map dragged'); from = null; };
let moves = 0;
map.onpointermove = (e) => { if (moves++ === 0) note(`first move ${e.pointerType} buttons ${e.buttons}`); };
let told = false;
addEventListener('wheel', (e) => { if (e.isTrusted && e.deltaY > 0 && !told) { told = true; tell('scrolled down'); } });
</script></body></html>
"""
# The steps for it: wait, a click on text, a click on CSS, a drag and a scroll.
STEPS = "wait:1;click:Reject%20all;click:css=%23promo%20.close;drag:30,70,70,70;scroll:50,40,600"


class QuietFiles(SimpleHTTPRequestHandler):
    """The test video and page, plus pages behind a password prompt and a login form."""

    def log_message(self, format, *args) -> None:  # noqa: A002 - signature from the base class
        pass

    def do_GET(self) -> None:  # noqa: N802 - name from the base class
        site: QuietServer = self.server
        if self.path.startswith("/prompt/"):
            expected = "Basic " + base64.b64encode(f"{SITE_USER}:{SITE_PASSWORD}".encode()).decode()
            if self.headers.get("Authorization") != expected:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="PleatView E2E"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            site.events.append("prompt ok")
            return self._page("<p>Signed in through a password prompt</p>")
        if self.path.startswith("/form/"):
            # A session works once, so the next visit has to sign in again.
            cookie = re.search(r"e2e_session=(\w+)", self.headers.get("Cookie", ""))
            if cookie and cookie.group(1) in site.sessions:
                site.sessions.discard(cookie.group(1))
                site.events.append("form ok")
                return self._page("<p>Signed in with a form</p>")
            return self._page(LOGIN_FORM, raw=True)
        if self.path.startswith("/steps/"):
            if self.path.startswith(("/steps/event?", "/steps/note?")):
                kind = "steps" if self.path.startswith("/steps/event?") else "note"
                site.events.append(f"{kind}: " + urllib.parse.unquote(self.path.split("?", 1)[1]))
                return self._page("")
            return self._page(STEPS_PAGE, raw=True)
        return super().do_GET()

    def do_POST(self) -> None:  # noqa: N802 - name from the base class
        site: QuietServer = self.server
        length = int(self.headers.get("Content-Length") or 0)
        fields = urllib.parse.parse_qs(self.rfile.read(length).decode())
        if self.path == "/form/login" and fields.get("username") == [SITE_USER] and fields.get("password") == [SITE_PASSWORD]:
            session = secrets.token_hex(8)
            site.sessions.add(session)
            site.events.append("form login")
            self.send_response(303)
            self.send_header("Location", "/form/")
            self.send_header("Set-Cookie", f"e2e_session={session}; Path=/form")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        site.events.append("form rejected")
        self._page(LOGIN_FORM, raw=True)

    def _page(self, body: str, raw: bool = False) -> None:
        html = body if raw else f'<!doctype html><html><body style="font:40px sans-serif;margin:64px">{body}</body></html>'
        data = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class QuietServer(ThreadingHTTPServer):
    def __init__(self, address, handler) -> None:
        super().__init__(address, handler)
        self.events: list[str] = []  # what the test site saw: "prompt ok", "form login", …
        self.sessions: set[str] = set()

    def handle_error(self, request, client_address) -> None:
        # The player hangs up mid-file when a video stops.
        if not isinstance(sys.exc_info()[1], ConnectionError):
            super().handle_error(request, client_address)


def text_center(text: str, texts: str) -> tuple[int, int] | None:
    """The middle of the view showing exactly this text, from a UI dump."""
    for node in texts.split("<node"):
        if f' text="{text}"' in node and ' bounds="' in node:
            left, top, right, bottom = map(int, re.findall(r"\d+", node.split(' bounds="', 1)[1].split('"', 1)[0]))
            return (left + right) // 2, (top + bottom) // 2
    return None


def screen_texts() -> str:
    adb("shell", "uiautomator", "dump", "/sdcard/ui.xml", check=False)
    return adb("shell", "cat", "/sdcard/ui.xml", check=False)


def frame(box: list[int], tools_image: str) -> bytes:
    """The screen's pixels inside [left, top, right, bottom], as PNG bytes of that area."""
    png = subprocess.run([*ADB_COMMAND, "exec-out", "screencap", "-p"], capture_output=True, check=False).stdout
    # Home Assistant's image has Pillow; crop there rather than adding a dependency here.
    return subprocess.run(["docker", "run", "--rm", "-i", "--entrypoint", "python3", tools_image, "-c",
                           "import io, sys; from PIL import Image; "
                           f"im = Image.open(io.BytesIO(sys.stdin.buffer.read())).crop(({box[0]}, {box[1]}, {box[2]}, {box[3]})); "
                           "out = io.BytesIO(); im.convert('RGB').save(out, 'PNG'); sys.stdout.buffer.write(out.getvalue())"],
                          input=png, capture_output=True, check=False).stdout


def brightest(box: list[int], tools_image: str) -> int:
    """The brightest colour channel among the screen's pixels inside [left, top, right, bottom]."""
    png = subprocess.run([*ADB_COMMAND, "exec-out", "screencap", "-p"], capture_output=True, check=False).stdout
    out = subprocess.run(["docker", "run", "--rm", "-i", "--entrypoint", "python3", tools_image, "-c",
                          "import io, sys; from PIL import Image; "
                          "im = Image.open(io.BytesIO(sys.stdin.buffer.read())).convert('RGB')"
                          f".crop(({box[0]}, {box[1]}, {box[2]}, {box[3]})); "
                          "print(max(max(p) for p in im.getdata()))"],
                         input=png, capture_output=True, check=False).stdout
    return int(out.decode().strip() or 255)


def colour_pixels(area: tuple[float, float, float, float], rgb: tuple[int, int, int], tools_image: str) -> int:
    """How many of the screen's pixels in an area (fractions of its width and height) are close to a colour."""
    png = subprocess.run([*ADB_COMMAND, "exec-out", "screencap", "-p"], capture_output=True, check=False).stdout
    out = subprocess.run(["docker", "run", "--rm", "-i", "--entrypoint", "python3", tools_image, "-c",
                          "import io, sys; from PIL import Image; "
                          "im = Image.open(io.BytesIO(sys.stdin.buffer.read())).convert('RGB'); w, h = im.size; "
                          f"a = {list(area)}; c = {list(rgb)}; "
                          "im = im.crop((int(a[0] * w), int(a[1] * h), int(a[2] * w), int(a[3] * h))); "
                          "print(sum(1 for p in im.getdata() if sum(abs(p[i] - c[i]) for i in range(3)) < 60))"],
                         input=png, capture_output=True, check=False).stdout
    return int(out.decode().strip() or 0)


def screen_attribute(view_id: str, attribute: str, texts: str | None = None) -> str:
    """An attribute of one of PleatView's views in the current UI dump."""
    for node in (texts if texts is not None else screen_texts()).split("<node"):
        if f'resource-id="{PACKAGE}:id/{view_id}"' in node and f' {attribute}="' in node:
            return node.split(f' {attribute}="', 1)[1].split('"', 1)[0]
    return ""


def view_bounds(view_id: str) -> tuple[int, int, int, int] | None:
    """Where one of PleatView's views is (left, top, right, bottom) while it's visible.

    From the activity's view dump, which is much faster than a UI dump; the bounds are
    relative to the view's parent, which for the views asked about fills the screen.
    """
    dump = adb("shell", "dumpsys", "activity", f"{PACKAGE}/.MainActivity", check=False)
    # Focusable views add their accessibility id: "… app:id/setup_demo aid=1073741824}".
    match = re.search(r"\{\w+ V\S* \S+ (-?\d+),(-?\d+)-(-?\d+),(-?\d+) #\w+ app:id/" + view_id + r"(?: [^}]*)?\}", dump)
    return tuple(int(n) for n in match.groups()) if match else None


def density() -> float:
    """Pixels per dp, after any override."""
    return int(re.findall(r"\d+", adb("shell", "wm", "density"))[-1]) / 160


def overlay_card() -> tuple[int, bool] | None:
    """PleatView's card over other apps: the width it asked for, and whether it's drawn."""
    for window in adb("shell", "dumpsys", "window", "windows", check=False).split("Window #")[1:]:
        if f" {PACKAGE}}}:" in window.split("\n", 1)[0]:
            width = re.search(r"mAttrs=\{\(-?\d+,-?\d+\)\((\d+)x", window)
            drawn = any(sign in window for sign in ("Surface: shown=true", "isReadyForDisplay()=true", "isOnScreen=true"))
            return (int(width.group(1)) if width else 0), drawn
    return None


def fits(width: int, screen: int) -> bool:
    """Whether a card this wide leaves its 24 dp margins on a screen this wide."""
    return width + 48 * density() <= screen + 1


def read_pin() -> str:
    return "".join(ch for ch in screen_attribute("setup_pin", "text") if ch.isdigit())


def prepare(r: Run) -> Path:
    print("Preparing images and the APK…", flush=True)
    run("docker", "build", "-q", "-t", "pleatview-android-build", str(ROOT / "tools/android-build"))
    dockerfile = (ROOT / "tools/e2e/emulator.Dockerfile").read_text()
    run("docker", "build", "-q", "-t", r.emulator_image, *r.emulator_build_args, "-", input=dockerfile, timeout=3600)
    run("docker", "pull", "-q", r.tools_image, timeout=3600)
    if r.args.apk:
        return Path(r.args.apk).resolve()
    run(str(ROOT / "tools/build-apk.sh"), "assembleDebug", timeout=3600)
    return ROOT / "android/app/build/outputs/apk/debug/app-debug.apk"


def connect_device(r: Run) -> None:
    ADB_KEYS.mkdir(parents=True, exist_ok=True)
    run("docker", "run", "-d", "--name", ADB_CONTAINER, "--network", "host", "--user", f"{os.getuid()}:{os.getgid()}",
        "-e", "HOME=/keys", "-e", f"ANDROID_ADB_SERVER_PORT={ADB_SERVER_PORT}", "-v", f"{ADB_KEYS}:/keys",
        r.emulator_image, "sleep", "infinity")
    print("  If the TV asks to allow USB debugging, tick \"Always allow\" and choose OK.", flush=True)

    def authorized():
        run("docker", "exec", ADB_CONTAINER, "adb", "connect", r.device, check=False, timeout=30)
        return adb("get-state", check=False, timeout=30).strip() == "device"

    wait_for(f"adb access to {r.device} (is ADB or USB debugging on?)", authorized, timeout=180, interval=5)


def start(r: Run, work: Path, apk: Path) -> None:
    tv = f"the TV at {r.device}" if r.device else "the TV emulator"
    print(f"Starting {tv}" + ("" if r.external else " and Home Assistant") + "…", flush=True)
    run("docker", "rm", "-f", TV_CONTAINER, ADB_CONTAINER, *([] if r.external else [HA_CONTAINER]), check=False)
    # The test video exists before Home Assistant starts: HA only serves /local when its www
    # folder exists at startup.
    media = work / "media"
    media.mkdir()
    run("docker", "run", "--rm", "--entrypoint", "sh", "-v", f"{media}:/out", r.tools_image, "-c",
        f"{TEST_VIDEO} /out/e2e.mp4 && {TEST_GIF} /out/e2e.gif && chmod a+r /out/e2e.mp4 /out/e2e.gif")
    (media / "e2e.html").write_text(TEST_PAGE)
    if r.args.screens:
        video = Path(r.args.screens).resolve()
        (media / "screens.mp4").symlink_to(video)
        stills = " && ".join(f"ffmpeg -loglevel error -y -ss {second} -i /video -frames:v 1 -vf scale=1280:-1 "
                             f"/out/{camera_file(name)}" for name, second in SCREENS_CAMERAS.items())
        run("docker", "run", "--rm", "--entrypoint", "sh", "-v", f"{media}:/out", "-v", f"{video}:/video:ro",
            r.tools_image, "-c", f"{stills} && chmod a+r /out/cam-*.jpg")
    handler = functools.partial(QuietFiles, directory=str(media))
    r.media_server = QuietServer(("127.0.0.1", MEDIA_PORT), handler)
    threading.Thread(target=r.media_server.serve_forever, daemon=True).start()
    if not r.external:
        (work / "ha").mkdir()
        (work / "ha/configuration.yaml").write_text(HA_CONFIG)
        run("docker", "run", "-d", "--name", HA_CONTAINER, "--network", "host", "-e", "TZ=UTC",
            "-v", f"{work / 'ha'}:/config", "-v", f"{media}:/config/www:ro",
            "-v", f"{ROOT / 'custom_components'}:/config/custom_components:ro", r.tools_image)
    if r.device:
        connect_device(r)
    else:
        # The host-side renderer is faster, but crashed the emulator (exit 139) in three of four
        # picture runs; drawing inside Android is slower and steady.
        gpu = os.environ.get("PLEATVIEW_E2E_GPU", "guest" if r.args.screens is not None else "swiftshader_indirect")
        run("docker", "run", "-d", "--name", TV_CONTAINER, "--network", "host", "--device", "/dev/kvm",
            "--cpus", "2", r.emulator_image, "emulator", "-avd", r.avd, "-port", str(EMULATOR_PORT),
            "-no-window", "-no-audio", "-no-boot-anim",
            "-gpu", gpu, "-no-snapshot", "-memory", "3072", "-cores", "2", "-no-metrics")
    if not r.external:
        # Home Assistant first: its HTTP port must be promoted within five minutes.
        wait_for("Home Assistant to start",
                 lambda: http("GET", f"{r.ha.url}/api/onboarding")[0] == 200, timeout=420)
        r.ha.onboard()
        # Home Assistant 2026.10+ keeps a changed HTTP port on trial and reverts it after
        # five minutes unless an admin promotes it. Older versions don't know the command.
        promoted = ws_call(r, {"type": "http/config/promote"}).get("success")
        print(f"  HTTP config promoted: {'yes' if promoted else 'not needed'}", flush=True)
    wait_for("the TV to boot",
             lambda: adb("shell", "getprop", "sys.boot_completed", check=False, timeout=30).strip() == "1",
             timeout=420)
    run("docker", "cp", str(apk), f"{ADB_CONTAINER if r.device else TV_CONTAINER}:/tmp/pleatview.apk")
    if r.device:
        # Start at the setup screen, whatever was installed before (a release build can't
        # be updated with a debug one anyway).
        adb("uninstall", PACKAGE, check=False)
    if r.args.form_factor != "tv":
        # Skip phones' and tablets' first-run hint for full-screen apps, which covers the
        # screen, and their animations. The phone image's Bluetooth sometimes crashes at
        # boot, and its "keeps stopping" dialog would hide PleatView: no Bluetooth, no
        # error dialogs, and close any that's already up.
        adb("shell", "settings", "put", "secure", "immersive_mode_confirmations", "confirmed")
        adb("shell", "settings", "put", "global", "hide_error_dialogs", "1")
        adb("shell", "cmd", "bluetooth_manager", "disable", check=False)
        adb("shell", "am", "broadcast", "-a", "android.intent.action.CLOSE_SYSTEM_DIALOGS", check=False)
        for scale in ("window_animation_scale", "transition_animation_scale", "animator_duration_scale"):
            adb("shell", "settings", "put", "global", scale, "0")
        if r.args.screen_size or r.args.form_factor in SCREEN_SIZES:
            size, density = r.args.screen_size or SCREEN_SIZES[r.args.form_factor]
            adb("shell", "wm", "size", size)
            adb("shell", "wm", "density", density)
    # The package manager can still be busy for a moment after boot_completed.
    wait_for("the APK to install", lambda: adb("install", "-r", "/tmp/pleatview.apk"), timeout=120, interval=5)
    if r.args.screens is not None:
        adb("shell", "settings", "put", "global", "device_name", f"'{SCREENS_TV_NAME}'")
    adb("shell", "am", "start", "-n", f"{PACKAGE}/.MainActivity")
    if r.device:
        adb("reverse", f"tcp:{MEDIA_PORT}", f"tcp:{MEDIA_PORT}")
    else:
        adb("forward", f"tcp:{TV_PORT}", "tcp:8765")
    wait_for("the TV's control API", lambda: http("GET", f"{r.tv_api}/api/info")[0] == 200)


def describe_tv() -> str:
    props = dict(re.findall(r"^\[([^\]]+)\]: \[([^\]]*)\]", adb("shell", "getprop", check=False), re.M))
    fire_os = props.get("ro.build.version.name", "")  # "Fire OS 7.6.6.9 (PS7669/4917)" on Fire TV
    return (f"{props.get('ro.product.manufacturer', '?')} {props.get('ro.product.model', '?')}, "
            f"Android {props.get('ro.build.version.release', '?')}"
            + (f", {fire_os.split(' (')[0]}" if fire_os.startswith("Fire OS") else ""))


def ws_call(r: Run, message: dict) -> dict:
    output = run("docker", "run", "--rm", "-i", "--network", "host", "--entrypoint", "python3", r.tools_image,
                 "-c", WS_SCRIPT, r.ha.url, input=f"{r.ha.access}\n{json.dumps(message)}", check=False, timeout=120)
    return json.loads(output) if output.strip() else {}


def devtools(r: Run, expression: str, timeout: float = 90):
    pid = adb("shell", "pidof", PACKAGE).strip()
    if not pid:
        raise RuntimeError("PleatView isn't running")
    adb("forward", "--remove", f"tcp:{DEVTOOLS_PORT}", check=False)
    adb("forward", f"tcp:{DEVTOOLS_PORT}", f"localabstract:webview_devtools_remote_{pid}")
    output = run("docker", "run", "--rm", "-i", "--network", "host", "--entrypoint", "python3", r.tools_image,
                 "-c", CDP_SCRIPT, str(DEVTOOLS_PORT), str(timeout - 5), input=expression, timeout=timeout)
    return json.loads(output)


def set_options(ha: HomeAssistant, entry_id: str, changes: dict) -> None:
    """Change some of the integration's options, keeping the rest."""
    _, flow = ha.api("POST", "/api/config/config_entries/options/flow", {"handler": entry_id})
    if not isinstance(flow, dict) or "data_schema" not in flow:
        raise AssertionError(f"couldn't open the options: {str(flow)[:200]}")
    values = {}
    for field in flow["data_schema"]:
        if "default" in field:
            values[field["name"]] = field["default"]
        elif (field.get("description") or {}).get("suggested_value") is not None:
            values[field["name"]] = field["description"]["suggested_value"]
    _, result = ha.api("POST", f"/api/config/config_entries/options/flow/{flow['flow_id']}", {**values, **changes})
    if not isinstance(result, dict) or result.get("type") != "create_entry":
        # An existing Home Assistant only loads new integration code when it restarts, and
        # until then it rejects options it doesn't know.
        raise AssertionError(f"couldn't change the options {sorted(changes)}: {str(result)[:200]}"
                             " (restart Home Assistant after updating the integration)")


def showcase_dashboard(present: set[str]) -> dict:
    """A tidy dashboard of the demo integration's devices, for the pictures: three columns
    of about the same height, with short names."""

    def heading(text: str, icon: str) -> list[dict]:
        return [{"type": "heading", "heading": text, "icon": icon}]

    def tiles(*entities: tuple[str, str], features: list[dict] | None = None) -> list[dict]:
        return [{"type": "tile", "entity": e, "name": name, **({"features": features} if features else {})}
                for e, name in entities if e in present]

    def card(card_type: str, entity: str, **extra) -> list[dict]:
        return [{"type": card_type, "entity": entity, **extra}] if entity in present else []

    sections = [
        heading("Living room", "mdi:sofa")
        + card("thermostat", "climate.hvac", name="Climate")
        + tiles(("sensor.outside_temperature", "Outside"), ("sensor.outside_humidity", "Humidity"),
                ("fan.living_room_fan", "Fan"), ("switch.decorative_lights", "Fairy lights")),
        heading("Lights", "mdi:lightbulb-group")
        + tiles(("light.living_room_rgbww_lights", "Living room"), ("light.ceiling_lights", "Ceiling"),
                ("light.kitchen_lights", "Kitchen"), ("light.office_rgbw_lights", "Office"),
                features=[{"type": "light-brightness"}])
        + heading("Security", "mdi:shield-home")
        + tiles(("alarm_control_panel.security", "Alarm"), ("lock.front_door", "Front door"),
                ("lock.kitchen_door", "Back door"), ("cover.garage_door", "Garage")),
        heading("Weather", "mdi:weather-partly-cloudy")
        + card("weather-forecast", "weather.demo_weather_south", name="Home", forecast_type="daily")
        + heading("Energy", "mdi:lightning-bolt")
        + [{**tile, "grid_options": {"columns": 12}} for tile in tiles(("sensor.power_consumption", "Using now"))]
        + heading("Blinds", "mdi:blinds")
        + tiles(("cover.living_room_window", "Living room"), ("cover.kitchen_window", "Kitchen")),
    ]
    return {"title": "Our home", "views": [{"title": "Our home", "type": "sections", "max_columns": 3,
                                        "sections": [{"type": "grid", "cards": cards} for cards in sections]}]}


def checks(r: Run, report: Report, apk: Path) -> None:
    ha = r.ha
    entity = {}
    debuggable = "debug" in apk.name

    def setup_screen():
        entity["pin"] = wait_for("the PIN on the setup screen", read_pin, timeout=60)
        # The QR code opens the TV's phone page with the PIN filled in. Right after boot the
        # network may still be coming up; the setup screen checks again every 10 s.
        code = wait_for("the QR code on the setup screen",
                        lambda: screen_attribute("setup_qr", "content-desc"), timeout=30)
        report.screenshot("01-setup")
        if not (code.startswith("http://") and code.endswith(f":8765/#pin={entity['pin']}")):
            raise AssertionError(f"the QR code links to {code!r}, not the phone page with PIN {entity['pin']}")
        status, page = http_text(f"{r.tv_api}/")
        if status != 200 or 'id="start"' not in page:
            raise AssertionError(f"the phone page doesn't offer adding the TV ({status})")
        return f"PIN and QR code shown ({code.split('#')[0]})"

    def demo():
        # For people without Home Assistant, and app reviewers: a sample home and a tour of
        # what Home Assistant can send, which ends back at the setup screen.
        pid = adb("shell", "pidof", PACKAGE).strip()
        touch = r.args.form_factor != "tv"
        wait_for("the demo button", lambda: view_bounds("setup_demo"), timeout=15)

        def press(label: str) -> None:
            # On a TV the button is already focused; touch screens tap it.
            if touch:
                x, y = wait_for(f"the {label} button", lambda: text_center(label, screen_texts()), timeout=30)
                adb("shell", "input", "tap", str(x), str(y))
            else:
                adb("shell", "input", "keyevent", "KEYCODE_DPAD_CENTER")

        press("Try the demo")
        # The sample pages' clocks tick every second, so UI dumps rarely settle while the
        # dashboard shows: these waits look at PleatView's own views instead.
        wait_for("the demo's first caption", lambda: view_bounds("demo_caption"), timeout=40, interval=1)
        report.screenshot("01-demo")
        wait_for("the demo's notification", lambda: view_bounds("toast_card"), timeout=30, interval=1)
        wait_for("the demo's door camera", lambda: view_bounds("web_overlay_container"), timeout=30, interval=1)
        time.sleep(2)  # the camera's page draws a moment after its corner appears
        report.screenshot("01-demo-camera")
        # Back closes the camera first, as it would one from Home Assistant; then it opens the menu.
        adb("shell", "input", "keyevent", "KEYCODE_BACK")
        wait_for("Back to close the door camera", lambda: not view_bounds("web_overlay_container"), timeout=10, interval=1)
        adb("shell", "input", "keyevent", "KEYCODE_BACK")
        # The overlay, not the panel: a view in a hidden layer still calls itself visible.
        wait_for("the demo's menu", lambda: view_bounds("menu_overlay"), timeout=15, interval=1)
        press("End the demo")
        pin = wait_for("the setup screen again", read_pin, timeout=30)
        if adb("shell", "pidof", PACKAGE).strip() != pid:
            raise AssertionError("PleatView restarted during the demo")
        if pin != entity["pin"]:
            raise AssertionError(f"the PIN changed from {entity['pin']} to {pin}")
        return "sample home, notification and door camera; End the demo went back to the setup screen"

    def pairing():
        # A TV that was set up before (a real one keeps its ID across reinstalls) would abort
        # with "already_configured". The run has just reinstalled the app, so that entry is dead.
        tv_id = http("GET", f"{r.tv_api}/api/info")[1].get("id")
        for device in ws_call(r, {"type": "config/device_registry/list"}).get("result") or []:
            if ["pleatview", tv_id] in device.get("identifiers", []):
                for entry_id in device.get("config_entries", []):
                    print(f"  Removing this TV's earlier Home Assistant entry ({entry_id[:8]}…)", flush=True)
                    ha.api("DELETE", f"/api/config/config_entries/entry/{entry_id}")
        _, flow = ha.api("POST", "/api/config/config_entries/flow", {"handler": "pleatview"})
        if not isinstance(flow, dict) or "flow_id" not in flow:
            raise AssertionError(f"couldn't start the PleatView setup: {str(flow)[:200]}")
        flow_path = f"/api/config/config_entries/flow/{flow['flow_id']}"
        try:
            _, result = ha.api("POST", flow_path, {"host": r.tv_address[0], "port": r.tv_address[1]})
            if result.get("step_id") == "approve":
                # Home Assistant asks the TV first (one-press pairing). Back refuses, and the
                # setup falls back to the PIN.
                wait_for("the pairing question on the TV", lambda: "Pair with Home Assistant?" in screen_texts(), timeout=30)
                report.screenshot("01-approve")
                adb("shell", "input", "keyevent", "KEYCODE_BACK")
                result = wait_for("the PIN step after refusing",
                                  lambda: (step := ha.api("GET", flow_path)[1]).get("step_id") == "pair" and step,
                                  timeout=30)
            if result.get("step_id") != "pair":
                raise AssertionError(f"expected the PIN step, got {result}")
            _, result = ha.api("POST", flow_path, {"pin": "000000"})
            if result.get("errors") != {"base": "invalid_pin"}:
                raise AssertionError(f"wrong PIN was not rejected: {result}")
            _, result = ha.api("POST", flow_path, {"pin": entity["pin"]})
            if result.get("type") != "create_entry":
                raise AssertionError(f"setup didn't finish at the PIN: {result}")
        except Exception:
            # A flow left open makes the next run against the same Home Assistant abort
            # with "already_in_progress".
            ha.api("DELETE", flow_path)
            raise
        r.entry_id = result["result"]["entry_id"]

        # This TV's own entities. A Home Assistant with other PleatView TVs has their media
        # players too, and a run once sent half its commands to one of them.
        def own_entities():
            listed = ws_call(r, {"type": "config/entity_registry/list"}).get("result") or []
            found = {e["entity_id"].split(".")[0]: e["entity_id"] for e in listed
                     if e.get("config_entry_id") == r.entry_id}
            return found if "media_player" in found else None

        found = wait_for("this TV's entities in Home Assistant", own_entities, timeout=30)
        entity["id"] = found["media_player"]
        entity["notify"] = found.get("notify", entity["id"].replace("media_player.", "notify."))
        entity["battery"], entity["charging"] = found.get("sensor"), found.get("binary_sensor")
        return entity["id"]

    def state():
        return ha.state(entity["id"])

    def ready():
        current = state()
        return current["state"] == "idle" and current["attributes"].get("ha_connected") and current

    def signed_in():
        current = wait_for("the dashboard to sign in", ready, timeout=120)
        time.sleep(5)
        report.screenshot("02-dashboard")
        if current["attributes"].get("auth_error"):
            raise AssertionError("the TV reports an authentication error")
        return f"{current['attributes'].get('url')}"

    def kiosk():
        if not debuggable:
            raise SkipCheck("needs a debug build")
        seen = {}

        def applied():
            seen.update(devtools(r, KIOSK_PROBE))
            return seen["panelLeft"] == 0 and seen["header"] == "none"

        try:
            wait_for("kiosk styles", applied, timeout=45)
        except TimeoutError as err:
            raise AssertionError(f"{err}; last seen {seen}") from None
        return f"panel at x={seen['panelLeft']}, header {seen['header']}"

    def notification():
        ha.call("notify", "send_message", {"entity_id": entity["notify"], "title": "E2E",
                                           "message": "Hello from the end-to-end test"})
        wait_for("the notification card", lambda: "Hello from the end-to-end test" in screen_texts(),
                 timeout=20, interval=1)
        report.screenshot("03-notification")
        # Cards are 440 dp wide where there's room; a phone is narrower.
        left, _, right, _ = view_bounds("toast_card")
        screen = view_bounds("root")[2]
        if not fits(right - left, screen):
            raise AssertionError(f"the notification card ({right - left} px) doesn't fit the {screen} px screen")
        # An animated GIF: Android 9's animated decoder once crashed the app on it. From
        # Android 10 the picture should move.
        pid = adb("shell", "pidof", PACKAGE).strip()
        ha.call("pleatview", "notify", {"entity_id": entity["id"], "message": "Animated picture",
                                        "image": f"{r.site}/e2e.gif", "duration": 30})
        bounds = wait_for("the notification picture", lambda: screen_attribute("toast_image", "bounds"), timeout=20)
        box = [int(n) for n in re.findall(r"\d+", bounds)]
        first = frame(box, r.tools_image)
        time.sleep(1.5)
        moving = frame(box, r.tools_image) != first
        time.sleep(6)
        if adb("shell", "pidof", PACKAGE).strip() != pid:
            raise AssertionError("PleatView restarted while showing an animated picture")
        animates = int(adb("shell", "getprop", "ro.build.version.sdk").strip()) >= 29
        if animates and not moving:
            raise AssertionError("the animated picture in the notification doesn't move")
        return "animated picture moves" if moving else "animated picture shows its first frame (Android 9)"

    def video():
        ha.call("media_player", "play_media", {"entity_id": entity["id"], "media_content_type": "video",
                                               "media_content_id": r.video_url, "extra": {"title": "E2E clip"}})
        playing = wait_for("playback", lambda: (s := state())["state"] == "playing" and s, timeout=45)
        report.screenshot("04-video")
        if r.args.form_factor != "tv":
            # A 16:9 video on a phone or tablet has black bars above and below it. The
            # picture-in-picture frame used to show at their edges (and the dashboard through it).
            _, _, width, height = view_bounds("root")
            top = height // 100
            # On a 16:9 screen the video fills it: no bars to check.
            if abs(width / height - 16 / 9) > 0.02 and (edge := brightest([0, top, 6, top + 6], r.tools_image)) > 16:
                raise AssertionError(f"the full-screen video has a frame at the screen's edge (brightness {edge})")
        ha.call("media_player", "media_stop", {"entity_id": entity["id"]})
        wait_for("the dashboard after stop", lambda: state()["state"] == "idle", timeout=20)
        return f"title {playing['attributes'].get('media_title')!r}, duration {playing['attributes'].get('media_duration')}"

    def levels():
        # A warning plays a tone and leaves what plays alone; an alert pauses it.
        ha.call("media_player", "play_media", {"entity_id": entity["id"], "media_content_type": "video",
                                               "media_content_id": r.video_url, "extra": {"title": "E2E clip"}})
        wait_for("playback", lambda: state()["state"] == "playing", timeout=45)
        try:
            ha.call("pleatview", "notify", {"entity_id": entity["id"], "title": "E2E", "message": "A warning",
                                            "level": "warning", "tone": "chime"})
            wait_for("the warning card", lambda: "A warning" in screen_texts(), timeout=20, interval=1)
            report.screenshot("03-warning")
            time.sleep(3)
            if state()["state"] != "playing":
                raise AssertionError(f"a warning stopped the video ({state()['state']})")
            ha.call("pleatview", "notify", {"entity_id": entity["id"], "title": "E2E", "message": "An alert",
                                            "level": "alert", "tone": "alarm", "duration": 45})
            wait_for("the alert to pause the video", lambda: state()["state"] == "paused", timeout=15, interval=1)
            report.screenshot("03-alert")
            # The alert's card has a red edge (colors.xml). Screen dumps don't finish while the
            # paused player's controls are up, so look at the picture: its top-right corner.
            edge = wait_for("the alert card's red edge", lambda: (n := colour_pixels((0.5, 0, 1, 0.4), (255, 110, 110), r.tools_image)) > 500 and n,
                            timeout=20, interval=1)
            # Both tones played (the emulator's sound goes nowhere, but Android plays it all the same).
            tones = adb("logcat", "-d", "-s", "PleatView:*", check=False)
            for tone in ("chime", "alarm"):
                if f"Tone: {tone}" not in tones:
                    why = tones.split("No tone", 1)[1][:120] if "No tone" in tones else ""
                    raise AssertionError(f"the {tone} didn't play{why}")
        finally:
            ha.call("media_player", "media_stop", {"entity_id": entity["id"]})
            wait_for("the dashboard after stop", lambda: state()["state"] == "idle", timeout=20)
        return f"the warning's chime left the video playing; the alert paused it with its alarm, its card edged in red ({edge} px)"

    def camera():
        ha.call("pleatview", "show_camera", {"entity_id": entity["id"], "camera": "camera.demo_camera",
                                             "mode": "pip", "duration": 60})
        wait_for("the camera picture-in-picture",
                 lambda: (s := state())["state"] == "playing" and s["attributes"].get("media_content_type") == "image",
                 timeout=30)
        time.sleep(3)
        report.screenshot("05-camera-pip")
        ha.call("media_player", "media_stop", {"entity_id": entity["id"]})
        wait_for("the dashboard after stop", lambda: state()["state"] == "idle", timeout=20)

    def dashboards():
        home = state()["attributes"].get("dashboard")
        ha.call("pleatview", "show_dashboard", {"entity_id": entity["id"], "path": "/map"})
        wait_for("the map dashboard", lambda: str(state()["attributes"].get("dashboard", "")).startswith("/map"),
                 timeout=30)
        time.sleep(3)
        report.screenshot("06-map")
        ha.call("pleatview", "home", {"entity_id": entity["id"]})
        wait_for("the home dashboard",
                 lambda: not str(state()["attributes"].get("dashboard", "")).startswith("/map"), timeout=30)
        return f"{home} → /map → home"

    def rotation_waits():
        # Issue #32: rotation replaced a dashboard Home Assistant had just shown, long before
        # its duration was up. No remote key has been pressed yet, so rotation is free to run.
        seen: list[str] = []

        def showing() -> str:
            current = str(state()["attributes"].get("dashboard"))
            if not seen or seen[-1] != current:
                seen.append(current)
            return current

        def settle() -> None:
            # New options reload the integration, which then sends the TV its settings again.
            time.sleep(8)
            wait_for("the TV after its new settings", ready, timeout=60)

        # The map isn't in the rotation, so rotation would replace it with the overview.
        set_options(ha, r.entry_id, {"dashboards": "/home/overview", "rotate_seconds": 10})
        try:
            settle()
            ha.call("pleatview", "show_dashboard", {"entity_id": entity["id"], "path": "/map", "duration": 25})
            try:
                wait_for("the map", lambda: showing().startswith("/map"), timeout=30, interval=1)
                for _ in range(18):  # more than the rotation period, less than the duration
                    time.sleep(1)
                    if not showing().startswith("/map"):
                        raise AssertionError("rotation replaced the dashboard before its 25 seconds were up")
                wait_for("the dashboard to go back", lambda: not showing().startswith("/map"), timeout=30, interval=1)
            except (AssertionError, TimeoutError) as err:
                raise AssertionError(f"{err}; the TV showed {' → '.join(seen)}") from None
            return "kept for its 25 seconds despite rotation every 10, then went back"
        finally:
            set_options(ha, r.entry_id, {"dashboards": "", "rotate_seconds": 0})
            settle()
            ha.call("pleatview", "home", {"entity_id": entity["id"]})

    def comes_back():
        # Someone presses Home and forgets PleatView: with nothing playing sound, it comes back
        # after reopen_minutes. (A settings side panel doesn't count: PleatView stays visible.)
        # Android 10+ needs "Display over other apps".
        def resumed() -> str:
            return adb("shell", "dumpsys activity activities | grep -E 'ResumedActivity'", check=False).strip()

        adb("shell", "appops", "set", PACKAGE, "SYSTEM_ALERT_WINDOW", "allow")
        set_options(ha, r.entry_id, {"reopen_minutes": 1})
        try:
            time.sleep(8)  # new options reload the integration, which sends the TV its settings again
            wait_for("the TV after its new settings", ready, timeout=60)
            screen = view_bounds("root")[2]
            adb("shell", "input", "keyevent", "KEYCODE_HOME")
            wait_for("the home screen in front", lambda: PACKAGE not in resumed(), timeout=20)
            left = time.time()
            # Meanwhile a notification shows over the other app, and fits the screen. Until
            # PleatView knows it's hidden (the entity turns off), a card would go to its own screen.
            wait_for("the entity to turn off", lambda: state()["state"] == "off", timeout=20)
            ha.call("pleatview", "notify", {"entity_id": entity["id"], "message": "Over another app", "duration": 10})
            try:
                width, _ = wait_for("the card over the other app", lambda: (card := overlay_card()) and card[1] and card,
                                    timeout=15, interval=1)
            except TimeoutError as err:
                windows = adb("shell", "dumpsys window windows | grep -E 'Window #|isOnScreen|isReadyForDisplay'",
                              check=False)
                raise AssertionError(f"{err}; windows: {' '.join(windows.split())[:600]}") from None
            report.screenshot("06-over-another-app")
            if not fits(width, screen):
                raise AssertionError(f"the card over another app ({width} px) doesn't fit the {screen} px screen")
            try:
                wait_for("PleatView to come back", lambda: PACKAGE in resumed(), timeout=150, interval=5)
            except TimeoutError as err:
                sound = adb("shell", "dumpsys audio | grep -iE 'state:started|isMusicActive'", check=False).strip()
                raise AssertionError(f"{err}; in front: {resumed()[:160]}; playing: {sound[:200] or 'nothing'}") from None
            return f"a {width} px card on the {screen} px screen; back after {time.time() - left:.0f} s"
        finally:
            if PACKAGE not in resumed():
                adb("shell", "am", "start", "-n", f"{PACKAGE}/.MainActivity")
            set_options(ha, r.entry_id, {"reopen_minutes": 10})
            time.sleep(8)
            wait_for("the TV after its new settings", ready, timeout=60)

    def pip_over_other_apps():
        # A door camera in a corner while someone watches a film in another app: it stays over
        # that app, which keeps the screen, the remote and the sound (testers asked for this).
        def resumed() -> str:
            return adb("shell", "dumpsys activity activities | grep -E 'ResumedActivity'", check=False).strip()

        def pip_window() -> str | None:
            for window in adb("shell", "dumpsys", "window", "windows", check=False).split("Window #")[1:]:
                if "PleatView picture-in-picture" in window.split("\n", 1)[0]:
                    return window
            return None

        adb("shell", "appops", "set", PACKAGE, "SYSTEM_ALERT_WINDOW", "allow")
        adb("shell", "input", "keyevent", "KEYCODE_HOME")
        try:
            wait_for("the home screen in front", lambda: PACKAGE not in resumed(), timeout=20)
            wait_for("the entity to turn off", lambda: state()["state"] == "off", timeout=20)
            seen = []
            for what, call in (
                ("camera", lambda: ha.call("pleatview", "show_camera", {"entity_id": entity["id"],
                                           "camera": "camera.demo_camera", "mode": "pip", "duration": 60})),
                ("video", lambda: ha.call("media_player", "play_media", {
                    "entity_id": entity["id"], "media_content_type": "video", "media_content_id": r.video_url,
                    "extra": {"mode": "pip", "position": "top-right", "size": 30}})),
            ):
                call()
                wait_for(f"the {what} over the other app", lambda: (w := pip_window()) and any(
                    sign in w for sign in ("Surface: shown=true", "isReadyForDisplay()=true", "isOnScreen=true")),
                    timeout=20, interval=1)
                time.sleep(3)
                if PACKAGE in resumed():
                    raise AssertionError(f"the {what} brought PleatView to the front instead of staying over the other app")
                # Home Assistant shows it as playing, so its media card can stop it.
                shown = wait_for("Home Assistant to show it playing", lambda: (s := state())["state"] == "playing" and s,
                                 timeout=15, interval=1)
                report.screenshot(f"06-pip-over-another-app-{what}")
                seen.append(f"{what} ({shown['attributes'].get('media_content_type')})")
            ha.call("media_player", "media_stop", {"entity_id": entity["id"]})
            wait_for("the picture-in-picture to close", lambda: pip_window() is None, timeout=15, interval=1)
            wait_for("the entity to turn off again", lambda: state()["state"] == "off", timeout=15, interval=1)
            return f"{' and '.join(seen)} over the home screen, playing in Home Assistant; stop closed it"
        finally:
            if PACKAGE not in resumed():
                adb("shell", "am", "start", "-n", f"{PACKAGE}/.MainActivity")
            wait_for("the TV back on its dashboard", ready, timeout=60)

    def webpage():
        # Home Assistant would show the site in a frame, where many sites refuse to load or
        # lose their cookies; PleatView opens the site itself.
        created = ws_call(r, {"type": "lovelace/dashboards/create", "url_path": "dashboard-pleatview-e2e",
                              "title": "E2E web page", "mode": "storage", "show_in_sidebar": True})
        if not created.get("success"):
            raise AssertionError(f"couldn't create a Webpage dashboard: {created}")
        try:
            ws_call(r, {"type": "lovelace/config/save", "url_path": "dashboard-pleatview-e2e",
                        "config": {"strategy": {"type": "iframe", "url": r.page_url}}})
            wait_for("the new dashboard among the TV's sources",
                     lambda: "E2E web page" in (state()["attributes"].get("source_list") or []), timeout=20)
            ha.call("pleatview", "show_dashboard", {"entity_id": entity["id"], "path": "/dashboard-pleatview-e2e"})
            wait_for("the web page on the TV", lambda: state()["attributes"].get("url") == r.page_url, timeout=30)
            time.sleep(2)
            report.screenshot("07-webpage")
            # A late heartbeat from the Home Assistant page must not take the source back.
            time.sleep(6)
            source = state()["attributes"].get("source")
            if source != "E2E web page":
                raise AssertionError(f"the TV shows the page, but its source is {source!r}")
        finally:
            ws_call(r, {"type": "lovelace/dashboards/delete", "dashboard_id": created["result"]["id"]})
            ha.call("pleatview", "home", {"entity_id": entity["id"]})
        return f"{r.page_url} opened directly"

    def page_steps():
        # Steps in the page's address (#pleatview=…): a click on text clears a cookie banner,
        # one on CSS closes a pop-up, then a drag and a scroll, as real input.
        site = r.media_server
        created = ws_call(r, {"type": "lovelace/dashboards/create", "url_path": "dashboard-pleatview-steps",
                              "title": "E2E steps", "mode": "storage", "show_in_sidebar": True})
        if not created.get("success"):
            raise AssertionError(f"couldn't create a Webpage dashboard: {created}")
        expected = ["steps: consent Reject all", "steps: promo closed", "steps: map dragged", "steps: scrolled down"]
        try:
            site.events.clear()
            ws_call(r, {"type": "lovelace/config/save", "url_path": "dashboard-pleatview-steps",
                        "config": {"strategy": {"type": "iframe", "url": f"{r.site}/steps/#pleatview={STEPS}"}}})
            wait_for("the new dashboard among the TV's sources",
                     lambda: "E2E steps" in (state()["attributes"].get("source_list") or []), timeout=20)
            seen = lambda: [e for e in site.events if e.startswith("steps: ")]  # noqa: E731

            def took_steps(how: str) -> None:
                try:
                    wait_for(f"the page's steps ({how})", lambda: len(seen()) >= len(expected), timeout=60)
                except TimeoutError:
                    notes = [e for e in site.events if e.startswith("note: ")]
                    raise AssertionError(f"{how}: the page saw only {seen()}; expected {expected}; {notes}") from None
                if seen() != expected:
                    raise AssertionError(f"{how}: the page saw {seen()}; expected {expected}, in that order")

            ha.call("pleatview", "show_dashboard", {"entity_id": entity["id"], "path": "/dashboard-pleatview-steps"})
            took_steps("a Webpage dashboard")
            report.screenshot("07-page-steps")
            site.events.clear()
            ha.call("pleatview", "show_url", {"entity_id": entity["id"], "url": f"{r.site}/steps/#pleatview={STEPS}"})
            took_steps("show_url")
        finally:
            ws_call(r, {"type": "lovelace/dashboards/delete", "dashboard_id": created["result"]["id"]})
            ha.call("pleatview", "home", {"entity_id": entity["id"]})
        return ("a cookie banner cleared by its text, a pop-up closed by CSS, a map dragged and the page scrolled, "
                "on a Webpage dashboard and with show_url")

    def connects_out():
        # Protocol v2: a controller elsewhere (here a small test one, over TLS with a certificate
        # of its own). It pairs over v1 like any controller, with one press of OK, then moves the
        # TV to v2 with a one-time token; from then on it reaches the TV over the TV's connection.
        from controller_v2 import TestController
        controller = TestController()
        v1_token = None
        try:
            address = f"wss://{'127.0.0.1' if r.device else EMULATOR_HOST}:{controller.port}/api/pleatview/v2"
            if r.device:
                adb("reverse", f"tcp:{controller.port}", f"tcp:{controller.port}")
            controller.address = address
            status, asked = http("POST", f"{r.tv_api}/api/pair/request", {"client": "E2E controller"})
            if status != 202:
                raise AssertionError(f"couldn't ask to pair: {status} {asked}")
            wait_for("the pairing question on the TV", lambda: "E2E controller" in screen_texts(), timeout=30)
            adb("shell", "input", "keyevent", "KEYCODE_DPAD_CENTER")
            def pairing_token():
                answer = http("GET", f"{r.tv_api}/api/pair/request/{asked['request']}")[1]
                return answer.get("token") if isinstance(answer, dict) else None
            v1_token = wait_for("the pairing token", pairing_token, timeout=20)
            status, reply = http("POST", f"{r.tv_api}/api/enrol",
                                 {"url": address, "token": controller.new_token(), "fingerprint": controller.pin}, token=v1_token)
            if status != 202:
                raise AssertionError(f"the TV didn't take the move to v2: {status} {reply}")
            hello = wait_for("the TV to connect out", lambda: controller.hellos and controller.hellos[0], timeout=30)
            if hello.get("type") != "enrol" or "token" not in hello or controller.problems:
                raise AssertionError(f"unexpected enrolment {hello.get('type')}: {controller.problems}")
            state = wait_for("its state over the connection", lambda: controller.states and controller.states[-1], timeout=20)
            if "protocol_v2" not in state.get("features", []):
                raise AssertionError(f"features {state.get('features')}")
            answer = controller.request("command", {"command": "notify", "message": "From afar", "duration": 20})
            if "result" not in answer:
                raise AssertionError(f"the command was refused: {answer}")
            wait_for("the controller's notification", lambda: "From afar" in screen_texts(), timeout=20, interval=1)
            report.screenshot("09-connects-out")
            config = controller.request("config").get("result", {})
            if "zoom" not in config:
                raise AssertionError(f"config over v2: {config}")
            refused = controller.request("command", {"command": "explode"})
            if "error" not in refused:
                raise AssertionError(f"a bad command wasn't refused: {refused}")
            # Upkeep (docs/protocol.md): its checks, then it starts again and signs back in.
            checks = controller.request("diagnostics", timeout=20).get("result", {})
            if not isinstance(checks.get("memory"), dict) or not isinstance(checks.get("network"), list) or "log" not in checks:
                raise AssertionError(f"diagnostics over v2: {sorted(checks)}")
            if not {"restart_app", "diagnostics"} <= set(state.get("features", [])):
                raise AssertionError(f"features {state.get('features')}")
            device = controller.request("command", {"command": "restart", "what": "device"})
            if device.get("error") != "not_supported":
                raise AssertionError(f"restarting the device should be refused on Android: {device}")
            process = adb("shell", "pidof", PACKAGE, check=False).strip()
            if "result" not in controller.request("command", {"command": "restart", "what": "app"}):
                raise AssertionError("restarting the app was refused")
            again = wait_for("the TV to sign back in after starting again",
                             lambda: len(controller.hellos) >= 2 and controller.hellos[1], timeout=60)
            if again.get("type") != "auth":
                raise AssertionError(f"after starting again it sent {again.get('type')}")
            started = adb("shell", "pidof", PACKAGE, check=False).strip()
            if not started or started == process:
                raise AssertionError(f"the app's process didn't change ({process} → {started})")
            wait_for("Home Assistant to see it again", lambda: ha.state(entity["id"]).get("state") not in (None, "unavailable"), timeout=60)
            # Removed by the controller: the TV forgets it and doesn't come back.
            signed_in = len(controller.hellos)
            controller.revoke()
            time.sleep(6)
            if len(controller.hellos) != signed_in:
                raise AssertionError(f"the TV came back {len(controller.hellos) - signed_in} time(s) after being removed")
        finally:
            if v1_token:
                http("POST", f"{r.tv_api}/api/unpair", {}, token=v1_token)
            controller.stop()
        return ("paired with OK, moved to v2 with a token over TLS with a pinned certificate; a notification, "
                "its settings and diagnostics over the connection; started again and signed back in; removed, it stays away")

    def sign_ins():
        site = r.media_server
        created = ws_call(r, {"type": "lovelace/dashboards/create", "url_path": "dashboard-pleatview-signin",
                              "title": "E2E sign-in", "mode": "storage", "show_in_sidebar": True})
        if not created.get("success"):
            raise AssertionError(f"couldn't create a Webpage dashboard: {created}")
        show = {"entity_id": entity["id"], "path": "/dashboard-pleatview-signin"}

        def use(url: str) -> None:
            ws_call(r, {"type": "lovelace/config/save", "url_path": "dashboard-pleatview-signin",
                        "config": {"strategy": {"type": "iframe", "url": url}}})
            time.sleep(2)  # Home Assistant tells the integration about the change

        def sign_in_from_a_phone(confirmation: str, screenshot: str) -> None:
            wait_for("the TV to ask for a sign-in",
                     lambda: state()["attributes"].get("sign_in_needed") == "E2E sign-in", timeout=45)
            report.screenshot(screenshot)
            # What a phone does after scanning the code on the card.
            code = wait_for("the sign-in code", lambda: screen_attribute("signin_qr", "content-desc"), timeout=15)
            pin = re.search(r"pin=(\d{6})", code).group(1)
            status, reply = http("POST", f"{r.tv_api}/api/signin",
                                 {"pin": pin, "username": SITE_USER, "password": SITE_PASSWORD, "remember": True})
            if status != 200:
                raise AssertionError(f"signing in from the phone page failed: {status} {reply}")
            wait_for(f"the test site to see '{confirmation}'", lambda: confirmation in site.events, timeout=45)
            wait_for("the sign-in card to close", lambda: state()["attributes"].get("sign_in_needed") is None, timeout=20)

        def saved(count: int) -> None:
            # PleatView saves a sign-in once it sees the site accept it, a moment after the
            # site itself does: wait for the menu to count it before restarting or reloading.
            def counted():
                adb("shell", "input", "keyevent", "KEYCODE_MENU")
                # The item sits near the end of the menu, which scrolls on a TV.
                adb("shell", "input", "keyevent", *["KEYCODE_DPAD_DOWN"] * 25)
                texts = screen_texts()
                if "Home dashboard" in texts:
                    adb("shell", "input", "keyevent", "KEYCODE_MENU")  # closes it again
                return f"Forget saved sign-ins ({count})" in texts
            wait_for(f"the TV to save sign-in {count}", counted, timeout=30, interval=1)

        def signs_in_by_itself(confirmation: str) -> None:
            deadline = time.monotonic() + 45
            while confirmation not in site.events:
                if state()["attributes"].get("sign_in_needed"):
                    raise AssertionError(f"the TV asked again instead of using the saved sign-in ('{confirmation}')")
                if time.monotonic() > deadline:
                    raise TimeoutError(f"timed out waiting for the test site to see '{confirmation}'")
                time.sleep(1)

        try:
            # A password prompt. After a restart, nothing is left in memory: the saved sign-in answers.
            use(f"{r.site}/prompt/")
            ha.call("pleatview", "show_dashboard", show)
            sign_in_from_a_phone("prompt ok", "08-sign-in-prompt")
            saved(1)
            site.events.clear()
            adb("shell", "am", "force-stop", PACKAGE)
            adb("shell", "am", "start", "-n", f"{PACKAGE}/.MainActivity")
            wait_for("the TV after a restart", ready, timeout=120)
            ha.call("pleatview", "show_dashboard", show)
            signs_in_by_itself("prompt ok")

            # A login form whose session works once: reloading needs the saved sign-in.
            use(f"{r.site}/form/")
            ha.call("pleatview", "show_dashboard", show)
            sign_in_from_a_phone("form ok", "09-sign-in-form")
            saved(2)
            site.events.clear()
            ha.call("pleatview", "reload", {"entity_id": entity["id"]})
            signs_in_by_itself("form ok")
        finally:
            ws_call(r, {"type": "lovelace/dashboards/delete", "dashboard_id": created["result"]["id"]})
            ha.call("pleatview", "forget_sign_ins", {"entity_id": entity["id"]})
            ha.call("pleatview", "home", {"entity_id": entity["id"]})
        return "a password prompt and a login form, from the phone code, then from the saved sign-in"

    def standby():
        ha.call("media_player", "turn_off", {"entity_id": entity["id"]})
        wait_for("standby", lambda: state()["state"] == "off", timeout=20)
        ha.call("media_player", "turn_on", {"entity_id": entity["id"]})
        wait_for("wake", lambda: state()["state"] == "idle", timeout=20)

    def ha_restart():
        if r.external:
            raise SkipCheck("won't restart an existing Home Assistant")
        run("docker", "restart", HA_CONTAINER)
        wait_for("Home Assistant to restart", lambda: http("GET", f"{ha.url}/api/")[0] in (200, 401), timeout=300)
        wait_for("the TV to reconnect", ready, timeout=180)

    def frozen_page():
        # 1 Nov soak: after a Wi-Fi drop a page froze the Fire TV's renderer, and every later
        # load went into that same renderer. PleatView has to start a new one.
        if not debuggable:
            raise SkipCheck("needs a debug build")
        wait_for("the dashboard", ready, timeout=60)
        devtools(r, "window.__e2eFrozen = true; setTimeout(() => { for (;;) {} }, 100); 'frozen'")
        started = time.monotonic()
        time.sleep(2)
        try:
            devtools(r, "1", timeout=15)
        except RuntimeError:
            pass
        else:
            raise AssertionError("the page still answered after the endless loop")
        wait_for("a new page", lambda: devtools(r, "window.__e2eFrozen ? 'old' : document.readyState", timeout=15)
                 == "complete", timeout=180, interval=5)
        wait_for("the dashboard to sign in again", ready, timeout=60)
        return f"a new page {time.monotonic() - started:.0f} s after it froze"

    def back_after_sign_in():
        # Issue #31: Back after "Not now" went back into the password-protected site, and
        # its card came back over the home dashboard, titled "Home Assistant".
        ha.call("pleatview", "forget_sign_ins", {"entity_id": entity["id"]})
        created = ws_call(r, {"type": "lovelace/dashboards/create", "url_path": "dashboard-pleatview-back",
                              "title": "E2E back", "mode": "storage", "show_in_sidebar": True})
        if not created.get("success"):
            raise AssertionError(f"couldn't create a Webpage dashboard: {created}")
        try:
            ws_call(r, {"type": "lovelace/config/save", "url_path": "dashboard-pleatview-back",
                        "config": {"strategy": {"type": "iframe", "url": f"{r.site}/prompt/"}}})
            time.sleep(2)
            ha.call("pleatview", "show_dashboard", {"entity_id": entity["id"], "path": "/dashboard-pleatview-back"})
            wait_for("the sign-in card", lambda: state()["attributes"].get("sign_in_needed") == "E2E back", timeout=45)
            adb("shell", "input", "keyevent", "KEYCODE_BACK")  # Not now
            wait_for("the card to close", lambda: not state()["attributes"].get("sign_in_needed"), timeout=15)
            ha.call("pleatview", "home", {"entity_id": entity["id"]})
            wait_for("the home dashboard",
                     lambda: str(state()["attributes"].get("dashboard") or "").startswith("/"), timeout=30)
            time.sleep(3)
            adb("shell", "input", "keyevent", "KEYCODE_BACK")
            time.sleep(3)
            if again := state()["attributes"].get("sign_in_needed"):
                raise AssertionError(f"Back brought the sign-in card back, titled {again!r}")
            if "Home dashboard" not in screen_texts():
                raise AssertionError("Back on the home dashboard didn't open the menu")
            adb("shell", "input", "keyevent", "KEYCODE_BACK")
            return "the menu opened, no card"
        finally:
            ws_call(r, {"type": "lovelace/dashboards/delete", "dashboard_id": created["result"]["id"]})
            ha.call("pleatview", "home", {"entity_id": entity["id"]})

    def battery():
        # Tablets and phones report their battery; TVs have none and get no sensors.
        if r.args.form_factor == "tv":
            if entity["battery"] or entity["charging"]:
                raise AssertionError(f"a TV got {entity['battery']} and {entity['charging']}")
            return "none on a TV"
        if not (entity["battery"] and entity["charging"]):
            raise AssertionError("no battery or charging sensor")
        level, charging = ha.state(entity["battery"])["state"], ha.state(entity["charging"])["state"]
        if not level.isdigit() or charging not in ("on", "off"):
            raise AssertionError(f"battery {level!r}, charging {charging!r}")
        return f"battery {level} %, charging {charging}"

    def ha_version_repair():
        # Dashboard cards before 2026.7 have published XSS bugs: PleatView asks for an update.
        version = ha.api("GET", "/api/config")[1]["version"]
        older = tuple(int(part) for part in version.split(".")[:2]) < (2026, 7)
        issues = ws_call(r, {"type": "repairs/list_issues"}).get("result", {}).get("issues", [])
        shown = any(i["domain"] == "pleatview" and i["issue_id"] == "old_home_assistant" for i in issues)
        if shown != older:
            raise AssertionError(f"Home Assistant {version} {'shows' if shown else 'lacks'} the update repair")
        return f"Home Assistant {version}: " + ("asks for an update" if shown else "no repair")

    def support_code():
        # The GitHub download (which this is) offers a donation QR code in its menu.
        ha.call("pleatview", "home", {"entity_id": entity["id"]})
        wait_for("the home dashboard",
                 lambda: str(state()["attributes"].get("dashboard") or "").startswith("/"), timeout=30)
        time.sleep(2)
        adb("shell", "input", "keyevent", "KEYCODE_BACK")
        texts = wait_for("the menu", lambda: (lambda t: t if "Home dashboard" in t else None)(screen_texts()),
                         timeout=15)
        # It sits near the end of the menu, which scrolls when there are many dashboards.
        for _ in range(20):
            if where := text_center("Support PleatView", texts):
                break
            adb("shell", "input", "keyevent", "KEYCODE_DPAD_DOWN")
            time.sleep(0.5)
            texts = screen_texts()
        else:
            raise AssertionError("the menu has no Support PleatView item")
        adb("shell", "input", "tap", str(where[0]), str(where[1]))
        code = wait_for("the QR code", lambda: screen_attribute("support_qr", "content-desc"), timeout=10)
        if code != DONATE_URL:
            raise AssertionError(f"the QR code opens {code!r}, not {DONATE_URL}")
        report.screenshot("08-support")
        adb("shell", "input", "keyevent", "KEYCODE_BACK")
        return f"QR code for {code}"

    def turned():
        # Turning a phone or tablet keeps PleatView and its page. Sideways, the sign-in card
        # puts its QR code beside the text: below it, the code was off the bottom of the screen.
        pid = adb("shell", "pidof", PACKAGE).strip()
        before = view_bounds("root")
        ha.call("pleatview", "forget_sign_ins", {"entity_id": entity["id"]})
        adb("shell", "settings", "put", "system", "accelerometer_rotation", "0")
        adb("shell", "settings", "put", "system", "user_rotation", "1")
        try:
            wait_for("the screen to turn", lambda: (b := view_bounds("root")) and (b[2] > b[3]) != (before[2] > before[3]),
                     timeout=15)
            ha.call("pleatview", "show_dashboard", {"entity_id": entity["id"], "path": f"{r.site}/prompt/"})
            wait_for("the sign-in card", lambda: state()["attributes"].get("sign_in_needed"), timeout=45)
            time.sleep(2)
            report.screenshot("11-turned-sign-in")
            bounds = screen_attribute("signin_qr", "bounds")
            top, bottom = (int(n) for n in re.findall(r"\d+", bounds)[1::2]) if bounds else (0, 0)
            if bottom - top < 220 * density() - 2:
                raise AssertionError(f"the sign-in card's QR code doesn't fit on the turned screen ({bounds or 'not shown'})")
            if adb("shell", "pidof", PACKAGE).strip() != pid:
                raise AssertionError("PleatView restarted when the screen turned")
            return f"same process; the QR code fits at {bounds}"
        finally:
            adb("shell", "input", "keyevent", "KEYCODE_BACK")
            adb("shell", "settings", "put", "system", "user_rotation", "0")
            ha.call("pleatview", "home", {"entity_id": entity["id"]})

    def touch_menu():
        # Phones, tablets and Chromebooks: a touch shows a menu button for a few seconds.
        ha.call("pleatview", "home", {"entity_id": entity["id"]})
        wait_for("the home dashboard",
                 lambda: str(state()["attributes"].get("dashboard") or "").startswith("/"), timeout=30)
        time.sleep(2)
        _, _, _, height = view_bounds("root")
        # Bottom left, where a dashboard rarely has a card that a tap would open.
        adb("shell", "input", "tap", "20", str(height - 20))
        left, top, right, bottom = wait_for("the menu button", lambda: view_bounds("touch_menu"),
                                            timeout=4, interval=0.5)
        adb("shell", "input", "tap", str((left + right) // 2), str((top + bottom) // 2))
        wait_for("the menu", lambda: "Home dashboard" in screen_texts(), timeout=15)
        report.screenshot("10-touch-menu")
        left, _, right, _ = view_bounds("menu_panel")
        if left < 0 or right > view_bounds("root")[2]:
            raise AssertionError(f"the menu ({left}-{right} px) runs off the screen")
        adb("shell", "input", "keyevent", "KEYCODE_BACK")
        # A tap on the black standby screen wakes it.
        ha.call("media_player", "turn_off", {"entity_id": entity["id"]})
        wait_for("standby", lambda: state()["state"] == "off", timeout=20)
        adb("shell", "input", "tap", "20", str(height - 20))
        wait_for("a tap to wake it", lambda: state()["state"] == "idle", timeout=20)
        return "a touch showed the menu button, which opened the menu; a tap woke the screen"

    def lock_screen():
        # A cast wakes a screen without a lock (a wall tablet) and shows the dashboard over
        # it. With a PIN it mustn't: the dashboard's controls would work without unlocking.
        def cast_while_asleep():
            ha.call("pleatview", "home", {"entity_id": entity["id"]})
            wait_for("the home dashboard", lambda: state()["state"] == "idle", timeout=30)
            adb("shell", "input", "keyevent", "KEYCODE_SLEEP")
            wait_for("the screen to go off", lambda: state()["state"] == "off", timeout=20)
            time.sleep(2)
            ha.call("pleatview", "show_dashboard", {"entity_id": entity["id"], "path": "/map"})

        cast_while_asleep()
        wait_for("the cast to wake the screen and show the dashboard",
                 lambda: state()["state"] == "idle", timeout=20)
        adb("shell", "locksettings", "set-pin", "1234")
        try:
            cast_while_asleep()
            time.sleep(10)
            if (shown := state())["state"] != "off":
                raise AssertionError(f"with a PIN, a cast showed {shown['attributes'].get('dashboard')} "
                                     "over the lock screen")
        finally:
            adb("shell", "locksettings", "clear", "--old", "1234", check=False)
            adb("shell", "input", "keyevent", "KEYCODE_WAKEUP")
            adb("shell", "wm", "dismiss-keyguard", check=False)
            ha.call("pleatview", "home", {"entity_id": entity["id"]})
            wait_for("PleatView after unlocking", lambda: state()["state"] == "idle", timeout=30)
        return "no lock: a cast woke the screen and showed the dashboard; with a PIN the lock screen stayed"

    def update_in_place():
        adb("install", "-r", "/tmp/pleatview.apk")
        adb("shell", "am", "start", "-n", f"{PACKAGE}/.MainActivity")
        if not r.device:
            adb("forward", f"tcp:{TV_PORT}", "tcp:8765")
        wait_for("the TV after the update", ready, timeout=180)

    def use_cases(shot) -> None:
        """Pictures of what people use PleatView for, for the website's use cases."""
        home = {"entity_id": entity["id"]}
        entries = {}
        for name in SCREENS_CAMERAS:
            _, flow = ha.api("POST", "/api/config/config_entries/flow", {"handler": "local_file"})
            # Home Assistant 2026.11 dropped the name from this form; the cards name the cameras.
            asks = {field["name"] for field in flow.get("data_schema", [])}
            data = {"file_path": f"/config/www/{camera_file(name)}", **({"name": name} if "name" in asks else {})}
            status, added = ha.api("POST", f"/api/config/config_entries/flow/{flow['flow_id']}", data)
            if not isinstance(added, dict) or added.get("type") != "create_entry":
                raise AssertionError(f"couldn't add the {name} camera: {status} {added}")
            entries[name] = added["result"]["entry_id"]

        def camera_entities():
            listed = ws_call(r, {"type": "config/entity_registry/list"}).get("result") or []
            found = {name: next((e["entity_id"] for e in listed if e.get("config_entry_id") == entry), None)
                     for name, entry in entries.items()}
            return found if all(found.values()) else None

        cameras = wait_for("the cameras", camera_entities, timeout=30)

        # Someone rings: the door camera in a corner and a card with its picture.
        ha.call("pleatview", "show_camera", {**home, "camera": cameras["Front door"], "mode": "pip",
                                             "position": "bottom-left", "size": 40, "duration": 60})
        ha.call("pleatview", "notify", {**home, "title": "Front door", "message": "Someone is at the door.",
                                        "camera": cameras["Front door"], "duration": 30})
        shot("usecase-doorbell", settle=5)
        ha.call("media_player", "media_stop", home)
        time.sleep(2)

        # The washing machine finishes during a film.
        ha.call("media_player", "play_media", {**home, "media_content_type": "video", "media_content_id": f"{r.site}/screens.mp4",
                                               "extra": {"title": "Caminandes: Gran Dillama", "start_position": 22}})
        wait_for("the film", lambda: state()["state"] == "playing", timeout=60)
        time.sleep(3)
        ha.call("pleatview", "notify", {**home, "title": "Laundry", "message": "The washing machine has finished.",
                                        "duration": 10})
        shot("usecase-laundry")
        ha.call("media_player", "media_stop", home)
        wait_for("the dashboard after the film", lambda: state()["state"] == "idle", timeout=20)

        # A morning board: the day, the weather, a family calendar and the shopping list.
        for item in ("Milk", "Bread", "Coffee", "Apples"):
            ha.call("todo", "add_item", {"entity_id": "todo.shopping_list", "item": item})
        _, flow = ha.api("POST", "/api/config/config_entries/flow", {"handler": "local_calendar"})
        field = next(f["name"] for f in flow.get("data_schema", []) if f.get("required"))
        status, added = ha.api("POST", f"/api/config/config_entries/flow/{flow['flow_id']}", {field: "Family"})
        if not isinstance(added, dict) or added.get("type") != "create_entry":
            raise AssertionError(f"couldn't add the family calendar: {status} {added}")
        entries["Family"] = added["result"]["entry_id"]
        family = wait_for("the family calendar", lambda: (found := camera_entities()) and found["Family"], timeout=30)
        today = datetime.now().date()
        for day, start, end, summary in ((0, "07:30", "08:00", "School run"), (0, "17:30", "18:30", "Football practice"),
                                         (1, "10:00", "10:30", "Dentist"), (1, "18:00", "18:15", "Bins out"),
                                         (2, "12:00", "13:00", "Lunch with Gran"), (4, "19:00", "21:00", "Movie night")):
            date = today + timedelta(days=day)
            ha.call("calendar", "create_event", {"entity_id": family, "summary": summary,
                                                 "start_date_time": f"{date} {start}:00",
                                                 "end_date_time": f"{date} {end}:00"})
        morning = {"views": [{"title": "Good morning", "type": "sections", "max_columns": 3, "sections": [
            {"type": "grid", "cards": [
                {"type": "markdown", "content": "# Good morning\n### {{ now().strftime('%A %-d %B') }}"},
                {"type": "weather-forecast", "entity": "weather.demo_weather_south", "name": "Today",
                 "forecast_type": "daily"}]},
            {"type": "grid", "cards": [
                {"type": "heading", "heading": "Calendar", "icon": "mdi:calendar"},
                {"type": "calendar", "entities": [family],
                 "initial_view": "listWeek", "grid_options": {"columns": 12, "rows": 6}}]},
            {"type": "grid", "cards": [
                {"type": "heading", "heading": "Shopping list", "icon": "mdi:cart"},
                {"type": "todo-list", "entity": "todo.shopping_list", "hide_create": True}]},
        ]}]}
        # Cameras around the house, on a screen of their own.
        wall = {"views": [{"title": "Cameras", "panel": True, "cards": [{"type": "grid", "columns": 2, "square": False, "cards": [
            {"type": "picture-entity", "entity": camera, "name": name, "camera_view": "auto", "show_state": False}
            for name, camera in cameras.items()]}]}]}
        boards = (("dashboard-morning", "Good morning", morning, "usecase-morning"),
                  ("dashboard-cameras", "Cameras", wall, "usecase-cameras"))
        created = []
        try:
            for path, title, config, _ in boards:
                created.append(ws_call(r, {"type": "lovelace/dashboards/create", "url_path": path, "title": title,
                                           "mode": "storage", "show_in_sidebar": True})["result"]["id"])
                ws_call(r, {"type": "lovelace/config/save", "url_path": path, "config": config})
            # The page loaded before these dashboards existed: a fresh one knows them.
            time.sleep(2)
            ha.call("pleatview", "reload", home)
            wait_for("the reloaded dashboard", lambda: state()["attributes"].get("ha_connected") is not False, timeout=60)
            time.sleep(5)
            for path, title, _, picture in boards:
                # Asked while the reload is still settling, the TV can end up back home: ask again.
                for attempt in range(3):
                    ha.call("pleatview", "show_dashboard", {**home, "path": f"/{path}"})
                    try:
                        wait_for(title, lambda p=path: str(state()["attributes"].get("dashboard", "")).startswith(f"/{p}"),
                                 timeout=15)
                        break
                    except TimeoutError:
                        if attempt == 2:
                            raise
                shot(picture, settle=10)
        finally:
            for dashboard_id in created:
                ws_call(r, {"type": "lovelace/dashboards/delete", "dashboard_id": dashboard_id})
            ha.call("pleatview", "home", home)

    def screens():
        """The website's and the stores' pictures: a tidy dashboard, then features over it."""
        folder = report.out / "screens"
        folder.mkdir(exist_ok=True)
        shutil.copy(report.out / "01-setup.png", folder / "setup.png")

        def shot(name: str, settle: float = 3) -> None:
            time.sleep(settle)
            png = subprocess.run([*ADB_COMMAND, "exec-out", "screencap", "-p"], capture_output=True, check=False).stdout
            (folder / f"{name}.png").write_bytes(png)

        # As the install guide recommends; the menu then has no warning about it.
        adb("shell", "appops", "set", PACKAGE, "SYSTEM_ALERT_WINDOW", "allow")
        _, states = ha.api("GET", "/api/states")
        ws_call(r, {"type": "lovelace/dashboards/create", "url_path": "dashboard-home", "title": "Our home",
                    "mode": "storage", "show_in_sidebar": True})
        ws_call(r, {"type": "lovelace/config/save", "url_path": "dashboard-home",
                    "config": showcase_dashboard({s["entity_id"] for s in states})})
        # The TV's home, zoomed out a little so a whole home fits on one screen.
        set_options(ha, r.entry_id, {"home_dashboard": "/dashboard-home", "zoom": 80})
        ha.call("pleatview", "home", {"entity_id": entity["id"]})
        wait_for("the showcase dashboard",
                 lambda: str(state()["attributes"].get("dashboard", "")).startswith("/dashboard-home"), timeout=30)
        shot("dashboard", settle=10)

        ha.call("pleatview", "notify", {"entity_id": entity["id"], "title": "Laundry",
                                        "message": "The washing machine has finished.", "duration": 8})
        shot("notification")
        time.sleep(8)  # until the card has gone

        # The menu, on a TV: a landscape phone or tablet emulator crashed (exit 139) right here.
        if r.args.form_factor == "tv":
            adb("shell", "input", "keyevent", "KEYCODE_BACK")
            shot("menu", settle=4)
            adb("shell", "input", "keyevent", "KEYCODE_BACK")

        if r.args.screens:
            ha.call("media_player", "play_media", {
                "entity_id": entity["id"], "media_content_type": "video", "media_content_id": f"{r.site}/screens.mp4",
                "extra": {"title": "Caminandes: Gran Dillama", "mode": "pip", "position": "bottom-right",
                          "size": 40, "start_position": 12}})
            wait_for("playback", lambda: state()["state"] == "playing", timeout=60)
            shot("picture-in-picture", settle=4)
            ha.call("media_player", "media_stop", {"entity_id": entity["id"]})
            wait_for("the dashboard after stop", lambda: state()["state"] == "idle", timeout=20)

        if r.args.screens:
            use_cases(shot)

        # The sign-in check saved a sign-in for the test site; without it the card shows.
        ha.call("pleatview", "forget_sign_ins", {"entity_id": entity["id"]})
        created = ws_call(r, {"type": "lovelace/dashboards/create", "url_path": "dashboard-energy",
                              "title": "Energy monitor", "mode": "storage", "show_in_sidebar": True})
        ws_call(r, {"type": "lovelace/config/save", "url_path": "dashboard-energy",
                    "config": {"strategy": {"type": "iframe", "url": f"{r.site}/prompt/"}}})
        time.sleep(2)
        try:
            ha.call("pleatview", "show_dashboard", {"entity_id": entity["id"], "path": "/dashboard-energy"})
            wait_for("the sign-in card",
                     lambda: state()["attributes"].get("sign_in_needed") == "Energy monitor", timeout=45)
            shot("sign-in")
            adb("shell", "input", "keyevent", "KEYCODE_BACK")
        finally:
            ha.call("pleatview", "home", {"entity_id": entity["id"]})
            ws_call(r, {"type": "lovelace/dashboards/delete", "dashboard_id": created["result"]["id"]})
        return ", ".join(sorted(p.stem for p in folder.glob("*.png")))

    report.check("Setup screen shows a PIN", setup_screen)
    if "pin" in entity:
        report.check("The demo shows a sample home and a tour, then ends", demo)
        report.check("Wrong PIN rejected, right PIN finishes setup", pairing)
    for name, fn in [
        ("Dashboard signs in automatically", signed_in),
        ("Repair for Home Assistant older than 2026.7", ha_version_repair),
        ("Battery sensors only where there's a battery", battery),
        ("Kiosk mode hides the sidebar and header", kiosk),
        ("Notification card appears", notification),
        ("Video plays and stops", video),
        ("Notification levels: a warning's tone, an alert pauses", levels),
        ("Camera without a stream shows as picture-in-picture", camera),
        ("show_dashboard and home switch dashboards", dashboards),
        ("Rotation keeps a dashboard Home Assistant showed", rotation_waits),
        ("Over another app: a card shows, then PleatView comes back", comes_back),
        ("Over another app: a camera and a video in picture-in-picture", pip_over_other_apps),
        ("Webpage dashboard opens the site directly", webpage),
        ("Page steps: clicks on text and CSS, a drag, a scroll", page_steps),
        ("Connects out to a controller elsewhere (protocol v2)", connects_out),
        ("Sign-ins: entered from a phone, then saved", sign_ins),
        ("turn_off and turn_on", standby),
        ("Recovers after a Home Assistant restart", ha_restart),
        ("Recovers from a frozen page", frozen_page),
        ("Update in place keeps the pairing", update_in_place),
        # After the update: it restarted the app, which forgets the test site's password
        # that the WebView kept for the session after the sign-in check.
        ("Back after a dismissed sign-in opens the menu", back_after_sign_in),
        ("Support PleatView in the menu", support_code),
        *([("Turning the screen keeps the page, and the sign-in card fits", turned),
           ("Touch opens the menu and wakes the screen", touch_menu),
           ("A PIN lock screen stays in front of a cast", lock_screen)] if r.args.form_factor != "tv" else []),
        *([("Website and store pictures", screens)] if r.args.screens is not None else []),
    ]:
        if r.args.only and r.args.only.lower() not in name.lower():
            continue
        if "id" in entity:
            report.check(name, fn)
        else:
            report.skip(name, "pairing failed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--ha-version", default="stable", help="throwaway Home Assistant image tag (default: stable)")
    parser.add_argument("--ha-url", help="use this existing Home Assistant instead of a throwaway one")
    parser.add_argument("--ha-token-file", help="file holding an admin long-lived token for --ha-url")
    parser.add_argument("--device", metavar="HOST[:PORT]", help="test on this TV over network adb, not the emulator")
    parser.add_argument("--screen-size", metavar="WxH@DPI", type=lambda v: tuple(v.split("@")),
                        help="a phone's or tablet's screen, for store pictures (Google Play wants 9:16 or 16:9)")
    parser.add_argument("--form-factor", choices=FORM_FACTORS, default="tv",
                        help="emulator to test on: tv (default), phone or tablet")
    parser.add_argument("--apk", help="APK to test (default: build a debug APK)")
    parser.add_argument("--keep", action="store_true", help="leave the containers running")
    parser.add_argument("--only", metavar="TEXT", help="after pairing, run only the checks whose name contains TEXT")
    parser.add_argument("--screens", nargs="?", const="", metavar="VIDEO",
                        help="after the checks, take the website and store pictures; VIDEO plays in picture-in-picture")
    args = parser.parse_args()
    if args.screens is not None and (args.device or args.ha_url):
        parser.error("--screens needs the emulator and a throwaway Home Assistant: it renames the TV and adds dashboards")
    if bool(args.ha_url) != bool(args.ha_token_file):
        parser.error("--ha-url and --ha-token-file go together")
    if args.device and args.form_factor != "tv":
        parser.error("--form-factor picks an emulator; --device tests the device it names")
    if args.device and not args.ha_url:
        parser.error("--device needs --ha-url: the TV must reach Home Assistant over the network")
    if args.device:
        if ":" not in args.device:
            args.device += ":5555"
        global ADB_COMMAND  # noqa: PLW0603 - one switch for every adb call
        ADB_COMMAND = ["docker", "exec", ADB_CONTAINER, "adb", "-s", args.device]

    r = Run(args)
    out = ROOT / "tools/e2e/out" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True)
    work = Path(tempfile.mkdtemp(prefix="pleatview-e2e-"))
    report = Report(out)
    try:
        apk = prepare(r)
        start(r, work, apk)
        version = http("GET", f"{r.tv_api}/api/info")[1].get("app_version")
        ha_version = r.ha.api("GET", "/api/config")[1].get("version")
        where = r.ha.url if r.external else "throwaway" if args.ha_version == ha_version else f"{args.ha_version}, throwaway"
        report.title = f"PleatView {version} on {describe_tv()} against Home Assistant {ha_version} ({where})"
        print(f"Running checks: {report.title}", flush=True)
        checks(r, report, apk)
    except Exception as err:  # noqa: BLE001
        report.results.append(("FAIL", "Environment", str(err)))
        print(f"  FAIL  Environment  ({err})")
    finally:
        # Everything PleatView logs, plus warnings and errors from the rest (the player, WebView).
        (out / "logcat.txt").write_text(adb("logcat", "-d", "PleatView:V", "*:W", check=False, timeout=60))
        if r.external and r.entry_id:
            # Removing the entry also deletes the user it created and resets the TV.
            r.ha.api("DELETE", f"/api/config/config_entries/entry/{r.entry_id}")
        if not r.external:
            logs = subprocess.run(["docker", "logs", HA_CONTAINER], capture_output=True, text=True, check=False)
            (out / "home-assistant.log").write_text(logs.stdout + logs.stderr)
        if r.media_server:
            r.media_server.shutdown()
        tv_container = ADB_CONTAINER if r.device else TV_CONTAINER
        if args.keep:
            print(f"Containers kept: {tv_container}" + ("" if r.external else f", {HA_CONTAINER} on {r.ha.url}"))
        else:
            run("docker", "rm", "-f", tv_container, *([] if r.external else [HA_CONTAINER]), check=False)
        if args.keep:
            print(f"Home Assistant's config and the test site's files stay in {work}")
        else:
            # Home Assistant writes its config as root; remove it from inside a container.
            run("docker", "run", "--rm", "-v", f"{work}:/work", r.emulator_image, "rm", "-rf", "/work/ha", "/work/media",
                check=False)
            shutil.rmtree(work, ignore_errors=True)

    lines = [f"{status}  {name}" + (f"  ({detail})" if detail else "") for status, name, detail in report.results]
    if report.title:
        lines[:0] = [report.title, ""]
    (out / "summary.txt").write_text("\n".join(lines) + "\n")
    print(f"\n{report.count('PASS')} passed, {report.count('FAIL')} failed, {report.count('SKIP')} skipped. "
          f"Artifacts: {out}")
    return 1 if report.count("FAIL") else 0


if __name__ == "__main__":
    sys.exit(main())
