"""Protocol v2 in PleatView Light: the shared test vectors, and a connection out to a fake
controller over TLS with a pinned certificate."""

from __future__ import annotations

import asyncio
import base64
import datetime
import hashlib
import json
import ssl
import sys
import tempfile
import unittest
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pleatview_light import connect, websocket  # noqa: E402

VECTORS = json.loads((Path(__file__).resolve().parents[2] / "tests/fixtures/protocol-v2.json").read_text())


def write(path: Path, text: str) -> None:
    path.write_text(text)


class Vectors(unittest.TestCase):
    def test_addresses_text_and_the_key(self) -> None:
        for case in VECTORS["addresses"]:
            self.assertEqual(connect.address(case["given"]), case["address"])
        signed = VECTORS["signed"]
        self.assertEqual(connect.signed_text(signed["address"], signed["nonce"]), signed["text"])
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "v2-key.pem").write_text(VECTORS["key"]["private_pem"])
            conn = connect.Connection(folder, info=dict, route=None, state=dict, subscribe=None,
                                      unsubscribe=None, show_code=None, write=write)
            self.assertEqual(conn.public, VECTORS["key"]["public"])
            self.assertEqual(conn.key_id, VECTORS["key"]["id"])
            # Its signatures check out against the public key, over the agreed text.
            public = serialization.load_der_public_key(base64.b64decode(conn.public))
            public.verify(base64.b64decode(conn.sign(signed["address"], signed["nonce"])),
                          signed["text"].encode(), ec.ECDSA(hashes.SHA256()))


def certificate(folder: Path) -> tuple[Path, Path, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "controller.test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30)).sign(key, hashes.SHA256()))
    cert_path, key_path = folder / "cert.pem", folder / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return cert_path, key_path, hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


class FakeController:
    """A controller's end of protocol v2, enough for one screen at a time."""

    def __init__(self, folder: Path) -> None:
        self.cert, self.key, self.pin = certificate(folder)
        self.keys: dict[str, str] = {}  # key id -> public key
        self.hellos: list[dict] = []
        self.answers: asyncio.Queue = asyncio.Queue()
        self.states: list[dict] = []
        self.give_code = True
        self.close_with: int | None = None
        self.port = 0

    @property
    def url(self) -> str:
        return f"wss://127.0.0.1:{self.port}/api/pleatview/v2"

    async def start(self) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.cert, self.key)
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0, ssl=context)
        self.port = self.server.sockets[0].getsockname()[1]

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = (await reader.readuntil(b"\r\n\r\n")).decode()
        headers = {k.strip().lower(): v.strip() for k, _, v in (l.partition(":") for l in head.split("\r\n")[1:] if l)}
        writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {websocket.accept_key(headers['sec-websocket-key'])}\r\n"
                      f"Sec-WebSocket-Protocol: {headers['sec-websocket-protocol']}\r\n\r\n").encode())
        self.writer = writer
        self.reader = reader
        nonce = "ab" * 32
        await self.send({"type": "challenge", "nonce": nonce})
        hello = await self.receive()
        self.hellos.append(hello)
        if self.close_with:
            return await self.close(self.close_with)
        if hello["type"] == "enrol":
            public = hello["key"]
            if self.give_code and "token" not in hello:
                await self.send({"type": "code", "code": "K7RM-W2XD", "expires_in": 600})
                await asyncio.sleep(0.2)
        else:
            public = self.keys[hello["key"]]
        key = serialization.load_der_public_key(base64.b64decode(public))
        key.verify(base64.b64decode(hello["signature"]),
                   connect.signed_text(connect.address(self.url), nonce).encode(), ec.ECDSA(hashes.SHA256()))
        self.keys[hashlib.sha256(base64.b64decode(public)).hexdigest()] = public
        await self.send({"type": "welcome"})
        try:
            while True:
                message = await self.receive()
                if message["type"] == "state":
                    self.states.append(message["state"])
                elif message["type"] == "response":
                    await self.answers.put(message)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass

    async def send(self, data: dict) -> None:
        self.writer.write(websocket.encode(websocket.TEXT, json.dumps(data).encode(), mask=False))
        await self.writer.drain()

    async def receive(self) -> dict:
        while True:
            _, opcode, payload = await websocket.read_frame(self.reader)
            if opcode == websocket.TEXT:
                return json.loads(payload)
            if opcode == websocket.CLOSE:
                raise ConnectionError("closed")

    async def close(self, code: int) -> None:
        self.writer.write(websocket.encode(websocket.CLOSE, code.to_bytes(2, "big"), mask=False))
        await self.writer.drain()
        self.writer.close()


class ConnectsOut(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.controller = FakeController(self.folder)
        await self.controller.start()
        self.codes: list[tuple] = []
        self.calls: list[tuple] = []
        self.listeners: set[asyncio.Queue] = set()

        async def route(method, path, body, token, *, trusted=False, local=False):  # noqa: ANN001, ANN202
            self.calls.append((method, path, body, trusted))
            if path == "/api/command" and body.get("command") == "explode":
                return 400, {"error": "unknown command explode"}
            return 200, {"ok": path}

        async def show_code(code, host):  # noqa: ANN001, ANN202
            self.codes.append((code, host))

        def subscribe() -> asyncio.Queue:
            queue: asyncio.Queue = asyncio.Queue()
            self.listeners.add(queue)
            return queue

        self.connection = connect.Connection(
            self.folder, info=lambda: {"id": "light-1", "name": "Hall", "app_version": "1.0.19"},
            route=route, state=lambda: {"power": "on", "features": ["protocol_v2"]}, subscribe=subscribe,
            unsubscribe=self.listeners.discard, show_code=show_code, write=write)

    async def asyncTearDown(self) -> None:
        await self.connection.stop()
        self.controller.server.close()
        self.tmp.cleanup()

    async def until(self, check) -> None:  # noqa: ANN001
        for _ in range(100):
            if check():
                return
            await asyncio.sleep(0.05)
        self.fail("timed out")

    async def test_enrol_with_a_code_answer_calls_and_sign_in_again(self) -> None:
        self.connection.start()
        self.connection.enrol(self.controller.url, pin=self.controller.pin)
        await self.until(lambda: self.connection.online)
        self.assertEqual(self.codes[0], ("K7RM-W2XD", "127.0.0.1"))
        self.assertEqual(self.codes[-1], (None, ""))  # the code goes once it's let in
        hello = self.controller.hellos[0]
        self.assertEqual((hello["type"], hello["info"]["id"]), ("enrol", "light-1"))
        self.assertNotIn("token", hello)
        self.assertTrue(json.loads((self.folder / "controller.json").read_text())["enrolled"])
        await self.until(lambda: self.controller.states)
        # A request is a v1 call, made as trusted.
        await self.controller.send({"type": "request", "id": 1, "method": "command", "params": {"command": "home"}})
        self.assertEqual(await self.controller.answers.get(), {"type": "response", "id": 1, "result": {"ok": "/api/command"}})
        self.assertEqual(self.calls[-1], ("POST", "/api/command", {"command": "home"}, True))
        await self.controller.send({"type": "request", "id": 2, "method": "command", "params": {"command": "explode"}})
        self.assertEqual(await self.controller.answers.get(), {"type": "response", "id": 2, "error": "unknown command explode"})
        await self.controller.send({"type": "request", "id": 3, "method": "pair", "params": {}})
        self.assertIn("unknown method", (await self.controller.answers.get())["error"])
        # State changes go to the controller.
        for queue in self.listeners:
            queue.put_nowait({"power": "off"})
        await self.until(lambda: {"power": "off"} in self.controller.states)
        # Dropped: back by itself, signing in with its key this time.
        self.controller.writer.close()
        await self.until(lambda: len(self.controller.hellos) == 2)
        self.assertEqual(self.controller.hellos[1]["type"], "auth")
        self.assertEqual(self.controller.hellos[1]["key"], self.connection.key_id)

    async def test_with_a_token_no_code_and_removed_means_forgotten(self) -> None:
        self.connection.start()
        self.connection.enrol(self.controller.url, token="one-time", pin=self.controller.pin)
        await self.until(lambda: self.connection.online)
        self.assertEqual(self.controller.hellos[0]["token"], "one-time")
        self.assertEqual(self.codes, [(None, "")])
        self.controller.close_with = connect.REVOKED
        self.controller.writer.close()
        await self.until(lambda: json.loads((self.folder / "controller.json").read_text()) == {})
        self.assertIsNone(self.connection.token)

    async def test_a_certificate_other_than_the_pinned_one_is_refused(self) -> None:
        self.connection.start()
        self.connection.enrol(self.controller.url, pin="00" * 32)
        await asyncio.sleep(0.5)
        self.assertEqual(self.controller.hellos, [])
        self.assertFalse(self.connection.online)
        with self.assertRaises(ValueError):
            self.connection.enrol(self.controller.url, pin="not-hex")
        with self.assertRaises(ValueError):
            self.connection.enrol("https://controller.example/")

    async def enrolled(self) -> None:
        self.connection.start()
        self.connection.enrol(self.controller.url, token="one-time", pin=self.controller.pin)
        await self.until(lambda: self.connection.online)

    async def test_a_move_is_kept_once_the_new_address_lets_it_in(self) -> None:
        await self.enrolled()
        (self.folder / "b").mkdir()
        moved = FakeController(self.folder / "b")
        moved.keys = self.controller.keys  # the same controller, at another address
        await moved.start()
        self.addCleanup(moved.server.close)
        await self.controller.send({"type": "move", "url": "https://elsewhere.example/api/pleatview/v2"})
        await self.controller.send({"type": "move", "url": moved.url, "fingerprint": moved.pin})
        await self.until(lambda: moved.hellos)
        self.assertEqual(moved.hellos[0]["type"], "auth")
        await self.until(lambda: json.loads((self.folder / "controller.json").read_text())["url"] == connect.address(moved.url))
        self.assertEqual(json.loads((self.folder / "controller.json").read_text())["pin"], moved.pin)

    async def test_a_move_that_doesnt_let_it_in_goes_back(self) -> None:
        connect.FIRST_WAIT = 0.05
        self.addCleanup(setattr, connect, "FIRST_WAIT", 2)
        await self.enrolled()
        (self.folder / "c").mkdir()
        refusing = FakeController(self.folder / "c")
        refusing.close_with = connect.REVOKED  # not even 4403 there makes it forget the controller
        await refusing.start()
        self.addCleanup(refusing.server.close)
        await self.controller.send({"type": "move", "url": refusing.url, "fingerprint": refusing.pin})
        await self.until(lambda: len(refusing.hellos) == connect.MOVE_TRIES and len(self.controller.hellos) == 2)
        kept = json.loads((self.folder / "controller.json").read_text())
        self.assertEqual((kept["url"], kept["enrolled"]), (connect.address(self.controller.url), True))
        self.assertIsNone(self.connection.moving)

    async def test_retry_waits_as_the_controller_says(self) -> None:
        await self.enrolled()
        await self.controller.send({"type": "retry", "after": 7200})  # out of range: ignored
        await self.controller.send({"type": "retry", "after": 1})
        await self.until(lambda: self.connection.retry_after == 1)
        self.controller.writer.close()
        await self.until(lambda: len(self.controller.hellos) == 2)

    async def test_busy_answers_say_when_to_come_back(self) -> None:
        async def busy(reader, writer):  # noqa: ANN001, ANN202
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 503 Service Unavailable\r\nRetry-After: 45\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.controller.cert, self.controller.key)
        server = await asyncio.start_server(busy, "127.0.0.1", 0, ssl=context)
        self.addCleanup(server.close)
        port = server.sockets[0].getsockname()[1]
        with self.assertRaises(websocket.Busy) as raised:
            await websocket.connect(f"wss://127.0.0.1:{port}/api/pleatview/v2", subprotocol=connect.SUBPROTOCOL,
                                    pin=self.controller.pin)
        self.assertEqual(raised.exception.retry_after, 45)


if __name__ == "__main__":
    unittest.main()
