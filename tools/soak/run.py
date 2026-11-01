#!/usr/bin/env python3
"""Soak test: keeps PleatView busy on a real TV for hours and records what happens.

Every minute it samples the app's memory, the WebView renderer, threads, file descriptors,
CPU, the TV's free memory and the Home Assistant entity; every five minutes the dashboard's
JavaScript heap; every hour a screenshot. Meanwhile the TV rotates through dashboards every
minute (one with a live camera card) and the runner acts like a busy household: lights
change, notifications arrive, a video plays, a camera pops up, and a Webpage dashboard and a
password prompt open. Crashes, ANRs and renderer deaths come from logcat.

  setsid nohup tools/soak/run.py --device 192.168.88.68 --ha-url http://127.0.0.1:8123 \\
      --ha-token-file ~/.config/pleatview/ha-test-token \\
      --entity media_player.living_room_tv --out tools/soak/out/living-room >/dev/null 2>&1 &
  tools/soak/run.py --report tools/soak/out/living-room    # the summary so far
  tools/soak/run.py --restore tools/soak/out/living-room   # stop, and undo the setup

It runs its own adb server (container pleatview-soak-adb, port 5039), so end-to-end runs
can come and go meanwhile. The setup (rotation options, a "Soak" dashboard) is recorded in
setup.json and undone by --restore.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import signal
import socket
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "e2e"))
import run as e2e  # noqa: E402  (the websocket and DevTools scripts)

PACKAGE = e2e.PACKAGE
ADB_CONTAINER = "pleatview-soak-adb"
ADB_SERVER_PORT = 5039
TOOLS_IMAGE = "ghcr.io/home-assistant/home-assistant:stable"
SOAK_DASHBOARD = "dashboard-soak"
SAMPLE_SECONDS = 60
TICK_SECONDS = 5
PAGE_ANSWER_SECONDS = 30  # a page that takes longer to answer DevTools counts as frozen

FIELDS = [
    "time", "elapsed_h", "activity", "pid", "pss_kb", "swap_pss_kb", "java_heap_kb", "native_heap_kb",
    "graphics_kb", "code_kb", "private_other_kb", "system_kb", "views", "view_roots", "app_contexts",
    "activities", "webviews", "threads", "fds", "cpu_pct", "renderers", "renderer_pid", "renderer_pss_kb",
    "renderer_cpu_pct", "js_heap_kb", "js_page", "mem_available_kb", "foreground", "wakefulness", "state",
    "dashboard", "ha_connected", "sign_in", "error",
]
SUMMARY = {
    "Java Heap": "java_heap_kb", "Native Heap": "native_heap_kb", "Code": "code_kb",
    "Graphics": "graphics_kb", "Private Other": "private_other_kb", "System": "system_kb",
}
OBJECTS = {
    "Views": "views", "ViewRootImpl": "view_roots", "AppContexts": "app_contexts",
    "Activities": "activities", "WebViews": "webviews",
}
# One adb round trip per sample. $P is PleatView, $R the first WebView renderer.
PROBE = f"""
P=$(pidof {PACKAGE})
echo "@@pid $P"
echo "@@threads $(grep Threads /proc/$P/status 2>/dev/null)"
echo "@@ticks $(cut -d' ' -f14,15 /proc/$P/stat 2>/dev/null)"
echo "@@fds $(run-as {PACKAGE} ls /proc/$P/fd 2>/dev/null | wc -l)"
set -- $(ps -A -o PID,NAME | grep -c sandboxed_process) ; echo "@@renderers $1"
set -- $(ps -A -o PID,NAME | grep sandboxed_process | head -1) ; R=$1
echo "@@renderer $R $(cut -d' ' -f14,15 /proc/$R/stat 2>/dev/null)"
[ -n "$R" ] && echo "@@renderer_total $(dumpsys meminfo $R 2>/dev/null | grep -E '^ *TOTAL( PSS)?:')"
echo "@@available $(grep MemAvailable /proc/meminfo)"
echo "@@resumed $(dumpsys activity activities | grep -m1 ResumedActivity)"
echo "@@wakefulness $(dumpsys power | grep -m1 mWakefulness=)"
echo "@@meminfo"
dumpsys meminfo {PACKAGE}
"""
JS_HEAP = ("(() => ({kb: performance.memory ? Math.round(performance.memory.usedJSHeapSize / 1024) : null,"
           " page: location.origin + location.pathname}))()")
EVENT_PATTERN = re.compile(
    r"FATAL EXCEPTION|Fatal signal|ANR in|lowmemorykiller|has died|onRenderProcessGone|OutOfMemoryError"
    r"|Renderer process|am_crash|am_anr|am_proc_died|am_kill|Force finishing|Process com\.hustlesman"
)
SERIOUS = re.compile(r"FATAL EXCEPTION|Fatal signal|ANR in|OutOfMemoryError|onRenderProcessGone|am_crash|am_anr")


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def number(text: str | None) -> int | None:
    match = re.search(r"\d+", text or "")
    return int(match.group()) if match else None


class Adb:
    def __init__(self, device: str) -> None:
        self.device = device if ":" in device else f"{device}:5555"

    def __call__(self, *args: str, timeout: float = 45, binary: bool = False):
        result = subprocess.run(["docker", "exec", ADB_CONTAINER, "adb", "-s", self.device, *args],
                                capture_output=True, timeout=timeout, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"adb {args[0]} failed: {result.stderr.decode(errors='replace').strip()[:200]}")
        return result.stdout if binary else result.stdout.decode(errors="replace").replace("\r", "")

    def connect(self) -> bool:
        subprocess.run(["docker", "exec", ADB_CONTAINER, "adb", "connect", self.device],
                       capture_output=True, timeout=30, check=False)
        state = subprocess.run(["docker", "exec", ADB_CONTAINER, "adb", "-s", self.device, "get-state"],
                               capture_output=True, text=True, timeout=30, check=False)
        return state.stdout.strip() == "device"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def live_soak(out: Path) -> Path | None:
    """Another soak, of another TV, whose runner is still going."""
    for pid_file in out.parent.glob("*/runner.pid"):
        if pid_file.parent == out:
            continue
        try:
            command = Path(f"/proc/{int(pid_file.read_text())}/cmdline").read_bytes()
        except (OSError, ValueError):
            continue
        if b"soak/run.py" in command:
            return pid_file.parent
    return None


def start_adb_server() -> None:
    running = e2e.run("docker", "inspect", "-f", "{{.State.Running}}", ADB_CONTAINER, check=False).strip()
    if running == "true":
        return
    e2e.run("docker", "rm", "-f", ADB_CONTAINER, check=False)
    e2e.ADB_KEYS.mkdir(parents=True, exist_ok=True)
    e2e.run("docker", "run", "-d", "--name", ADB_CONTAINER, "--network", "host", "--restart", "unless-stopped",
            "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/keys",
            "-e", f"ANDROID_ADB_SERVER_PORT={ADB_SERVER_PORT}", "-v", f"{e2e.ADB_KEYS}:/keys",
            e2e.TV_IMAGE, "sleep", "infinity")


class HomeAssistant:
    def __init__(self, url: str, token: str) -> None:
        self.url = url.rstrip("/")
        self.token = token

    def request(self, method: str, path: str, body: dict | None = None, timeout: float = 30):
        request = urllib.request.Request(
            self.url + path, method=method, data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                text = response.read().decode()
                return response.status, json.loads(text) if text else None
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode(errors="replace")[:300]

    def state(self, entity: str) -> dict:
        status, data = self.request("GET", f"/api/states/{entity}")
        return data if status == 200 else {}

    def service(self, domain: str, service: str, data: dict) -> int:
        return self.request("POST", f"/api/services/{domain}/{service}", data, timeout=60)[0]

    def ws(self, message: dict) -> dict:
        output = e2e.run("docker", "run", "--rm", "-i", "--network", "host", "--entrypoint", "python3",
                         TOOLS_IMAGE, "-c", e2e.WS_SCRIPT, self.url,
                         input=f"{self.token}\n{json.dumps(message)}", check=False, timeout=120)
        return json.loads(output) if output.strip() else {}

    def options_form(self, entry_id: str) -> tuple[str, dict]:
        status, flow = self.request("POST", "/api/config/config_entries/options/flow", {"handler": entry_id})
        if status != 200 or flow.get("type") != "form":
            raise RuntimeError(f"couldn't open the options of {entry_id}: {status} {flow}")
        values = {}
        for field in flow["data_schema"]:
            if "default" in field:
                values[field["name"]] = field["default"]
            elif (field.get("description") or {}).get("suggested_value") is not None:
                values[field["name"]] = field["description"]["suggested_value"]
        return flow["flow_id"], values

    def set_options(self, entry_id: str, changes: dict) -> dict:
        flow_id, values = self.options_form(entry_id)
        status, result = self.request("POST", f"/api/config/config_entries/options/flow/{flow_id}",
                                      {**values, **changes})
        if status != 200 or result.get("type") != "create_entry":
            raise RuntimeError(f"couldn't save the options: {status} {result}")
        return values


class LogWatcher(threading.Thread):
    """Follows logcat and keeps the lines that matter: crashes, ANRs, kills, renderer deaths
    and PleatView's own errors (each distinct one written 20 times at most)."""

    def __init__(self, soak: Soak) -> None:
        super().__init__(daemon=True)
        self.soak = soak
        self.seen: dict[str, int] = {}
        self.serious = 0

    def run(self) -> None:
        while not self.soak.stopping.is_set():
            try:
                process = subprocess.Popen(
                    ["docker", "exec", ADB_CONTAINER, "adb", "-s", self.soak.adb.device, "logcat", "-v", "threadtime",
                     "-b", "main", "-b", "system", "-b", "crash", "-b", "events", "-T", "1"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, errors="replace")
                for line in process.stdout:
                    self.consider(line.rstrip())
                    if self.soak.stopping.is_set():
                        process.kill()
                        return
            except Exception as error:  # noqa: BLE001  (keep watching whatever happens)
                self.soak.note(f"logcat watcher: {error}")
            self.soak.stopping.wait(15)

    def consider(self, line: str) -> None:
        fields = line.split(None, 5)
        pid = fields[2] if len(fields) > 4 else ""
        level = fields[4] if len(fields) > 4 else ""
        ours = pid and pid == self.soak.pid and level in ("E", "F")
        if not (ours or EVENT_PATTERN.search(line)):
            return
        if "lowmemorykiller" in line and PACKAGE not in line and "sandboxed" not in line:
            key = "lowmemorykiller (other apps)"
        else:
            key = re.sub(r"\d+", "#", (fields[5] if len(fields) > 5 else line))[:80]
        count = self.seen[key] = self.seen.get(key, 0) + 1
        if SERIOUS.search(line) and (ours or PACKAGE in line or "sandboxed_process" in line):
            self.serious += 1
            self.soak.write("serious.log", f"{now()}  {line}")
        if count <= 20:
            self.soak.write("events.log", f"{now()}  {line}")
        elif count in (50, 100, 500, 1000, 5000):
            self.soak.write("events.log", f"{now()}  (seen {count} times) {key}")


class Soak:
    def __init__(self, args: argparse.Namespace, out: Path) -> None:
        self.args = args
        self.out = out
        self.adb = Adb(args.device)
        self.ha = HomeAssistant(args.ha_url, Path(args.ha_token_file).expanduser().read_text().strip())
        self.stopping = threading.Event()
        self.lock = threading.Lock()
        self.started = time.time()
        self.pid = ""
        self.ticks: dict[str, tuple[int, float]] = {}
        self.activity = ("idle", 0.0)
        self.counts: dict[str, list[int]] = {}
        self.notify_turn = 0
        self.gif_url = None
        self.watcher = LogWatcher(self)
        self.devtools_port = free_port()  # its own, so two TVs can soak at once
        self.page_silent = False

    # -- files ---------------------------------------------------------------------------
    def write(self, name: str, line: str) -> None:
        with self.lock, open(self.out / name, "a") as file:
            file.write(line + "\n")

    def note(self, message: str) -> None:
        self.write("runner.log", f"{now()}  {message}")

    def issue(self, message: str) -> None:
        self.write("issues.log", f"{now()}  {message}")

    # -- setup ---------------------------------------------------------------------------
    def setup(self) -> None:
        start_adb_server()
        if not e2e.wait_for(f"adb access to {self.adb.device}", self.adb.connect, timeout=120, interval=5):
            raise RuntimeError(f"no adb access to {self.adb.device}")
        setup_file = self.out / "setup.json"
        setup = json.loads(setup_file.read_text()) if setup_file.exists() else {}
        entity = self.ha.ws({"type": "config/entity_registry/get", "entity_id": self.args.entity})
        entry_id = (entity.get("result") or {}).get("config_entry_id")
        if not entry_id:
            raise RuntimeError(f"{self.args.entity} isn't a PleatView entity: {entity}")
        setup["entry_id"] = entry_id
        if self.args.camera and "created_dashboard" not in setup:
            setup["created_dashboard"] = self.create_dashboard()
        if self.args.rotate and "original_options" not in setup:
            dashboards = "\n".join(["/home/overview", *([f"/{SOAK_DASHBOARD}"] if self.args.camera else []), "/map"])
            setup["original_options"] = self.ha.set_options(
                entry_id, {"dashboards": dashboards, "rotate_seconds": self.args.rotate})
        setup_file.write_text(json.dumps(setup, indent=2))
        if self.args.gif:
            signed = self.ha.ws({"type": "auth/sign_path", "path": self.args.gif, "expires": 30 * 86400})
            path = (signed.get("result") or {}).get("path")
            if path:
                path, _, query = path.partition("?")
                path = urllib.parse.quote(urllib.parse.unquote(path), safe="/")
                self.gif_url = f"{self.tv_ha_url()}{path}?{query}"
        self.note(f"setup done: {json.dumps({k: v for k, v in setup.items() if k != 'original_options'})}")

    def create_dashboard(self) -> bool:
        listed = self.ha.ws({"type": "lovelace/dashboards/list"}).get("result") or []
        if any(d.get("url_path") == SOAK_DASHBOARD for d in listed):
            return False
        self.ha.ws({"type": "lovelace/dashboards/create", "url_path": SOAK_DASHBOARD, "title": "Soak",
                    "icon": "mdi:timer-sand", "show_in_sidebar": False, "require_admin": False, "mode": "storage"})
        lights = [light for light in self.args.lights.split(",") if light]
        sensors = [s["entity_id"] for s in self.ha.request("GET", "/api/states")[1]
                   if s["entity_id"].startswith("sensor.") and number(s["state"]) is not None][:6]
        cards = [
            {"type": "picture-entity", "entity": self.args.camera, "camera_view": "live"},
            {"type": "glance", "entities": lights + sensors[:2]},
            {"type": "history-graph", "hours_to_show": 6, "entities": sensors[:3]},
            {"type": "entities", "entities": sensors + lights},
        ]
        self.ha.ws({"type": "lovelace/config/save", "url_path": SOAK_DASHBOARD,
                    "config": {"title": "Soak", "views": [{"title": "Soak", "cards": cards}]}})
        return True

    def tv_ha_url(self) -> str:
        """Home Assistant's address as the TV knows it."""
        attributes = self.ha.state(self.args.entity).get("attributes", {})
        url = attributes.get("url") or ""
        if attributes.get("dashboard") and url.startswith("http"):
            parts = urllib.parse.urlsplit(url)
            return f"{parts.scheme}://{parts.netloc}"
        return self.ha.url

    # -- sampling ------------------------------------------------------------------------
    def cpu(self, key: str, pid: str, ticks: str) -> float | None:
        values = [int(v) for v in ticks.split()[:2] if v.isdigit()]
        if len(values) != 2:
            return None
        total, when = sum(values), time.time()
        previous = self.ticks.get(key)
        self.ticks[key] = (int(pid or 0), total, when)
        if not previous or previous[0] != int(pid or 0):
            return None
        return round((total - previous[1]) / ((when - previous[2]) * 100) * 100, 1)

    def sample(self, js: bool) -> dict:
        row = {"time": now(), "elapsed_h": round((time.time() - self.started) / 3600, 3)}
        activity, until = self.activity
        row["activity"] = activity if time.time() < until else "idle"
        output = self.adb("shell", PROBE, timeout=60)
        lines = {line.split(" ", 1)[0]: (line.split(" ", 1) + [""])[1] for line in output.splitlines()
                 if line.startswith("@@")}
        pid = lines.get("@@pid", "").strip()
        if pid != self.pid:
            if self.pid:
                self.issue(f"PleatView's process changed: {self.pid or 'none'} → {pid or 'none'}")
            self.pid = pid
        row["pid"] = pid
        row["threads"] = number(lines.get("@@threads"))
        row["cpu_pct"] = self.cpu("app", pid, lines.get("@@ticks", ""))
        row["fds"] = number(lines.get("@@fds"))
        row["renderers"] = number(lines.get("@@renderers"))
        renderer = lines.get("@@renderer", "").split(None, 1)
        if renderer:
            row["renderer_pid"] = renderer[0]
            row["renderer_cpu_pct"] = self.cpu("renderer", renderer[0], renderer[1] if len(renderer) > 1 else "")
        row["renderer_pss_kb"] = number(lines.get("@@renderer_total"))
        row["mem_available_kb"] = number(lines.get("@@available"))
        row["foreground"] = PACKAGE in lines.get("@@resumed", "")
        row["wakefulness"] = lines.get("@@wakefulness", "").split("=")[-1].strip()
        meminfo = output.split("@@meminfo", 1)[-1]
        summary = meminfo.split("App Summary", 1)[-1]
        for label, key in SUMMARY.items():
            match = re.search(rf"^\s*{label}:\s+(\d+)", summary, re.M)
            row[key] = int(match.group(1)) if match else None
        match = re.search(r"^\s*TOTAL(?: PSS)?:\s+(\d+)", summary, re.M)
        row["pss_kb"] = int(match.group(1)) if match else None
        match = re.search(r"TOTAL SWAP(?: PSS)?:\s+(\d+)", summary)
        row["swap_pss_kb"] = int(match.group(1)) if match else None
        objects = dict(re.findall(r"([A-Za-z][A-Za-z ]*?):\s+(\d+)", meminfo.split(" Objects", 1)[-1].split(" SQL", 1)[0]))
        for label, key in OBJECTS.items():
            row[key] = int(objects[label]) if label in objects else None
        state = self.ha.state(self.args.entity)
        attributes = state.get("attributes", {})
        row["state"] = state.get("state")
        row["dashboard"] = attributes.get("dashboard") or attributes.get("url")
        row["ha_connected"] = attributes.get("ha_connected")
        row["sign_in"] = attributes.get("sign_in_needed")
        if js and pid:
            try:
                self.adb("forward", f"tcp:{self.devtools_port}", f"localabstract:webview_devtools_remote_{pid}")
                started = time.monotonic()
                output = e2e.run("docker", "run", "--rm", "-i", "--network", "host", "--entrypoint", "python3",
                                 TOOLS_IMAGE, "-c", e2e.CDP_SCRIPT, str(self.devtools_port), str(PAGE_ANSWER_SECONDS),
                                 input=JS_HEAP, timeout=PAGE_ANSWER_SECONDS + 15, check=False)
                value = json.loads(output) if output.strip() else None
                if isinstance(value, dict):
                    row["js_heap_kb"], row["js_page"] = value.get("kb"), value.get("page")
                    self.page_silent = False
                elif time.monotonic() - started >= PAGE_ANSWER_SECONDS and not self.page_silent:
                    # The 1 Nov soak missed a page that froze its renderer after a Wi-Fi drop.
                    self.page_silent = True
                    self.issue(f"the page on screen didn't answer for {PAGE_ANSWER_SECONDS} s: a frozen renderer?")
            except Exception as error:  # noqa: BLE001
                row["error"] = f"js heap: {error}"[:120]
        return row

    def screenshot(self) -> None:
        folder = self.out / "screens"
        folder.mkdir(exist_ok=True)
        png = self.adb("exec-out", "screencap", "-p", binary=True, timeout=60)
        (folder / f"{datetime.now():%Y%m%d-%H%M}.png").write_bytes(png)

    # -- the household -------------------------------------------------------------------
    def act(self, name: str, seconds: float, domain: str, service: str, data: dict) -> bool:
        status = self.ha.service(domain, service, data)
        ok = status == 200
        self.counts.setdefault(name, [0, 0])[0 if ok else 1] += 1
        self.write("actions.log", f"{now()}  {name}: {domain}.{service} → {status}")
        if ok and seconds:
            self.activity = (name, time.time() + seconds)
        if not ok:
            self.issue(f"{name}: {domain}.{service} answered {status}")
        return ok

    def later(self, seconds: float, description: str, check) -> None:
        """Runs the check on its own timer. The main loop can be busy with a sample for most of a
        minute, which once made a 25-second check run after the 60-second duration it checked."""
        def run() -> None:
            if self.stopping.is_set():
                return
            try:
                check()
            except Exception as error:  # noqa: BLE001  (a soak keeps going)
                self.note(f"check failed ({description}): {error!r}")
        timer = threading.Timer(seconds, run)
        timer.daemon = True
        timer.start()

    def expect(self, description: str, condition) -> None:
        state = self.ha.state(self.args.entity)
        if not condition(state.get("state"), state.get("attributes", {})):
            self.issue(f"{description} (state {state.get('state')}, "
                       f"dashboard {state.get('attributes', {}).get('dashboard')}, "
                       f"sign_in {state.get('attributes', {}).get('sign_in_needed')})")
            return False
        return True

    def lights(self) -> None:
        lights = [light for light in self.args.lights.split(",") if light]
        if lights:
            self.act("lights", 0, "light", "turn_on", {"entity_id": lights[0], "brightness": random.randint(30, 255)})
        if len(lights) > 1:
            self.act("lights", 0, "light", "toggle", {"entity_id": lights[1]})

    def notify(self) -> None:
        variants = [{"title": "PleatView soak", "message": "A notification with just text"}]
        if self.args.camera:
            variants.append({"title": "Front door", "message": "Someone is at the door", "camera": self.args.camera})
        if self.gif_url:
            variants.append({"title": "PleatView soak", "message": "An animated picture", "image": self.gif_url})
        data = variants[self.notify_turn % len(variants)]
        self.notify_turn += 1
        self.act("notify", 12, "pleatview", "notify", {"entity_id": self.args.entity, **data})

    def video(self) -> None:
        if self.act("video", 160, "media_player", "play_media", {
                "entity_id": self.args.entity, "media_content_id": self.args.video, "media_content_type": "video/mp4"}):
            self.later(30, "video", lambda: self.expect("the video isn't playing 30 s after play_media",
                                                        lambda state, _: state == "playing"))

    def camera(self) -> None:
        self.act("camera", 65, "pleatview", "show_camera",
                 {"entity_id": self.args.entity, "camera": self.args.camera, "duration": 60})

    def webpage(self) -> None:
        self.act("webpage", 125, "pleatview", "show_dashboard",
                 {"entity_id": self.args.entity, "path": self.args.webpage, "duration": 120})

    def sign_in(self) -> None:
        if not self.act("sign_in", 70, "pleatview", "show_dashboard",
                        {"entity_id": self.args.entity, "path": self.args.sign_in_page, "duration": 60}):
            return
        self.later(25, "sign-in card", lambda: self.expect(
            "the sign-in card didn't show for the password prompt", lambda _, a: bool(a.get("sign_in_needed"))))

        def gone() -> None:
            if not self.expect("the sign-in card was still up 2 minutes later (duration 60 s)",
                               lambda _, a: not a.get("sign_in_needed")):
                self.act("home", 0, "pleatview", "home", {"entity_id": self.args.entity})
        self.later(120, "sign-in gone", gone)

    def reload(self) -> None:
        self.act("reload", 30, "pleatview", "reload", {"entity_id": self.args.entity})

    def schedule(self) -> list[tuple[str, int, int, callable]]:
        """(name, every minutes, at minute, action)"""
        plan = [("lights", 2, 1, self.lights), ("notify", 15, 7, self.notify), ("reload", 360, 5, self.reload)]
        if self.args.video:
            plan.append(("video", 60, 20, self.video))
        if self.args.camera:
            plan.append(("camera", 60, 40, self.camera))
        if self.args.webpage:
            plan.append(("webpage", 120, 30, self.webpage))
        if self.args.sign_in_page:
            plan.append(("sign_in", 180, 50, self.sign_in))
        return plan

    # -- main loop -----------------------------------------------------------------------
    def run(self) -> None:
        (self.out / "runner.pid").write_text(str(os.getpid()))
        self.setup()
        self.watcher.start()
        new = not (self.out / "samples.csv").exists()
        samples = open(self.out / "samples.csv", "a", newline="")
        writer = csv.DictWriter(samples, fieldnames=FIELDS)
        if new:
            writer.writeheader()
        plan = self.schedule()
        done: set[tuple[str, int]] = set()
        sampled = 0
        next_sample = time.time()
        failures = 0
        while not self.stopping.is_set():
            elapsed = time.time() - self.started
            if self.args.hours and elapsed >= self.args.hours * 3600:
                break
            minute = int(elapsed // 60)
            try:
                for name, every, at, action in plan:
                    if minute >= at and (minute - at) % every == 0 and (name, minute) not in done:
                        done.add((name, minute))
                        action()
                if time.time() >= next_sample:
                    next_sample += SAMPLE_SECONDS
                    try:
                        row = self.sample(js=sampled % 5 == 0)
                        failures = 0
                    except Exception as error:  # noqa: BLE001
                        failures += 1
                        row = {"time": now(), "elapsed_h": round(elapsed / 3600, 3), "error": str(error)[:200]}
                        if failures in (1, 5, 30):
                            self.issue(f"sampling failed {failures}× in a row: {error}")
                        self.adb.connect()
                    writer.writerow(row)
                    samples.flush()
                    if sampled % 60 == 0:
                        try:
                            self.screenshot()
                        except Exception as error:  # noqa: BLE001
                            self.note(f"screenshot failed: {error}")
                    sampled += 1
                    (self.out / "status.json").write_text(json.dumps(
                        {"updated": now(), "samples": sampled, "last": row, "actions": self.counts,
                         "serious_log_events": self.watcher.serious}, indent=1, default=str))
            except Exception as error:  # noqa: BLE001  (a soak keeps going)
                self.note(f"tick failed: {error!r}")
            self.stopping.wait(TICK_SECONDS)
        samples.close()
        self.note("stopped")
        (self.out / "report.txt").write_text(report(self.out) + "\n")


# -- report --------------------------------------------------------------------------------
def report(out: Path) -> str:
    rows = list(csv.DictReader(open(out / "samples.csv")))
    good = [r for r in rows if r.get("pss_kb")]
    if not good:
        return "No samples yet."

    def values(key: str, subset=None) -> list[float]:
        rows = subset or good
        if key == "js_heap_kb":  # other sites' heaps say nothing about the dashboard
            rows = [r for r in rows if (r.get("dashboard") or "").startswith("/")]
        return [float(r[key]) for r in rows if r.get(key) not in (None, "")]

    hours = float(good[-1]["elapsed_h"])
    idle = [r for r in good if r["activity"] == "idle"]
    warm = [r for r in idle if float(r["elapsed_h"]) >= 1] or idle
    first = [r for r in idle if float(r["elapsed_h"]) < 1] or idle[:60]
    last = [r for r in idle if float(r["elapsed_h"]) >= hours - 1] or idle[-60:]
    lines = [f"PleatView soak in {out.name}: {hours:.1f} h, {len(rows)} samples, "
             f"{len(rows) - len(good)} without data, last at {good[-1]['time']}", ""]

    def slope(key: str, subset) -> float | None:
        points = [(float(r["elapsed_h"]), float(r[key]) / 1024) for r in subset if r.get(key) not in (None, "")]
        if len(points) < 10:
            return None
        mean_x = statistics.fmean(p[0] for p in points)
        mean_y = statistics.fmean(p[1] for p in points)
        spread = sum((p[0] - mean_x) ** 2 for p in points)
        return sum((p[0] - mean_x) * (p[1] - mean_y) for p in points) / spread if spread else None

    lines.append(f"{'Memory (MB, idle samples)':28}{'1st hour':>10}{'last hour':>11}{'change':>9}{'trend after 1 h':>17}")
    growth = None
    for key, label in [("pss_kb", "PleatView total (PSS)"), ("java_heap_kb", "  Java heap"),
                       ("native_heap_kb", "  native heap"), ("graphics_kb", "  graphics"),
                       ("renderer_pss_kb", "WebView renderer (PSS)"), ("js_heap_kb", "Dashboard JS heap (HA pages)"),
                       ("mem_available_kb", "TV memory available")]:
        a, b = values(key, first), values(key, last)
        if not a or not b:
            continue
        start, end = statistics.median(a) / 1024, statistics.median(b) / 1024
        change = (end - start) / start * 100 if start else 0
        trend = slope(key, warm)
        if key == "pss_kb":
            growth = change
        lines.append(f"{label:28}{start:10.1f}{end:11.1f}{change:+8.1f}%"
                     + (f"{trend:+12.2f} MB/h" if trend is not None else ""))
    lines.append("")
    for key, label in [("threads", "Threads"), ("fds", "Open files"), ("views", "Views"),
                       ("view_roots", "Windows (ViewRootImpl)"), ("activities", "Activities"), ("webviews", "WebViews")]:
        v = values(key)
        if v:
            lines.append(f"{label:28}first {v[0]:.0f}, last {v[-1]:.0f}, max {max(v):.0f}")
    restarts = len({r["pid"] for r in good if r.get("pid")}) - 1
    renderers = len({r["renderer_pid"] for r in good if r.get("renderer_pid")}) - 1
    on_ha = [r for r in good if (r.get("dashboard") or "").startswith("/")]
    online = f"{sum(r.get('ha_connected') == 'True' for r in on_ha) / len(on_ha):.1%}" if on_ha else "n/a"
    lines += ["", f"PleatView process restarts: {restarts}, renderer restarts: {renderers}",
              f"In the foreground: {sum(r['foreground'] == 'True' for r in good) / len(good):.1%} of samples; "
              f"TV awake: {sum(r['wakefulness'] == 'Awake' for r in good) / len(good):.1%}; "
              f"Home Assistant connected on its dashboards: {online}"]
    status = json.loads((out / "status.json").read_text()) if (out / "status.json").exists() else {}
    actions = status.get("actions", {})
    if actions:
        lines.append("Household actions: " + ", ".join(f"{k} {ok} ok" + (f"/{bad} failed" if bad else "")
                                                       for k, (ok, bad) in sorted(actions.items())))
    issues = (out / "issues.log").read_text().splitlines() if (out / "issues.log").exists() else []
    events = (out / "events.log").read_text().splitlines() if (out / "events.log").exists() else []
    serious = (out / "serious.log").read_text().splitlines() if (out / "serious.log").exists() else []
    ours = [e for e in events if PACKAGE in e and ("has died" in e or "Kill" in e or "am_kill" in e)]
    lines += [f"Crashes, ANRs or renderer deaths in logcat: {len(serious)}; PleatView killed or died: {len(ours)}",
              f"Issues noticed: {len(issues)}"]
    lines += [f"  {i}" for i in issues[-15:]]
    lines += [f"  {e[:220]}" for e in serious[-10:]]
    if growth is not None:
        verdict = "PASS" if growth < 20 and not serious and not restarts else "CHECK"
        lines += ["", f"{verdict}: memory {growth:+.1f}% from the first to the last hour (limit +20%), "
                      f"{len(serious)} crash/ANR events, {restarts} restarts"]
    return "\n".join(lines)


def restore(out: Path, ha: HomeAssistant) -> None:
    pid_file = out / "runner.pid"
    if pid_file.exists():
        try:
            os.kill(int(pid_file.read_text()), signal.SIGTERM)
            print("Stopped the runner.")
        except ProcessLookupError:
            pass
    setup = json.loads((out / "setup.json").read_text())
    if "original_options" in setup:
        original = setup.pop("original_options")
        flow_id, _ = ha.options_form(setup["entry_id"])
        status, result = ha.request("POST", f"/api/config/config_entries/options/flow/{flow_id}", original)
        print(f"Options restored: {status} {result.get('type') if isinstance(result, dict) else result}")
    other = live_soak(out)
    if setup.get("created_dashboard") and other:
        # The other TV still rotates through the Soak dashboard: its restore removes it.
        other_setup = json.loads((other / "setup.json").read_text())
        other_setup["created_dashboard"] = True
        (other / "setup.json").write_text(json.dumps(other_setup, indent=2))
        setup.pop("created_dashboard")
        print(f"Left the Soak dashboard to the soak still running in {other.name}.")
    if setup.pop("created_dashboard", False):
        listed = ha.ws({"type": "lovelace/dashboards/list"}).get("result") or []
        for dashboard in listed:
            if dashboard.get("url_path") == SOAK_DASHBOARD:
                ha.ws({"type": "lovelace/dashboards/delete", "dashboard_id": dashboard["id"]})
                print("Removed the Soak dashboard.")
    (out / "setup.json").write_text(json.dumps(setup, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--device", help="the TV, HOST[:PORT] for network adb")
    parser.add_argument("--ha-url", default="http://127.0.0.1:8123")
    parser.add_argument("--ha-token-file", default="~/.config/pleatview/ha-test-token")
    parser.add_argument("--entity", help="the TV's media_player entity")
    parser.add_argument("--out", type=Path, help="folder for the results (default: out/<time>)")
    parser.add_argument("--hours", type=float, default=0, help="stop after this many hours (default: never)")
    parser.add_argument("--rotate", type=int, default=60, help="dashboard rotation in seconds, 0 to leave alone")
    parser.add_argument("--camera", default="camera.demo_camera", help="camera for the Soak dashboard and pop-ups")
    parser.add_argument("--lights", default="light.kitchen_lights,light.bed_light", help="lights to change")
    parser.add_argument("--video", help="media to play every hour, e.g. media-source://media_source/local/clip.mp4")
    parser.add_argument("--gif", help="path on Home Assistant of an image for notifications, e.g. /media/local/a.gif")
    parser.add_argument("--webpage", help="a Webpage dashboard to open every 2 hours, e.g. /dashboard-grafana")
    parser.add_argument("--sign-in-page", help="a page behind a password prompt to open every 3 hours")
    parser.add_argument("--report", type=Path, metavar="OUT", help="print the summary of a soak and exit")
    parser.add_argument("--restore", type=Path, metavar="OUT", help="stop a soak and undo its setup")
    args = parser.parse_args()
    if args.report:
        print(report(args.report))
        return 0
    if args.restore:
        restore(args.restore, HomeAssistant(args.ha_url, Path(args.ha_token_file).expanduser().read_text().strip()))
        return 0
    if not (args.device and args.entity):
        parser.error("--device and --entity are needed to start a soak")
    out = args.out or Path(__file__).resolve().parent / "out" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    soak = Soak(args, out)
    signal.signal(signal.SIGTERM, lambda *_: soak.stopping.set())
    signal.signal(signal.SIGINT, lambda *_: soak.stopping.set())
    soak.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
