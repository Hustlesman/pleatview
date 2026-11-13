"""Upkeep (docs/protocol.md, "Upkeep"): what a controller needs to look after a screen it can't
walk up to. The app's own recent log, a few fixed checks, restarting itself, and asking the
package's helper (linux/build/pleatview-helper, the one part that runs as root) to restart the
computer or install a newer release."""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import re
import shutil
import socket
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_LOGGER = logging.getLogger(__name__)

RUN = Path(os.environ.get("PLEATVIEW_LIGHT_HELPER_RUN", "/run/pleatview-light"))
VERSION_TEXT = re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,6}$")
UPDATE_STATES = ("waiting", "downloading", "installing", "installed", "failed")
CHECK_SECONDS = 4
STARTED = time.monotonic()
# Addresses keep their host and path; their queries (where tokens travel) are left out.
QUERY = re.compile(r"(\b[a-z][a-z0-9+.-]*://[^\s?#\"']*)\?[^\s\"']*", re.IGNORECASE)
BEARER = re.compile(r"(Bearer\s+)\S+", re.IGNORECASE)


def redact(text: str) -> str:
    return BEARER.sub(r"\1…", QUERY.sub(r"\1?…", text))


class RecentLog(logging.Handler):
    """The app's last log lines, for diagnostics, without the queries of addresses."""

    def __init__(self, size: int = 100) -> None:
        super().__init__(logging.INFO)
        self.lines: collections.deque[str] = collections.deque(maxlen=size)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created))
            self.lines.append(redact(f"{when} {record.levelname} {record.getMessage()}")[:400])
        except Exception:  # noqa: BLE001 - a log line must never break the app
            pass


def newer(version: str, than: str) -> bool:
    def parts(text: str) -> tuple[int, ...]:
        return tuple(int(p) for p in re.findall(r"\d+", text)[:3])
    return parts(version) > parts(than)


class Helper:
    """The package's helper, through its folder in /run (sudo pleatview-light enable sets it up)."""

    def __init__(self, run: Path = RUN) -> None:
        self.run = run

    @property
    def ready(self) -> bool:
        return os.access(self.run / "requests", os.W_OK | os.X_OK)

    def ask(self, name: str) -> None:
        """A request is an empty file named for it; the helper reads only the name."""
        try:
            fd = os.open(self.run / "requests" / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o660)
            os.close(fd)
        except FileExistsError:
            pass

    def restart_device(self) -> None:
        self.ask("restart")

    def update(self, version: str) -> None:
        if not VERSION_TEXT.match(version):
            raise ValueError("version is like 1.0.20")
        self.ask(f"update-{version}")

    def status(self) -> dict[str, Any] | None:
        """How the last update went, as the helper wrote it."""
        try:
            data = json.loads((self.run / "status.json").read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("state") not in UPDATE_STATES or not VERSION_TEXT.match(str(data.get("version"))):
            return None
        error = data.get("error")
        return {"version": data["version"], "state": data["state"], "error": str(error)[:200] if error else None,
                "at": data.get("at") if isinstance(data.get("at"), int) else None}


def restart_app() -> None:
    """Starts this app again in the same process, as systemd keeps it: same arguments, new code
    (after an update) and a fresh start."""
    _LOGGER.info("Starting again")
    logging.shutdown()
    os.execv(sys.executable, [sys.executable, "-m", "pleatview_light", *sys.argv[1:]])


# Diagnostics

def _read(path: str) -> str | None:
    try:
        return Path(path).read_text()
    except OSError:
        return None


def _meminfo() -> dict[str, int] | None:
    text = _read("/proc/meminfo") or ""
    values = {k: int(v.split()[0]) * 1024 for k, _, v in (line.partition(":") for line in text.splitlines()) if v.strip()}
    if "MemTotal" not in values:
        return None
    return {"total": values["MemTotal"], "available": values.get("MemAvailable", values.get("MemFree", 0))}


def _temperature() -> float | None:
    for zone in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
        kind = (_read(str(zone / "type")) or "").strip()
        value = (_read(str(zone / "temp")) or "").strip()
        if value.lstrip("-").isdigit() and ("cpu" in kind or "soc" in kind or "x86_pkg" in kind or zone.name == "thermal_zone0"):
            return round(int(value) / 1000, 1)
    return None


def _power_warning() -> bool | None:
    """Raspberry Pi firmware: the voltage is too low now (bit 0), or has been since it started (bit 16)."""
    text = _read("/sys/devices/platform/soc/soc:firmware/get_throttled")
    if text is None:
        return None
    try:
        return bool(int(text.strip(), 16) & 0x10001)
    except ValueError:
        return None


def _gateway() -> str | None:
    for line in (_read("/proc/net/route") or "").splitlines()[1:]:
        fields = line.split()
        if len(fields) > 2 and fields[1] == "00000000":
            return socket.inet_ntoa(int(fields[2], 16).to_bytes(4, "little"))
    return None


def _wifi_signals() -> dict[str, int]:
    signals = {}
    for line in (_read("/proc/net/wireless") or "").splitlines()[2:]:
        name, _, rest = line.partition(":")
        fields = rest.split()
        if len(fields) > 2:
            try:
                signals[name.strip()] = int(float(fields[2]))
            except ValueError:
                pass
    return signals


def _network() -> list[dict[str, Any]]:
    """Interfaces that are up, with their addresses (not loopback)."""
    signals = _wifi_signals()
    addresses: dict[str, list[str]] = collections.defaultdict(list)
    for line in (_read("/proc/net/if_inet6") or "").splitlines():
        fields = line.split()
        if len(fields) == 6 and fields[5] != "lo":
            raw = fields[0]
            addresses[fields[5]].append(socket.inet_ntop(socket.AF_INET6, bytes.fromhex(raw)))
    try:
        import fcntl
        import struct
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            for _, name in socket.if_nameindex():
                if name == "lo":
                    continue
                try:
                    packed = fcntl.ioctl(sock.fileno(), 0x8915, struct.pack("256s", name[:15].encode()))  # SIOCGIFADDR
                    addresses[name].insert(0, socket.inet_ntoa(packed[20:24]))
                except OSError:
                    pass
    except (ImportError, OSError):
        pass
    result = []
    for name in sorted(addresses):
        if (_read(f"/sys/class/net/{name}/operstate") or "").strip() == "down":
            continue
        wifi = name in signals or Path(f"/sys/class/net/{name}/wireless").exists()
        kind = "wifi" if wifi else "ethernet" if Path(f"/sys/class/net/{name}/device").exists() else "other"
        result.append({"name": name, "kind": kind, "addresses": addresses[name][:6], "signal": signals.get(name)})
    return result


def _os_name() -> str | None:
    for line in (_read("/etc/os-release") or "").splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip().strip('"')[:100]
    return None


async def _timed(check: str, target: str, action: Any) -> dict[str, Any]:
    started = time.monotonic()
    try:
        ok = bool(await asyncio.wait_for(action, CHECK_SECONDS))
    except (OSError, asyncio.TimeoutError, ValueError):
        ok = False
    return {"check": check, "target": target, "ok": ok, "ms": round((time.monotonic() - started) * 1000)}


async def _ping(address: str) -> bool:
    if not shutil.which("ping"):
        raise OSError("no ping")
    process = await asyncio.create_subprocess_exec("ping", "-c", "1", "-W", "2", "-n", "--", address,
                                                   stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    return await process.wait() == 0


async def _resolve(host: str) -> bool:
    return bool(await asyncio.get_running_loop().getaddrinfo(host, None))


async def _open(host: str, port: int) -> bool:
    _, writer = await asyncio.open_connection(host, port)
    writer.close()
    return True


async def checks(home: str | None) -> list[dict[str, Any]]:
    """The gateway answers, the home page's host resolves, and a connection to it opens."""
    jobs = []
    gateway = _gateway()
    if gateway:
        jobs.append(_timed("gateway", gateway, _ping(gateway)))
    parts = urlsplit(home or "")
    if parts.scheme in ("http", "https") and parts.hostname:
        host = parts.hostname
        try:
            port = parts.port or (443 if parts.scheme == "https" else 80)
        except ValueError:
            port = 443
        jobs.append(_timed("dns", host, _resolve(host)))
        jobs.append(_timed("home", f"{host}:{port}", _open(host, port)))
    return list(await asyncio.gather(*jobs))


async def diagnostics(*, home: str | None, log: RecentLog | None, data: Path) -> dict[str, Any]:
    uptime = _read("/proc/uptime")
    try:
        disk = shutil.disk_usage(data)
        storage = {"total": disk.total, "free": disk.free}
    except OSError:
        storage = None
    try:
        load = round(os.getloadavg()[0], 2)
    except OSError:
        load = None
    return {
        "uptime": int(float(uptime.split()[0])) if uptime else None,
        "app_uptime": int(time.monotonic() - STARTED),
        "os": _os_name(),
        "kernel": os.uname().release,
        "load": load,
        "processors": os.cpu_count(),
        "memory": _meminfo(),
        "storage": storage,
        "temperature": _temperature(),
        "power_warning": _power_warning(),
        "network": _network(),
        "gateway": _gateway(),
        "checks": await checks(home),
        "log": list(log.lines) if log else [],
    }
