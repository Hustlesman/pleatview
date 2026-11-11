"""A home page that is a playlist (`home_playlist`, from PleatView Console): every item is kept
on disk, so the list plays on without the network and after a restart."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from pathlib import Path
from typing import Any
import urllib.request

_LOGGER = logging.getLogger(__name__)

CHUNK = 1024 * 1024
TIMEOUT = 30


class MediaCache:
    def __init__(self, folder: Path) -> None:
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._task: asyncio.Task | None = None

    def path(self, url: str) -> Path:
        return self.folder / hashlib.sha256(url.encode()).hexdigest()

    def source(self, url: str) -> str:
        """The cached file when it's complete, else the address itself."""
        path = self.path(url)
        return str(path) if path.is_file() else url

    def keep(self, items: list[dict[str, Any]]) -> None:
        """Fetches what's missing in the background, and forgets what the list no longer has."""
        wanted = {self.path(item["url"]).name for item in items}
        for old in self.folder.iterdir():
            if old.name not in wanted and not old.name.endswith(".part"):
                old.unlink(missing_ok=True)
        if self._task:
            self._task.cancel()
        self._task = asyncio.create_task(self._fetch_all([item["url"] for item in items]))

    async def _fetch_all(self, urls: list[str]) -> None:
        for url in urls:
            if not self.path(url).is_file():
                try:
                    await asyncio.to_thread(self._fetch, url)
                except OSError as err:
                    _LOGGER.info("Not cached yet: %s (%s)", url, err)

    def _fetch(self, url: str) -> None:
        target = self.path(url)
        part = target.with_name(target.name + ".part")
        request = urllib.request.Request(url, headers={"User-Agent": "PleatView Light"})
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response, open(part, "wb") as out:
            while chunk := response.read(CHUNK):
                out.write(chunk)
        os.replace(part, target)
