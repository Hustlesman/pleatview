"""PleatView Light: python3 -m pleatview_light. See light/README.md."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import platform
import secrets
import signal
import socket

from . import VERSION
from .api import Api
from .config import Config
from .mdns import Responder, local_address
from .pairing import Pairing
from .player import Player
from .screen import Screen

_LOGGER = logging.getLogger("pleatview_light")
PORT = int(os.environ.get("PLEATVIEW_LIGHT_PORT", "8765"))
API_VERSION = 1


def state_dir() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    folder = base / "pleatview-light"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    return folder


def write_private(path: Path, text: str) -> None:
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as out:
        out.write(text)
    os.replace(tmp, path)


def read(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError:
        return None


def screen_size() -> tuple[int, int]:
    """The display's mode, or PLEATVIEW_LIGHT_SCREEN (such as 1280x720) to have Home Assistant
    render smaller, which suits the smallest boards."""
    forced = os.environ.get("PLEATVIEW_LIGHT_SCREEN", "")
    if "x" in forced:
        width, height = forced.lower().split("x", 1)
        return int(width), int(height)
    for modes in sorted(Path("/sys/class/drm").glob("card*-*/modes")):
        status = read(modes.with_name("status")) or ""
        first = (read(modes) or "").split("\n", 1)[0]
        if status.strip() == "connected" and "x" in first:
            width, height = first.split("x", 1)
            return int(width), int(height.rstrip("ip"))
    return 1920, 1080


def model() -> str:
    text = read(Path("/proc/device-tree/model")) or read(Path("/sys/devices/virtual/dmi/id/product_name")) or ""
    return text.strip("\0\n ") or platform.machine()


def take_over(folder: Path) -> None:
    """On a computer that ran the PleatView Linux app, keep its identity, pairings and
    settings, so Home Assistant and other controllers carry on without pairing again."""
    old = folder.parent / "pleatview"
    if (folder / "device.json").exists() or not (old / "device.json").exists():
        return
    for name in ("device.json", "tokens.json", "config.json"):
        if text := read(old / name):
            write_private(folder / name, text)
    _LOGGER.info("Took over the identity and pairings of the PleatView app in %s", old)


async def main() -> None:
    folder = state_dir()
    take_over(folder)
    device = json.loads(read(folder / "device.json") or "{}")
    if not device.get("id"):
        device = {"id": secrets.token_hex(16)}
        write_private(folder / "device.json", json.dumps(device))
    config = Config.load(json.loads(read(folder / "config.json") or "{}"))

    def name() -> str:
        return config_ref[0].device_name or socket.gethostname()

    config_ref = [config]

    def save_config(new: Config) -> None:
        old_name = name()
        config_ref[0] = new
        write_private(folder / "config.json", json.dumps(new.stored()))
        if name() != old_name:
            asyncio.ensure_future(responder.announce())

    def info() -> dict[str, object]:
        return {"id": device["id"], "name": name(), "manufacturer": "Raspberry Pi" if "Raspberry" in model() else "",
                "model": model(), "app_version": VERSION, "api_version": API_VERSION}

    screen: Screen

    def setup() -> dict[str, str]:
        return {"name": name(), "address": local_address(), "port": str(PORT), "pin": pairing.pin}

    pairing = Pairing(lambda: read(folder / "tokens.json"), lambda text: write_private(folder / "tokens.json", text))
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or folder)
    player = Player(runtime / "pleatview-light-mpv.sock", lambda event: asyncio.ensure_future(screen.on_player(event)))
    screen = Screen(config=config, save_config=save_config, player=player, setup=setup, version=VERSION,
                    size=screen_size())
    pairing.on_changed = lambda: asyncio.ensure_future(screen.pin_changed())
    api = Api(screen, pairing, info)
    responder = Responder(lambda: {"id": device["id"], "name": name(), "api": str(API_VERSION), "version": VERSION},
                          PORT)

    server = await asyncio.start_server(api.handle, "0.0.0.0", PORT)
    player.start()
    try:
        await responder.start()
    except OSError as err:
        _LOGGER.warning("Can't announce over mDNS: %s", err)
    await screen.home()
    screen.restart_rotation()
    _LOGGER.info("PleatView Light %s on %s:%d", VERSION, local_address(), PORT)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    await responder.announce(ttl=0)
    responder.stop()
    server.close()
    await player.stop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(main())
