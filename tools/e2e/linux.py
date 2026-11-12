#!/usr/bin/env python3
"""End-to-end test: PleatView for Linux against a real Home Assistant, all in Docker.

The app runs from this checkout in the tools/linux image, on a virtual screen (Xvfb), next
to a throwaway Home Assistant; both listen only on 127.0.0.1, and the app doesn't announce
itself on the network. The checks follow the Android ones in run.py where they apply.

    tools/e2e/linux.py                         # Home Assistant stable
    tools/e2e/linux.py --ha-version 2025.1.4   # any Home Assistant image tag
    tools/e2e/linux.py --keep                  # leave the containers running afterwards

Screenshots, logs and a summary go to tools/e2e/out/<time>-linux/. Exit status 0 means
every check passed.
"""

from __future__ import annotations

import argparse
import functools
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run as e2e  # noqa: E402 - the Android harness: Home Assistant, the test site, the report
from run import (  # noqa: E402
    HA_CONTAINER, HA_PORT, KIOSK_PROBE, MEDIA_PORT, ROOT, SITE_PASSWORD, SITE_USER, TEST_GIF, TEST_PAGE,
    TEST_VIDEO, HomeAssistant, QuietFiles, QuietServer, Report, SkipCheck, http, run, set_options, wait_for,
    ws_call,
)

APP_CONTAINER = "pleatview-e2e-linux"
APP_IMAGE = "pleatview-linux"
APP_PORT = 18776  # the app's control API (8765 elsewhere)
APP_DEVTOOLS = 19223  # Electron's DevTools, for reading and pressing keys on the screen
CACHE_VOLUME = "pleatview-e2e-linux-cache"  # npm's and Electron's downloads, kept between runs
HA_CONFIG = e2e.HA_CONFIG.replace(e2e.EMULATOR_HOST, "127.0.0.1")

# The app from this checkout: copied, its packages installed once per container (a restart
# keeps them, and the app's settings), then started on the virtual screen.
APP_START = """set -e
if [ ! -d /tmp/w/linux/node_modules ]; then
  mkdir -p /tmp/w/android/app/src/main
  cp -r /repo/android/app/src/main/assets /tmp/w/android/app/src/main/
  cp -r /repo/linux /tmp/w/
  cd /tmp/w/linux && npm ci --no-audit --no-fund >/tmp/npm.log 2>&1
fi
cd /tmp/w/linux
exec sh /repo/tools/linux/headless.sh --remote-debugging-port=%d
""" % APP_DEVTOOLS

# Runs in the Home Assistant image (Python with aiohttp): evaluates JavaScript in PleatView's
# own layer ("ui") or the dashboard ("dashboard"), or presses a key there ("key:Escape").
CDP_SCRIPT = """
import asyncio, json, sys
import aiohttp

async def main(port, which, expression):
    async with aiohttp.ClientSession() as s:
        pages = await (await s.get(f"http://127.0.0.1:{port}/json")).json()
        def wanted(p):
            ui = p["url"].endswith("/ui/index.html")
            return p["type"] == "page" and (ui if which == "ui" else not ui and not p["url"].startswith("devtools"))
        page = next(p for p in pages if wanted(p))
        async with s.ws_connect(page["webSocketDebuggerUrl"], max_msg_size=0) as ws:
            calls = []
            if expression.startswith("key:"):
                key = expression[4:]
                code = {"Escape": 27, "Enter": 13}.get(key, 0)
                down = {"type": "keyDown", "text": "\\r"} if key == "Enter" else {"type": "rawKeyDown"}
                for event in (down, {"type": "keyUp"}):
                    calls.append(("Input.dispatchKeyEvent", {**event, "key": key, "code": key,
                                                             "windowsVirtualKeyCode": code}))
            elif expression == "rightclick":
                for kind in ("mousePressed", "mouseReleased"):
                    calls.append(("Input.dispatchMouseEvent", {"type": kind, "x": 20, "y": 20, "button": "right",
                                                               "clickCount": 1}))
            else:
                calls.append(("Runtime.evaluate", {"expression": expression, "returnByValue": True}))
            for i, (method, params) in enumerate(calls, 1):
                await ws.send_json({"id": i, "method": method, "params": params})
                async for msg in ws:
                    data = json.loads(msg.data)
                    if data.get("id") == i:
                        result = data.get("result", {}).get("result", {}).get("value")
                        break
            print(json.dumps(None if expression.startswith("key:") or expression == "rightclick" else result))

asyncio.run(asyncio.wait_for(main(int(sys.argv[1]), sys.argv[2], sys.stdin.read()), float(sys.argv[3])))
"""


class LinuxRun:
    """What the shared helpers (ws_call, set_options) need, for the Linux app."""

    def __init__(self, args) -> None:
        self.args = args
        self.external = False
        self.ha = HomeAssistant(f"http://127.0.0.1:{HA_PORT}")
        self.tools_image = f"ghcr.io/home-assistant/home-assistant:{args.ha_version}"
        self.app_api = f"http://127.0.0.1:{APP_PORT}"
        self.site = f"http://127.0.0.1:{MEDIA_PORT}"
        self.page_url = f"{self.site}/e2e.html"
        self.video_url = f"http://127.0.0.1:{HA_PORT}/local/e2e.mp4"
        self.entry_id = ""
        self.media_server: QuietServer | None = None


class LinuxReport(Report):
    def screenshot(self, name: str) -> None:
        png = subprocess.run(["docker", "exec", APP_CONTAINER, "import", "-window", "root", "-display", ":99", "png:-"],
                             capture_output=True, check=False).stdout
        if png:
            (self.out / f"{name}.png").write_bytes(png)


def screen(r: LinuxRun, which: str, expression: str, timeout: float = 30):
    """Evaluates JavaScript in the app's layer or its dashboard (or presses a key: "key:Escape")."""
    output = run("docker", "run", "--rm", "-i", "--network", "host", "--entrypoint", "python3", r.tools_image,
                 "-c", CDP_SCRIPT, str(APP_DEVTOOLS), which, str(timeout - 5), input=expression, timeout=timeout)
    return json.loads(output)


def ui_text(r: LinuxRun) -> str:
    return screen(r, "ui", "document.getElementById('screen').innerText") or ""


def start(r: LinuxRun, work: Path) -> None:
    print("Starting Home Assistant and PleatView for Linux…", flush=True)
    run("docker", "rm", "-f", APP_CONTAINER, HA_CONTAINER, check=False)
    if not run("docker", "images", "-q", APP_IMAGE, check=False).strip():
        run("docker", "build", "-t", APP_IMAGE, str(ROOT / "tools/linux"), timeout=1800)
    media = work / "media"
    media.mkdir()
    run("docker", "run", "--rm", "--entrypoint", "sh", "-v", f"{media}:/out", r.tools_image, "-c",
        f"{TEST_VIDEO} /out/e2e.mp4 && {TEST_GIF} /out/e2e.gif && chmod a+r /out/e2e.mp4 /out/e2e.gif")
    (media / "e2e.html").write_text(TEST_PAGE)
    r.media_server = QuietServer(("127.0.0.1", MEDIA_PORT), functools.partial(QuietFiles, directory=str(media)))
    threading.Thread(target=r.media_server.serve_forever, daemon=True).start()
    (work / "ha").mkdir()
    (work / "ha/configuration.yaml").write_text(HA_CONFIG)
    run("docker", "run", "-d", "--name", HA_CONTAINER, "--network", "host", "-e", "TZ=UTC",
        "-v", f"{work / 'ha'}:/config", "-v", f"{media}:/config/www:ro",
        "-v", f"{ROOT / 'custom_components'}:/config/custom_components:ro", r.tools_image)
    # The app's downloads are kept in a volume, which its unprivileged user must own.
    run("docker", "run", "--rm", "--user", "root", "-v", f"{CACHE_VOLUME}:/cache", APP_IMAGE, "chown", "node:node", "/cache")
    run("docker", "run", "-d", "--name", APP_CONTAINER, "--network", "host",
        "-e", "HOME=/tmp", "-e", "PLEATVIEW_HOME=/tmp/pleatview", "-e", "PLEATVIEW_MDNS=0", "-e", f"PLEATVIEW_PORT={APP_PORT}",
        "-e", "npm_config_cache=/cache/npm", "-e", "electron_config_cache=/cache/electron",
        "-v", f"{ROOT}:/repo:ro", "-v", f"{CACHE_VOLUME}:/cache", APP_IMAGE, "sh", "-c", APP_START)
    wait_for("Home Assistant to start", lambda: http("GET", f"{r.ha.url}/api/onboarding")[0] == 200, timeout=420)
    r.ha.onboard()
    promoted = ws_call(r, {"type": "http/config/promote"}).get("success")
    print(f"  HTTP config promoted: {'yes' if promoted else 'not needed'}", flush=True)
    wait_for("the app's control API", lambda: http("GET", f"{r.app_api}/api/info")[0] == 200, timeout=600, interval=5)


def checks(r: LinuxRun, report: Report) -> None:
    ha = r.ha
    entity: dict = {}

    def pin() -> str:
        return (screen(r, "ui", "document.getElementById('setup-pin').textContent") or "").replace(" ", "")

    def state():
        return ha.state(entity["id"])

    def ready():
        current = state()
        return current["state"] == "idle" and current["attributes"].get("ha_connected") and current

    def setup_screen():
        entity["pin"] = wait_for("the PIN on the setup screen", lambda: len(p := pin()) == 6 and p, timeout=60)
        report.screenshot("01-setup")
        status, page = e2e.http_text(f"{r.app_api}/")
        if status != 200 or 'id="start"' not in page:
            raise AssertionError(f"the phone page doesn't offer adding the screen ({status})")
        if "Scan with a phone" not in ui_text(r):
            raise AssertionError("the setup screen has no code for phones")

    def pairing():
        _, flow = ha.api("POST", "/api/config/config_entries/flow", {"handler": "pleatview"})
        flow_path = f"/api/config/config_entries/flow/{flow['flow_id']}"
        try:
            # The address as the screen shows it, with its port.
            _, result = ha.api("POST", flow_path, {"host": f"127.0.0.1:{APP_PORT}", "port": 8765})
            if result.get("step_id") == "approve":
                # Home Assistant asks the screen first (one-press pairing). Escape refuses, and
                # the setup falls back to the PIN.
                wait_for("the pairing question on the screen", lambda: "Pair with Home Assistant?" in ui_text(r), timeout=30)
                report.screenshot("01-approve")
                screen(r, "ui", "key:Escape")
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
            ha.api("DELETE", flow_path)
            raise
        r.entry_id = result["result"]["entry_id"]

        def own_entities():
            listed = ws_call(r, {"type": "config/entity_registry/list"}).get("result") or []
            found = {e["entity_id"].split(".")[0]: e["entity_id"] for e in listed if e.get("config_entry_id") == r.entry_id}
            return found if "media_player" in found else None

        found = wait_for("the screen's entities in Home Assistant", own_entities, timeout=30)
        entity["id"] = found["media_player"]
        entity["notify"] = found.get("notify", entity["id"].replace("media_player.", "notify."))
        return entity["id"]

    def signed_in():
        current = wait_for("the dashboard to sign in", ready, timeout=120)
        time.sleep(5)
        report.screenshot("02-dashboard")
        if current["attributes"].get("auth_error"):
            raise AssertionError("the screen reports an authentication error")
        return f"{current['attributes'].get('url')}"

    def kiosk():
        seen = {}

        def applied():
            seen.update(screen(r, "dashboard", KIOSK_PROBE) or {})
            return seen.get("panelLeft") == 0 and seen.get("header") == "none"

        try:
            wait_for("kiosk styles", applied, timeout=45)
        except TimeoutError as err:
            raise AssertionError(f"{err}; last seen {seen}") from None
        return f"panel at x={seen['panelLeft']}, header {seen['header']}"

    def notification():
        ha.call("notify", "send_message", {"entity_id": entity["notify"], "title": "E2E",
                                           "message": "Hello from the end-to-end test"})
        wait_for("the notification card", lambda: "Hello from the end-to-end test" in ui_text(r), timeout=20, interval=1)
        report.screenshot("03-notification")

    def video():
        ha.call("media_player", "play_media", {"entity_id": entity["id"], "media_content_type": "video",
                                               "media_content_id": r.video_url, "extra": {"title": "E2E clip"}})
        playing = wait_for("playback", lambda: (s := state())["state"] == "playing" and s, timeout=45)
        moving = wait_for("the video to move", lambda: (screen(r, "ui", "document.getElementById('video').currentTime") or 0) > 1,
                          timeout=30, interval=1)
        report.screenshot("04-video")
        ha.call("media_player", "media_stop", {"entity_id": entity["id"]})
        wait_for("the dashboard after stop", lambda: state()["state"] == "idle", timeout=20)
        return f"title {playing['attributes'].get('media_title')!r}, playing: {moving}"

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
        wait_for("the map dashboard", lambda: str(state()["attributes"].get("dashboard", "")).startswith("/map"), timeout=30)
        time.sleep(3)
        report.screenshot("06-map")
        ha.call("pleatview", "home", {"entity_id": entity["id"]})
        wait_for("the home dashboard", lambda: not str(state()["attributes"].get("dashboard", "")).startswith("/map"),
                 timeout=30)
        return f"{home} → /map → home"

    def webpage():
        created = ws_call(r, {"type": "lovelace/dashboards/create", "url_path": "dashboard-pleatview-e2e",
                              "title": "E2E web page", "mode": "storage", "show_in_sidebar": True})
        if not created.get("success"):
            raise AssertionError(f"couldn't create a Webpage dashboard: {created}")
        try:
            ws_call(r, {"type": "lovelace/config/save", "url_path": "dashboard-pleatview-e2e",
                        "config": {"strategy": {"type": "iframe", "url": r.page_url}}})
            wait_for("the new dashboard among the screen's sources",
                     lambda: "E2E web page" in (state()["attributes"].get("source_list") or []), timeout=20)
            ha.call("pleatview", "show_dashboard", {"entity_id": entity["id"], "path": "/dashboard-pleatview-e2e"})
            wait_for("the web page on the screen", lambda: state()["attributes"].get("url") == r.page_url, timeout=30)
            time.sleep(2)
            report.screenshot("07-webpage")
        finally:
            ws_call(r, {"type": "lovelace/dashboards/delete", "dashboard_id": created["result"]["id"]})
            ha.call("pleatview", "home", {"entity_id": entity["id"]})
        return f"{r.page_url} opened directly"

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
            time.sleep(2)

        def sign_in_from_a_phone(confirmation: str, shot: str) -> None:
            wait_for("the screen to ask for a sign-in",
                     lambda: state()["attributes"].get("sign_in_needed") == "E2E sign-in", timeout=45)
            if "Sign in to E2E sign-in" not in ui_text(r):
                raise AssertionError("the sign-in panel isn't on the screen")
            report.screenshot(shot)
            # What a phone does after scanning the panel's code: the PIN comes with it.
            status, reply = http("POST", f"{r.app_api}/api/signin",
                                 {"pin": pin(), "username": SITE_USER, "password": SITE_PASSWORD, "remember": True})
            if status != 200:
                raise AssertionError(f"signing in from the phone page failed: {status} {reply}")
            wait_for(f"the test site to see '{confirmation}'", lambda: confirmation in site.events, timeout=45)
            wait_for("the panel to close", lambda: state()["attributes"].get("sign_in_needed") is None, timeout=20)

        def saved(count: int) -> None:
            # Saved once the site accepts it, a moment after the site does: the menu counts them.
            # (Keys sent over DevTools reach pages but not the app, which opens the menu on
            # Escape; a right-click opens it too, and a click beside it closes it.)
            def counted():
                screen(r, "dashboard", "rightclick")
                text = ui_text(r)
                if "Home dashboard" in text:
                    screen(r, "ui", "document.getElementById('menu').click()")
                return f"Forget saved sign-ins ({count})" in text
            wait_for(f"the screen to save sign-in {count}", counted, timeout=30, interval=1)

        def signs_in_by_itself(confirmation: str) -> None:
            deadline = time.monotonic() + 45
            while confirmation not in site.events:
                if state()["attributes"].get("sign_in_needed"):
                    raise AssertionError(f"the screen asked again instead of using the saved sign-in ('{confirmation}')")
                if time.monotonic() > deadline:
                    raise TimeoutError(f"timed out waiting for the test site to see '{confirmation}'")
                time.sleep(1)

        try:
            # A password prompt. After a restart nothing is left in memory: the saved sign-in answers.
            use(f"{r.site}/prompt/")
            ha.call("pleatview", "show_dashboard", show)
            sign_in_from_a_phone("prompt ok", "08-sign-in-prompt")
            saved(1)
            site.events.clear()
            run("docker", "restart", APP_CONTAINER)
            wait_for("the app after a restart", lambda: http("GET", f"{r.app_api}/api/info")[0] == 200, timeout=120)
            wait_for("the screen after a restart", ready, timeout=120)
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

    def rotation_waits():
        set_options(ha, r.entry_id, {"dashboards": "/home/overview", "rotate_seconds": 10})
        try:
            time.sleep(8)
            wait_for("the screen after its new settings", ready, timeout=60)
            ha.call("pleatview", "show_dashboard", {"entity_id": entity["id"], "path": "/map", "duration": 25})
            wait_for("the map", lambda: str(state()["attributes"].get("dashboard")).startswith("/map"), timeout=30, interval=1)
            for _ in range(18):
                time.sleep(1)
                if not str(state()["attributes"].get("dashboard")).startswith("/map"):
                    raise AssertionError("rotation replaced the dashboard before its 25 seconds were up")
            wait_for("the dashboard to go back", lambda: not str(state()["attributes"].get("dashboard")).startswith("/map"),
                     timeout=30, interval=1)
            return "kept for its 25 seconds despite rotation every 10, then went back"
        finally:
            set_options(ha, r.entry_id, {"dashboards": "", "rotate_seconds": 0})
            time.sleep(8)
            wait_for("the screen after its new settings", ready, timeout=60)

    def standby():
        ha.call("media_player", "turn_off", {"entity_id": entity["id"]})
        wait_for("standby", lambda: state()["state"] == "off", timeout=20)
        ha.call("media_player", "turn_on", {"entity_id": entity["id"]})
        wait_for("wake", lambda: state()["state"] == "idle", timeout=20)

    def menu():
        screen(r, "dashboard", "rightclick")
        try:
            text = wait_for("the menu", lambda: "Home dashboard" in (t := ui_text(r)) and t, timeout=10, interval=1)
            report.screenshot("10-menu")
        finally:
            screen(r, "ui", "document.getElementById('menu').click()")
        if "Reload" not in text or "PIN" not in text:
            raise AssertionError(f"the menu lacks its items: {text[:200]!r}")
        return "opened with a right-click (a long press on a touch screen)"

    def ha_restart():
        run("docker", "restart", HA_CONTAINER)
        wait_for("Home Assistant to restart", lambda: http("GET", f"{ha.url}/api/")[0] in (200, 401), timeout=300)
        wait_for("the screen to reconnect", ready, timeout=180)

    def one_press():
        # Removed from Home Assistant, the screen goes back to its setup screen; added again,
        # one press of Enter on the screen pairs it, without the PIN.
        ha.api("DELETE", f"/api/config/config_entries/entry/{r.entry_id}")
        r.entry_id = ""
        wait_for("the setup screen again", lambda: "When Home Assistant asks for a PIN" in ui_text(r), timeout=60)
        _, flow = ha.api("POST", "/api/config/config_entries/flow", {"handler": "pleatview"})
        flow_path = f"/api/config/config_entries/flow/{flow['flow_id']}"
        try:
            _, result = ha.api("POST", flow_path, {"host": "127.0.0.1", "port": APP_PORT})
            if result.get("step_id") != "approve":
                raise AssertionError(f"Home Assistant didn't ask the screen first: {result}")
            wait_for("the pairing question on the screen", lambda: "Pair with Home Assistant?" in ui_text(r), timeout=30)
            screen(r, "ui", "key:Enter")
            result = wait_for("the entry after Enter",
                              lambda: (step := ha.api("GET", flow_path)[1]).get("type") == "create_entry" and step,
                              timeout=30)
        except Exception:
            ha.api("DELETE", flow_path)
            raise
        r.entry_id = result["result"]["entry_id"]
        listed = wait_for("the screen's entities", lambda: [e["entity_id"] for e in (ws_call(
            r, {"type": "config/entity_registry/list"}).get("result") or []) if e.get("config_entry_id") == r.entry_id
            and e["entity_id"].startswith("media_player.")], timeout=30)
        entity["id"] = listed[0]
        wait_for("the dashboard to sign in", ready, timeout=120)
        return "paired with one press of Enter, then signed in"

    report.check("Setup screen shows a PIN and a code for phones", setup_screen)
    report.check("Pairing with the PIN (a wrong one is refused)", pairing)
    if not entity.get("id"):
        report.skip("The rest", "not paired")
        return
    for name, fn in (
        ("Dashboard signed in, without a login page", signed_in),
        ("Kiosk mode hides the sidebar and header", kiosk),
        ("Notification card", notification),
        ("Video plays and stops", video),
        ("Camera in picture-in-picture", camera),
        ("show_dashboard and home", dashboards),
        ("Webpage dashboard opens the site directly", webpage),
        ("Sign-ins: password prompt and login form", sign_ins),
        ("Rotation waits for a dashboard's duration", rotation_waits),
        ("Menu: address, PIN and dashboards", menu),
        ("Standby and wake", standby),
        ("Reconnects after Home Assistant restarts", ha_restart),
        ("One-press pairing: Enter on the screen instead of the PIN", one_press),
    ):
        report.check(name, fn)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--ha-version", default="stable", help="throwaway Home Assistant image tag (default: stable)")
    parser.add_argument("--keep", action="store_true", help="leave the containers running")
    args = parser.parse_args()
    r = LinuxRun(args)
    out = ROOT / "tools/e2e/out" / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-linux")
    out.mkdir(parents=True)
    work = Path(tempfile.mkdtemp(prefix="pleatview-e2e-linux-"))
    report = LinuxReport(out)
    try:
        start(r, work)
        version = http("GET", f"{r.app_api}/api/info")[1].get("app_version")
        ha_version = r.ha.api("GET", "/api/config")[1].get("version")
        report.title = f"PleatView for Linux {version} against Home Assistant {ha_version} (throwaway)"
        print(f"Running checks: {report.title}", flush=True)
        checks(r, report)
    except Exception as err:  # noqa: BLE001
        report.results.append(("FAIL", "Environment", str(err)))
        print(f"  FAIL  Environment  ({err})")
    finally:
        for name, container in (("linux-app.log", APP_CONTAINER), ("home-assistant.log", HA_CONTAINER)):
            logs = subprocess.run(["docker", "logs", container], capture_output=True, text=True, check=False)
            (out / name).write_text(logs.stdout + logs.stderr)
        if r.media_server:
            r.media_server.shutdown()
        if args.keep:
            print(f"Containers kept: {APP_CONTAINER}, {HA_CONTAINER} on {r.ha.url}; files in {work}")
        else:
            run("docker", "rm", "-f", APP_CONTAINER, HA_CONTAINER, check=False)
            # Home Assistant writes its config as root; remove it from inside a container.
            run("docker", "run", "--rm", "-v", f"{work}:/work", "--entrypoint", "rm", r.tools_image,
                "-rf", "/work/ha", "/work/media", check=False)
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
