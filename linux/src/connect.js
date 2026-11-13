// Protocol v2 (docs/protocol-v2.md): the screen connects out to a controller over TLS, signs
// in with its own key and answers v1's calls over that one connection. Node's crypto and tls
// only: no extra packages.

import { createHash, createPrivateKey, createPublicKey, generateKeyPairSync, sign } from "node:crypto";
import { connect as wsConnect } from "./websocket.js";

export const SUBPROTOCOL = "pleatview.v2";
export const PATH = "/api/pleatview/v2";
const NONCE = /^[0-9a-f]{64}$/;
const PIN = /^[0-9a-f]{64}$/;
const PING_MS = 20_000;
const SILENCE_MS = 60_000;
const FIRST_WAIT_MS = 2000;
const LONGEST_WAIT_MS = 60_000;
const LONGEST_RETRY_S = 3600;
export const MOVE_TRIES = 3;
export const timing = { firstWaitMs: FIRST_WAIT_MS };
export const CLOSE = { UNKNOWN_KEY: 4401, REVOKED: 4403, CODE_EXPIRED: 4408, REPLACED: 4409 };

/** A controller's address as signatures name it: wss, host in lower case, no :443. */
export function address(given) {
  let url;
  try {
    url = new URL(String(given).trim());
  } catch {
    throw new RangeError("a controller's address is wss://host[:port]/api/pleatview/v2");
  }
  if (url.protocol !== "wss:" || !url.hostname) throw new RangeError("a controller's address is wss://host[:port]/api/pleatview/v2");
  const port = url.port && url.port !== "443" ? `:${url.port}` : "";
  return `wss://${url.hostname.toLowerCase()}${port}${PATH}`;
}

export const signedText = (controller, nonce) => `pleatview-v2\n${controller}\n${nonce}`;

export class Connection {
  /**
   * @param {object} screen
   * @param {import("./store.js").Store} screen.store keeps the key and the controller
   * @param {() => object} screen.info what v1's /api/info says
   * @param {(method: string, params: object) => Promise<object>} screen.call a v1 call; throws {error}
   * @param {() => string} screen.stateJson
   * @param {(listener: (json: string) => void) => () => void} screen.onState
   * @param {(code: string | null, host: string) => void} screen.showCode
   */
  constructor({ store, info, call, stateJson, onState, showCode }) {
    Object.assign(this, { store, info, call, stateJson, onState, showCode });
    this.key = this.loadKey();
    const der = createPublicKey(this.key).export({ type: "spki", format: "der" });
    this.public = der.toString("base64");
    this.keyId = createHash("sha256").update(der).digest("hex");
    this.controller = store.readJson("controller.json", {});
    this.token = null;
    this.online = false;
    this.ws = null;
    this.stopped = false;
    this.wake = null;
    this.waitMs = timing.firstWaitMs;
    this.retryAfter = null; // the controller's own wait, from `retry` or a 503
    this.moving = null; // a new address, until it lets this screen in
    this.moveTries = 0;
  }

  loadKey() {
    const pem = this.store.read("v2-key.pem");
    if (pem) {
      try {
        return createPrivateKey(pem);
      } catch {
        // made again below
      }
    }
    const { privateKey } = generateKeyPairSync("ec", { namedCurve: "P-256" });
    this.store.write("v2-key.pem", privateKey.export({ type: "pkcs8", format: "pem" }));
    return privateKey;
  }

  sign(controller, nonce) {
    return sign("sha256", Buffer.from(signedText(controller, nonce)), { key: this.key, dsaEncoding: "der" }).toString("base64");
  }

  /** Connect to this controller: with a token from one paired over v1, or to show a code. */
  enrol(url, token = null, pin = null) {
    if (pin !== null && (typeof pin !== "string" || !PIN.test(pin.toLowerCase()))) {
      throw new RangeError("fingerprint is the hex of the certificate's SHA-256");
    }
    this.save({ url: address(url), pin: pin ? pin.toLowerCase() : null, enrolled: false });
    this.token = token;
    this.restart();
  }

  forget() {
    this.save({});
    this.token = null;
    this.showCode(null, "");
  }

  save(controller) {
    this.controller = controller;
    this.store.writeJson("controller.json", controller);
  }

  restart() {
    this.waitMs = timing.firstWaitMs;
    this.ws?.close(1000);
    this.wake?.();
  }

  start() {
    this.run().catch((e) => console.error("PleatView: the connection out stopped:", e));
  }

  stop() {
    this.stopped = true;
    this.ws?.close(1001);
    this.wake?.();
  }

  /** The controller has a new address: try it, keeping the old one until it lets us in. */
  move(message) {
    let url;
    try {
      url = address(String(message.url ?? ""));
    } catch {
      return console.warn("PleatView: ignoring a move to an address that isn't wss://");
    }
    const pin = message.fingerprint ?? null;
    if (pin !== null && (typeof pin !== "string" || !PIN.test(pin.toLowerCase()))) return;
    if (url === this.controller.url) return;
    this.moving = { url, pin: pin ? pin.toLowerCase() : null };
    this.moveTries = 0;
    console.log(`PleatView: the controller asks this screen to use ${url}`);
    this.ws?.close(1000);
  }

  /** A move ends: kept once the new address let this screen in, given up after three tries. */
  moved(ok) {
    if (ok) {
      console.log(`PleatView: the controller moved to ${this.moving.url}`);
      this.save({ ...this.controller, url: this.moving.url, pin: this.moving.pin });
    } else if (++this.moveTries >= MOVE_TRIES) {
      console.warn(`PleatView: the controller's new address ${this.moving.url} didn't let this screen in; staying at ${this.controller.url}`);
    } else {
      return;
    }
    this.moving = null;
  }

  async run() {
    while (!this.stopped) {
      if (!this.controller.url) {
        await this.sleep(Infinity);
        continue;
      }
      const moving = this.moving;
      let code = null;
      try {
        code = await this.session();
      } catch (e) {
        if (Number.isInteger(e.retryAfter)) this.retryAfter = e.retryAfter;
        else console.log(`PleatView: can't reach the controller at ${(this.moving ?? this.controller).url}: ${e.message}`);
      }
      this.online = false;
      if (moving && this.moving === moving) {
        // Whatever the new address said, 4401, 4403 and 4409 included, the old one stands.
        this.moved(false);
        this.waitMs = timing.firstWaitMs;
      } else if (this.moving && !moving) {
        continue; // just told to move: go now
      } else if (code === CLOSE.UNKNOWN_KEY || code === CLOSE.REVOKED) {
        console.log(`PleatView: the controller removed this screen (${code})`);
        this.forget();
        continue;
      } else if (code === CLOSE.REPLACED) {
        await this.sleep(Infinity);
        continue;
      } else if (code === CLOSE.CODE_EXPIRED) {
        this.waitMs = timing.firstWaitMs;
      }
      if (this.retryAfter) {
        await this.sleep(1000 * (1 + Math.random() * (Math.min(this.retryAfter, LONGEST_RETRY_S) - 1)));
        this.retryAfter = null;
      } else {
        await this.sleep(this.waitMs * (0.5 + Math.random() * 0.5));
      }
      this.waitMs = Math.min(this.waitMs * 2, LONGEST_WAIT_MS);
    }
  }

  sleep(ms) {
    return new Promise((resolve) => {
      const timer = Number.isFinite(ms) ? setTimeout(done, ms) : null;
      function done() {
        clearTimeout(timer);
        resolve();
      }
      this.wake = done;
    });
  }

  /** One connection, from the challenge until it closes; resolves to the close code. */
  async session() {
    const target = this.moving ?? this.controller;
    const url = target.url;
    const ws = await wsConnect(url, { subprotocol: SUBPROTOCOL, pin: target.pin });
    this.ws = ws;
    return new Promise((resolve) => {
      let welcomed = false;
      let heard = Date.now();
      let removeListener = null;
      const timers = [];
      const greet = setTimeout(() => ws.close(1001), 15_000);
      ws.on("close", (code) => {
        clearTimeout(greet);
        timers.forEach(clearInterval);
        removeListener?.();
        if (this.ws === ws) this.ws = null;
        resolve(code);
      });
      ws.on("message", (message) => {
        heard = Date.now();
        if (message.type === "challenge") {
          if (typeof message.nonce !== "string" || !NONCE.test(message.nonce)) return ws.close(4400);
          clearTimeout(greet);
          const hello = { signature: this.sign(url, message.nonce), info: this.info() };
          if (this.controller.enrolled) return ws.send({ type: "auth", key: this.keyId, ...hello });
          return ws.send({ type: "enrol", key: this.public, ...hello, ...(this.token ? { token: this.token } : {}) });
        }
        if (message.type === "code") return this.showCode(String(message.code ?? ""), new URL(url).hostname);
        if (message.type === "ping") return ws.send({ type: "pong" });
        if (message.type === "welcome" && !welcomed) {
          welcomed = true;
          if (this.moving && this.moving.url === url) this.moved(true);
          if (!this.controller.enrolled) {
            this.save({ ...this.controller, enrolled: true });
            this.token = null;
            this.showCode(null, "");
            console.log(`PleatView: enrolled with the controller at ${url}`);
          }
          this.online = true;
          this.waitMs = timing.firstWaitMs;
          ws.send({ type: "state", state: JSON.parse(this.stateJson()) });
          removeListener = this.onState((json) => {
            if (!ws.closed) ws.send({ type: "state", state: JSON.parse(json) });
          });
          timers.push(setInterval(() => {
            if (Date.now() - heard > SILENCE_MS) return ws.close(1001);
            if (!ws.closed) ws.send({ type: "ping" });
          }, PING_MS));
          return;
        }
        if (message.type === "request" && welcomed) return this.answer(ws, message);
        if (message.type === "retry" && welcomed) {
          const after = message.after;
          if (Number.isInteger(after) && after >= 1 && after <= LONGEST_RETRY_S) this.retryAfter = after;
          return;
        }
        if (message.type === "move" && welcomed) return this.move(message);
      });
    });
  }

  async answer(ws, request) {
    const params = request.params && typeof request.params === "object" && !Array.isArray(request.params) ? request.params : {};
    let reply;
    try {
      reply = { result: await this.call(String(request.method), params) };
    } catch (e) {
      reply = { error: e?.error ?? e?.message ?? String(e) };
    }
    if (!ws.closed) ws.send({ type: "response", id: request.id, ...reply });
  }
}
