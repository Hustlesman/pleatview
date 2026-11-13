"""A controller's end of protocol v2 (docs/protocol-v2.md) for the end-to-end tests: TLS with
a certificate it makes for itself, one screen at a time, enrolment with a token, requests,
and revocation. It checks every signature against the address it gave the screen."""

from __future__ import annotations

import asyncio
import base64
import datetime
import hashlib
import json
import secrets
import ssl
import sys
import tempfile
import threading
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "light"))
from pleatview_light import websocket  # noqa: E402  # the frame functions, shared with PleatView Light

NONCE = "ab" * 32  # a fixed challenge is fine for a test


def signed_text(address: str, nonce: str) -> str:
    return f"pleatview-v2\n{address}\n{nonce}"


class TestController:
    def __init__(self, port: int = 18443) -> None:
        self.port = port
        self.folder = tempfile.TemporaryDirectory()
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "PleatView end-to-end test controller")])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
                .not_valid_after(now + datetime.timedelta(days=2)).sign(key, hashes.SHA256()))
        folder = Path(self.folder.name)
        (folder / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (folder / "key.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                           serialization.NoEncryption()))
        self.pin = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
        self.address = ""
        self.tokens: set[str] = set()
        self.keys: dict[str, str] = {}
        self.revoked: set[str] = set()
        self.hellos: list[dict] = []
        self.states: list[dict] = []
        self.problems: list[str] = []
        self._answers: dict[int, asyncio.Future] = {}
        self._next = 1
        self._writer: asyncio.StreamWriter | None = None
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()
        self._ready.wait(10)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(Path(self.folder.name) / "cert.pem", Path(self.folder.name) / "key.pem")
        self.server = self.loop.run_until_complete(asyncio.start_server(self._handle, "127.0.0.1", self.port, ssl=context))
        self._ready.set()
        self.loop.run_forever()

    def new_token(self) -> str:
        token = secrets.token_urlsafe(24)
        self.tokens.add(token)
        return token

    async def _send(self, writer: asyncio.StreamWriter, message: dict) -> None:
        writer.write(websocket.encode(websocket.TEXT, json.dumps(message).encode(), mask=False))
        await writer.drain()

    async def _close(self, writer: asyncio.StreamWriter, code: int) -> None:
        writer.write(websocket.encode(websocket.CLOSE, code.to_bytes(2, "big"), mask=False))
        await writer.drain()
        writer.close()

    async def _receive(self, reader: asyncio.StreamReader) -> dict:
        while True:
            _, opcode, payload = await websocket.read_frame(reader)
            if opcode == websocket.TEXT:
                return json.loads(payload)
            if opcode == websocket.CLOSE:
                raise ConnectionError("closed")

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
            headers = {k.strip().lower(): v.strip() for k, _, v in (l.partition(":") for l in head.split("\r\n")[1:] if l)}
            writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                          f"Sec-WebSocket-Accept: {websocket.accept_key(headers['sec-websocket-key'])}\r\n"
                          "Sec-WebSocket-Protocol: pleatview.v2\r\n\r\n").encode())
            await self._send(writer, {"type": "challenge", "nonce": NONCE})
            hello = await self._receive(reader)
            self.hellos.append(hello)
            if hello.get("type") == "enrol":
                public = hello.get("key", "")
                if hello.get("token") not in self.tokens:
                    return await self._close(writer, 4403)
                self.tokens.discard(hello["token"])
            else:
                public = self.keys.get(hello.get("key", ""), "")
                if not public or hello.get("key") in self.revoked:
                    return await self._close(writer, 4401)
            key = serialization.load_der_public_key(base64.b64decode(public))
            try:
                key.verify(base64.b64decode(hello["signature"]), signed_text(self.address, NONCE).encode(),
                           ec.ECDSA(hashes.SHA256()))
            except Exception:  # noqa: BLE001
                self.problems.append("a signature didn't match the address")
                return await self._close(writer, 4403)
            self.keys[hashlib.sha256(base64.b64decode(public)).hexdigest()] = public
            self._writer = writer
            await self._send(writer, {"type": "welcome"})
            while True:
                message = await self._receive(reader)
                if message.get("type") == "state":
                    self.states.append(message["state"])
                elif message.get("type") == "response" and message.get("id") in self._answers:
                    self._answers.pop(message["id"]).set_result(message)
                elif message.get("type") == "ping":
                    await self._send(writer, {"type": "pong"})
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass

    def request(self, method: str, params: dict | None = None, timeout: float = 15) -> dict:
        async def ask() -> dict:
            request_id = self._next
            self._next += 1
            answer = self.loop.create_future()
            self._answers[request_id] = answer
            await self._send(self._writer, {"type": "request", "id": request_id, "method": method, "params": params or {}})
            return await asyncio.wait_for(answer, timeout)
        return asyncio.run_coroutine_threadsafe(ask(), self.loop).result(timeout + 5)

    def revoke(self) -> None:
        """Removes the screen: its key is refused from now on, and its connection closed."""
        self.revoked.update(self.keys)
        if self._writer:
            asyncio.run_coroutine_threadsafe(self._close(self._writer, 4403), self.loop).result(5)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.server.close)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.folder.cleanup()
