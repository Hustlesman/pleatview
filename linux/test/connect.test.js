import assert from "node:assert/strict";
import { X509Certificate, createHash, createPublicKey, verify } from "node:crypto";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createServer } from "node:tls";
import { test } from "node:test";
import { builtIn, decode, encode as encodeAddress } from "../src/builtin.js";
import { CLOSE, CODES_MOST, Connection, MOVE_TRIES, address, signedText, timing } from "../src/connect.js";
import { Question } from "../src/question.js";
import { FrameReader, OP, acceptKey, connect as wsConnect, encode } from "../src/websocket.js";

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
  // confirm: the organisation to ask the screen about (enrol_confirm); expireCodes: every code
  // lapses before anyone types it.
  const c = { hellos: [], states: [], answers: [], sockets: [], closeWith: null, keys: new Map(), confirm: null,
    expireCodes: false, confirmed: [] };
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
          if (message.type === "enrol" && c.expireCodes) return setTimeout(() => c.close(CLOSE.CODE_EXPIRED), 20);
          if (message.type === "enrol" && c.confirm) return setTimeout(() => c.send({ type: "confirm", organisation: c.confirm }), 20);
          setTimeout(() => c.send({ type: "welcome" }), 50);
        } else if (message.type === "confirmed") {
          c.confirmed.push(true);
          c.send({ type: "welcome" });
        } else if (message.type === "declined") {
          c.confirmed.push(false);
          c.close(CLOSE.REVOKED);
        } else if (message.type === "state") c.states.push(message.state);
        else if (message.type === "response") c.answers.push(message);
      }
    });
  });
  return c;
}

async function setUp(t, more = {}) {
  const controller = fakeController();
  await new Promise((r) => controller.server.listen(0, "127.0.0.1", r));
  controller.url = `wss://127.0.0.1:${controller.server.address().port}/api/pleatview/v2`;
  const codes = [];
  const calls = [];
  const listeners = new Set();
  const store = new MemoryStore();
  const conn = new Connection({
    store,
    info: () => ({ id: "linux-1", name: "Lobby", manufacturer: "Raspberry Pi", app_version: "1.0.19" }),
    call: async (method, params) => {
      calls.push([method, params]);
      if (method === "command" && params.command === "explode") throw new Error("unknown command explode");
      return { ok: method };
    },
    stateJson: () => JSON.stringify({ power: "on", features: ["protocol_v2", "enrol_confirm"] }),
    onState: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    showCode: (code, host) => codes.push([code, host]),
    builtIn: () => null,
    ...more,
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
  // Until it's added, little about itself: no name, which often holds a person's.
  assert.deepEqual(controller.hellos[0].info, { id: "linux-1", manufacturer: "Raspberry Pi", app_version: "1.0.19",
    features: ["protocol_v2", "enrol_confirm"] });
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

async function another(t, keys) {
  const other = fakeController();
  other.keys = keys; // the same controller, at another address
  await new Promise((r) => other.server.listen(0, "127.0.0.1", r));
  other.url = `wss://127.0.0.1:${other.server.address().port}/api/pleatview/v2`;
  t.after(() => {
    other.sockets.forEach((s) => s.destroy());
    other.server.close();
  });
  return other;
}

test("a move is kept once the new address lets it in", async (t) => {
  const { controller, conn, store } = await setUp(t);
  conn.start();
  conn.enrol(controller.url, "one-time", pin);
  await until(() => conn.online);
  const moved = await another(t, controller.keys);
  controller.send({ type: "move", url: "https://elsewhere.example/api/pleatview/v2" });
  controller.send({ type: "move", url: moved.url, fingerprint: pin });
  await until(() => moved.hellos.length === 1);
  assert.equal(moved.hellos[0].type, "auth");
  await until(() => JSON.parse(store.files["controller.json"]).url === address(moved.url));
  assert.equal(JSON.parse(store.files["controller.json"]).pin, pin);
});

test("a move that doesn't let it in goes back, without forgetting the controller", async (t) => {
  timing.firstWaitMs = 50;
  t.after(() => (timing.firstWaitMs = 2000));
  const { controller, conn, store } = await setUp(t);
  conn.start();
  conn.enrol(controller.url, "one-time", pin);
  await until(() => conn.online);
  const refusing = await another(t, new Map());
  refusing.closeWith = CLOSE.REVOKED;
  controller.send({ type: "move", url: refusing.url, fingerprint: pin });
  await until(() => refusing.hellos.length === MOVE_TRIES && controller.hellos.length === 2);
  const kept = JSON.parse(store.files["controller.json"]);
  assert.equal(kept.url, address(controller.url));
  assert.equal(kept.enrolled, true);
  assert.equal(conn.moving, null);
});

test("retry says how long to wait; out of range it's ignored", async (t) => {
  const { controller, conn } = await setUp(t);
  conn.start();
  conn.enrol(controller.url, "one-time", pin);
  await until(() => conn.online);
  controller.send({ type: "retry", after: 7200 });
  controller.send({ type: "retry", after: 1 });
  await until(() => conn.retryAfter === 1);
  controller.sockets.at(-1).destroy();
  await until(() => controller.hellos.length === 2);
});

test("a busy controller's 503 says when to come back", async (t) => {
  const server = createServer({ cert, key: certKey }, (socket) => {
    socket.on("error", () => {});
    socket.once("data", () => socket.end("HTTP/1.1 503 Service Unavailable\r\nRetry-After: 45\r\nContent-Length: 0\r\n\r\n"));
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  t.after(() => server.close());
  await assert.rejects(wsConnect(`wss://127.0.0.1:${server.address().port}/api/pleatview/v2`, { subprotocol: "pleatview.v2", pin }),
    (e) => e.retryAfter === 45);
});

test("allowed on the screen: the person in front of it lets the organisation in", async (t) => {
  const asked = [];
  const { controller, conn, store } = await setUp(t, {
    askAllow: async (organisation, host, how) => {
      asked.push([organisation, host, how.waitMs]);
      return true;
    },
  });
  controller.confirm = "Acme";
  conn.start();
  conn.enrol(controller.url, null, pin);
  await until(() => conn.online);
  assert.deepEqual(asked, [["Acme", "127.0.0.1", timing.allowWaitMs]]);
  assert.deepEqual(controller.confirmed, [true]);
  assert.equal(JSON.parse(store.files["controller.json"]).enrolled, true);
  // Once it's in, the controller reads the rest (its name) with info.
  controller.sockets.at(-1).destroy();
  await until(() => controller.hellos.length === 2);
  assert.equal(controller.hellos[1].type, "auth");
  assert.equal(controller.hellos[1].info.name, "Lobby");
});

for (const [answer, why, said] of [[false, "not allowed on the screen", [false]], [null, "nobody allowed it in time", []]]) {
  test(`not added when ${why}: nothing connects again`, async (t) => {
    const told = [];
    const { controller, conn, store, codes } = await setUp(t, {
      askAllow: async () => answer,
      notAdded: (host, reason) => told.push([host, reason]),
    });
    controller.confirm = "Acme";
    conn.start();
    conn.enrol(controller.url, null, pin);
    await until(() => told.length === 1);
    assert.deepEqual(told, [["127.0.0.1", why]]);
    assert.deepEqual(controller.confirmed, said);
    assert.equal(store.files["controller.json"], "{}");
    assert.deepEqual(codes.at(-1), [null, ""]);
    await new Promise((r) => setTimeout(r, 300));
    assert.equal(controller.hellos.length, 1);
  });
}

test("the question goes when the controller stops waiting", async (t) => {
  let signal = null;
  const { controller, conn } = await setUp(t, {
    askAllow: (organisation, host, how) => {
      signal = how.signal;
      return new Promise(() => {}); // nobody at the screen
    },
  });
  controller.confirm = "Acme";
  conn.start();
  conn.enrol(controller.url, null, pin);
  await until(() => signal);
  assert.equal(signal.aborted, false);
  controller.close(CLOSE.CODE_EXPIRED);
  await until(() => signal.aborted);
});

test("not for ever: given up after six codes, or an hour", async (t) => {
  timing.firstWaitMs = 5;
  t.after(() => (timing.firstWaitMs = 2000));
  const told = [];
  const { controller, conn, store, codes } = await setUp(t, { notAdded: (host, why) => told.push(why) });
  controller.expireCodes = true;
  conn.start();
  conn.enrol(controller.url, null, pin);
  await until(() => told.length === 1, 15_000);
  assert.deepEqual(told, ["nobody added it within an hour"]);
  assert.equal(codes.filter(([code]) => code).length, CODES_MOST);
  assert.equal(controller.hellos.length, CODES_MOST + 1);
  assert.equal(store.files["controller.json"], "{}");

  // Starting again doesn't make the hour longer: since when is kept.
  const later = await setUp(t, { notAdded: (host, why) => told.push(why) });
  later.store.writeJson("controller.json", { url: address(later.controller.url), pin, enrolled: false, since: Date.now() - 3601_000 });
  later.conn.controller = later.store.readJson("controller.json");
  later.conn.start();
  await until(() => told.length === 2);
  assert.equal(later.codes.filter(([code]) => code).length, 0);
});

test("connecting without an address: the one the build was made with, or none", async (t) => {
  const { controller, conn, store } = await setUp(t, { builtIn: () => "wss://127.0.0.1:1/api/pleatview/v2" });
  conn.enrol(null);
  assert.equal(JSON.parse(store.files["controller.json"]).url, "wss://127.0.0.1:1/api/pleatview/v2");
  const none = await setUp(t);
  assert.throws(() => none.conn.enrol(""), /this build has no controller's address/);
  assert.ok(controller);
});

test("the built-in address is kept encoded, and a build without one has none", () => {
  const given = "wss://devices.example/api/pleatview/v2";
  const encoded = encodeAddress(given);
  assert.ok(!Buffer.from(encoded, "base64").toString().includes("devices"));
  assert.ok(!encoded.includes("devices"));
  assert.equal(decode(encoded), given);
  const dir = mkdtempSync(join(tmpdir(), "pleatview-builtin-"));
  try {
    assert.equal(builtIn(join(dir, "builtin")), null);
    writeFileSync(join(dir, "builtin"), encoded + "\n");
    assert.equal(builtIn(join(dir, "builtin")), given);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("the question on the screen: answered, timed out, withdrawn, or replaced", async () => {
  let changes = 0;
  const question = new Question(() => changes++);
  const allowed = question.ask("Acme", "devices.example", { waitMs: 5000 });
  assert.equal(question.pending.organisation, "Acme");
  assert.equal(question.answer(true, "v2-other"), false); // not this question
  assert.equal(question.answer(true, question.pending.id), true);
  assert.equal(await allowed, true);
  assert.equal(question.answer(true), false); // nothing asked

  assert.equal(await question.ask("Acme", "h", { waitMs: 20 }), null);
  const stop = new AbortController();
  const withdrawn = question.ask("Acme", "h", { signal: stop.signal });
  stop.abort();
  assert.equal(await withdrawn, null);
  const first = question.ask("Acme", "h");
  const second = question.ask("Globex", "h");
  assert.equal(await first, null);
  question.answer(false);
  assert.equal(await second, false);
  assert.equal(question.pending, null);
  assert.ok(changes >= 8);
});
