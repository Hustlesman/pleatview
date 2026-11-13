import assert from "node:assert/strict";
import { X509Certificate, createHash, createPublicKey, verify } from "node:crypto";
import { readFileSync } from "node:fs";
import { createServer } from "node:tls";
import { test } from "node:test";
import { CLOSE, Connection, address, signedText } from "../src/connect.js";
import { FrameReader, OP, acceptKey, encode } from "../src/websocket.js";

const vectors = JSON.parse(readFileSync(new URL("../../tests/fixtures/protocol-v2.json", import.meta.url), "utf8"));
const cert = readFileSync(new URL("./fixtures/controller-cert.pem", import.meta.url));
const certKey = readFileSync(new URL("./fixtures/controller-key.pem", import.meta.url));
const pin = createHash("sha256").update(new X509Certificate(cert).raw).digest("hex");

class MemoryStore {
  constructor(files = {}) {
    this.files = { ...files };
  }
  read(name) {
    return this.files[name] ?? null;
  }
  readJson(name, fallback = {}) {
    return this.files[name] ? JSON.parse(this.files[name]) : fallback;
  }
  write(name, text) {
    this.files[name] = text;
  }
  writeJson(name, value) {
    this.files[name] = JSON.stringify(value);
  }
}

const until = async (check, ms = 8000) => {
  for (const end = Date.now() + ms; Date.now() < end; ) {
    if (check()) return;
    await new Promise((r) => setTimeout(r, 25));
  }
  assert.fail("timed out");
};

test("addresses, the signed text and the key are the shared test vectors'", () => {
  for (const c of vectors.addresses) assert.equal(address(c.given), c.address);
  assert.equal(signedText(vectors.signed.address, vectors.signed.nonce), vectors.signed.text);
  const conn = new Connection({ store: new MemoryStore({ "v2-key.pem": vectors.key.private_pem }), info: () => ({}),
    call: async () => ({}), stateJson: () => "{}", onState: () => () => {}, showCode: () => {} });
  assert.equal(conn.public, vectors.key.public);
  assert.equal(conn.keyId, vectors.key.id);
  const signature = Buffer.from(conn.sign(vectors.signed.address, vectors.signed.nonce), "base64");
  const key = createPublicKey({ key: Buffer.from(conn.public, "base64"), format: "der", type: "spki" });
  assert.ok(verify("sha256", Buffer.from(vectors.signed.text), { key, dsaEncoding: "der" }, signature));
  assert.throws(() => address("https://controller.example/"), RangeError);
});

/** A controller's end of protocol v2, enough for one screen at a time. */
function fakeController() {
  const c = { hellos: [], states: [], answers: [], sockets: [], closeWith: null, keys: new Map() };
  c.server = createServer({ cert, key: certKey }, (socket) => {
    c.sockets.push(socket);
    const reader = new FrameReader();
    let upgraded = false;
    let head = Buffer.alloc(0);
    c.send = (value) => socket.write(encode(OP.TEXT, JSON.stringify(value), { mask: false }));
    c.close = (code) => {
      const payload = Buffer.alloc(2);
      payload.writeUInt16BE(code);
      socket.end(encode(OP.CLOSE, payload, { mask: false }));
    };
    socket.on("error", () => {});
    socket.on("data", (chunk) => {
      if (!upgraded) {
        head = Buffer.concat([head, chunk]);
        const end = head.indexOf("\r\n\r\n");
        if (end < 0) return;
        const key = /sec-websocket-key: *(\S+)/i.exec(head.toString())[1];
        socket.write(`HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n` +
          `Sec-WebSocket-Accept: ${acceptKey(key)}\r\nSec-WebSocket-Protocol: pleatview.v2\r\n\r\n`);
        upgraded = true;
        c.send({ type: "challenge", nonce: "ab".repeat(32) });
        chunk = head.subarray(end + 4);
      }
      reader.feed(chunk);
      for (let frame = reader.next(); frame; frame = reader.next()) {
        if (frame.opcode !== OP.TEXT) continue;
        const message = JSON.parse(frame.payload.toString());
        if (message.type === "auth" || message.type === "enrol") {
          c.hellos.push(message);
          if (c.closeWith) return c.close(c.closeWith);
          const publicKey = message.type === "enrol" ? message.key : c.keys.get(message.key);
          const key = createPublicKey({ key: Buffer.from(publicKey, "base64"), format: "der", type: "spki" });
          const text = signedText(address(c.url), "ab".repeat(32));
          assert.ok(verify("sha256", Buffer.from(text), { key, dsaEncoding: "der" }, Buffer.from(message.signature, "base64")));
          c.keys.set(createHash("sha256").update(Buffer.from(publicKey, "base64")).digest("hex"), publicKey);
          if (message.type === "enrol" && !message.token) c.send({ type: "code", code: "K7RM-W2XD", expires_in: 600 });
          setTimeout(() => c.send({ type: "welcome" }), 50);
        } else if (message.type === "state") c.states.push(message.state);
        else if (message.type === "response") c.answers.push(message);
      }
    });
  });
  return c;
}

async function setUp(t) {
  const controller = fakeController();
  await new Promise((r) => controller.server.listen(0, "127.0.0.1", r));
  controller.url = `wss://127.0.0.1:${controller.server.address().port}/api/pleatview/v2`;
  const codes = [];
  const calls = [];
  const listeners = new Set();
  const store = new MemoryStore();
  const conn = new Connection({
    store,
    info: () => ({ id: "linux-1", name: "Lobby", app_version: "1.0.19" }),
    call: async (method, params) => {
      calls.push([method, params]);
      if (method === "command" && params.command === "explode") throw new Error("unknown command explode");
      return { ok: method };
    },
    stateJson: () => JSON.stringify({ power: "on" }),
    onState: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    showCode: (code, host) => codes.push([code, host]),
  });
  t.after(() => {
    conn.stop();
    controller.sockets.forEach((s) => s.destroy());
    controller.server.close();
  });
  return { controller, conn, codes, calls, listeners, store };
}

test("enrols with a code, answers calls, pushes state and signs in again", async (t) => {
  const { controller, conn, codes, calls, listeners, store } = await setUp(t);
  conn.start();
  conn.enrol(controller.url, null, pin);
  await until(() => conn.online);
  assert.deepEqual(codes[0], ["K7RM-W2XD", "127.0.0.1"]);
  assert.deepEqual(codes.at(-1), [null, ""]);
  assert.equal(controller.hellos[0].type, "enrol");
  assert.equal(controller.hellos[0].info.id, "linux-1");
  assert.equal(JSON.parse(store.files["controller.json"]).enrolled, true);
  await until(() => controller.states.length > 0);
  controller.send({ type: "request", id: 1, method: "command", params: { command: "home" } });
  controller.send({ type: "request", id: 2, method: "command", params: { command: "explode" } });
  await until(() => controller.answers.length === 2);
  assert.deepEqual(controller.answers[0], { type: "response", id: 1, result: { ok: "command" } });
  assert.deepEqual(controller.answers[1], { type: "response", id: 2, error: "unknown command explode" });
  assert.deepEqual(calls[0], ["command", { command: "home" }]);
  for (const listener of listeners) listener(JSON.stringify({ power: "off" }));
  await until(() => controller.states.some((s) => s.power === "off"));
  controller.sockets.at(-1).destroy();
  await until(() => controller.hellos.length === 2);
  assert.equal(controller.hellos[1].type, "auth");
  assert.equal(controller.hellos[1].key, conn.keyId);
});

test("enrols with a token without a code; a controller that removes it is forgotten", async (t) => {
  const { controller, conn, codes, store } = await setUp(t);
  conn.start();
  conn.enrol(controller.url, "one-time", pin);
  await until(() => conn.online);
  assert.equal(controller.hellos[0].token, "one-time");
  assert.deepEqual(codes, [[null, ""]]);
  controller.closeWith = CLOSE.REVOKED;
  controller.sockets.at(-1).destroy();
  await until(() => store.files["controller.json"] === "{}");
  assert.equal(conn.token, null);
});

test("a certificate other than the pinned one is refused", async (t) => {
  const { controller, conn } = await setUp(t);
  conn.start();
  conn.enrol(controller.url, null, "00".repeat(32));
  await new Promise((r) => setTimeout(r, 500));
  assert.equal(controller.hellos.length, 0);
  assert.equal(conn.online, false);
  assert.throws(() => conn.enrol(controller.url, null, "not-hex"), RangeError);
});
