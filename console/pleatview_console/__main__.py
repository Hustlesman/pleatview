"""Starts the console: python3 -m pleatview_console (settings in docs/console.md)."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import ssl

import aiohttp
from aiohttp import web

from . import VERSION
from .keys import ensure_certificate, fingerprint, load_master_key
from .screens import Screens
from .server import create_app
from .settings import from_env
from .store import Store

_LOGGER = logging.getLogger("pleatview_console")


async def main() -> None:
    settings = from_env()
    os.umask(0o077)  # databases, keys and media readable by the console's user only
    store = Store(settings.data, load_master_key(settings.master_key))
    context = None
    if settings.https:
        cert = settings.tls_cert or settings.data / "tls" / "cert.pem"
        key = settings.tls_key or settings.data / "tls" / "key.pem"
        if not settings.tls_cert:
            ensure_certificate(cert, key)
            _LOGGER.info("Certificate fingerprint (SHA-256): %s", fingerprint(cert))
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(cert, key)
    async with aiohttp.ClientSession() as session:
        screens = Screens(store, session, discovery=settings.discovery)
        await screens.start()
        runner = web.AppRunner(create_app(store, screens, https=settings.https), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, settings.host, settings.port, ssl_context=context).start()
        scheme = "https" if context else "http"
        _LOGGER.info("PleatView Console %s at %s://%s:%d", VERSION, scheme, settings.host, settings.port)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await stop.wait()
        await screens.stop()
        await runner.cleanup()
    store.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(main())
