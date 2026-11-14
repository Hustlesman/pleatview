// The local control API, as in the Android app (Api.kt and HttpServer.kt). The wire format
// is documented in docs/protocol.md; the Home Assistant integration is its client.
import { createServer } from "node:http";
import { isIP } from "node:net";
import { BadCommand, parseCommand } from "./command.js";

export const API_VERSION = 1;
const MAX_BODY_BYTES = 256 * 1024;
const MAX_STREAMS = 8;
const PING_MS = 20_000;
const NONCE = /^[0-9a-f]{32,128}$/;

class HttpError extends Error {
  constructor(status, message, extra = {}) {
    super(message);
    this.status = status;
    this.extra = extra;
  }
}

/**
 * @param {object} backend
 * @param {() => object} backend.info `id`, `name`, `manufacturer`, `model`, `app_version`, `api_version`
 * @param {() => string} backend.stateJson
 * @param {(listener: (json: string) => void) => () => void} backend.onState adds a listener; returns its removal
 * @param {(command: object) => void} backend.dispatch
 * @param {() => import("./config.js").Config} backend.config
 * @param {(update: object) => import("./config.js").Config} backend.updateConfig throws RangeError
 * @param {(username: string, password: string, remember: boolean) => boolean} backend.submitSignIn
 * @param {() => object[]} backend.signIns
 * @param {(site: string | null) => number} backend.forgetSignIns
 * @param {() => Buffer} backend.setupPage
 * @param {() => void} [backend.approvalRequested] someone asked to pair without the PIN
 * @param {() => Promise<object>} [backend.diagnostics] upkeep's report (docs/protocol.md, "Upkeep")
 * @param {import("./pairing.js").Pairing} pairing
 */
/** A refused call made over protocol v2's connection, with v1's reason. */
export class CallError extends Error {}

/**
 * Protocol v2's calls (docs/protocol-v2.md): v1's endpoints without HTTP, for a controller
 * that already proved who it is over its connection. Throws CallError with v1's reason.
 */
export function createCalls(backend, pairing) {
  return async (method, params) => {
    switch (method) {
      case "info":
        return { ...backend.info(), paired: pairing.isPaired, pairing: ["pin", "approve"] };
      case "state":
        return JSON.parse(backend.stateJson());
      case "command": {
        try {
          backend.dispatch(parseCommand(params));
        } catch (e) {
          if (e instanceof BadCommand) throw new CallError(e.message);
          throw e;
        }
        return { ok: true };
      }
      case "diagnostics":
        if (!backend.diagnostics) throw new CallError(`unknown method ${method}`);
        return backend.diagnostics();
      case "config":
        return backend.config().toJson();
      case "set_config":
        try {
          return backend.updateConfig(params).toJson();
        } catch (e) {
          if (e instanceof RangeError) throw new CallError(e.message);
          throw e;
        }
      case "signins":
        return { sign_ins: backend.signIns() };
      case "forget_signins": {
        const site = params.site;
        return { forgotten: backend.forgetSignIns(typeof site === "string" && site.trim() ? site.trim() : null) };
      }
      case "signin": {
        const password = typeof params.password === "string" ? params.password : "";
        if (!password) throw new CallError("'password' is required");
        const username = typeof params.username === "string" ? params.username.trim() : "";
        if (!backend.submitSignIn(username, password, params.remember !== false)) throw new CallError("no_sign_in_pending");
        return { ok: true };
      }
      default:
        throw new CallError(`unknown method ${method}`);
    }
  };
}

// Requests name the screen by its address, not another site's name: a web page elsewhere that
// points its own name at the screen (DNS rebinding) is refused (docs/protocol.md).
const LOCAL_SUFFIXES = [".local", ".lan", ".home", ".home.arpa", ".internal"];

/**
 * Changes come from the screen's own pages or from programs, never from another site's page:
 * a browser names the page that sent a request (Origin), so one from elsewhere is refused, and
 * bodies are JSON (a page can't send that to another site without asking first). Even a page
 * shown on this very screen can't change its settings or pair it (docs/protocol.md).
 * @returns {[number, string] | null}
 */
export function crossSite(method, headers) {
  if (!["POST", "PUT", "PATCH", "DELETE"].includes(method)) return null;
  const origin = String(headers.origin ?? "").replace(/\/$/, "");
  if (origin && origin !== `http://${headers.host ?? ""}`) return [403, "cross_site"];
  if (Number(headers["content-length"] ?? 0) && !String(headers["content-type"] ?? "").toLowerCase().startsWith("application/json")) {
    return [415, "json_required"];
  }
  return null;
}

export function hostAllowed(value) {
  let host = String(value ?? "").trim().toLowerCase();
  if (!host) return true; // HTTP/1.0 clients send none; browsers always do
  if (host.startsWith("[")) host = host.slice(1, host.includes("]") ? host.indexOf("]") : undefined);
  else if ((host.match(/:/g) || []).length === 1) host = host.slice(0, host.lastIndexOf(":"));
  if (isIP(host)) return true;
  host = host.replace(/\.$/, "");
  return host === "localhost" || !host.includes(".") || LOCAL_SUFFIXES.some((suffix) => host.endsWith(suffix));
}

export function createApi(backend, pairing, { local = (req) => LOOPBACK.has(req.socket.remoteAddress ?? "") } = {}) {
  let openStreams = 0;

  async function route(req, res) {
    if (!hostAllowed(req.headers.host)) throw new HttpError(421, "misdirected");
    const refusal = crossSite(req.method, req.headers);
    if (refusal) throw new HttpError(...refusal);
    const url = new URL(req.url ?? "/", "http://screen");
    const path = url.pathname.replace(/\/+$/, "") || "/";
    switch (path) {
      case "/":
      case "/setup":
        return get(req, () => send(res, 200, backend.setupPage(), "text/html; charset=utf-8"));
      case "/api/info":
        return get(req, () => json(res, 200, { ...backend.info(), paired: pairing.isPaired, pairing: ["pin", "approve"] }));
      case "/api/pair":
        return post(req, async () => pair(res, await body(req)));
      // Pairing approved with OK on the screen instead of the PIN.
      case "/api/pair/request":
        return post(req, async () => {
          const request = await body(req);
          const approval = pairing.requestApproval(typeof request.client === "string" ? request.client : "client",
            (req.socket.remoteAddress ?? "?").replace(/^::ffff:/, ""));
          if (!approval) throw new HttpError(429, "busy", { retry_after: 30 });
          backend.approvalRequested?.();
          json(res, 202, { request: approval.id, expires_in: Math.max(1, Math.round((approval.expires - Date.now()) / 1000)) });
        });
      // Lets Home Assistant check that a screen announcing itself at a new address is the one
      // it paired with, before it sends that address its token.
      case "/api/verify":
        return post(req, async () => {
          const nonce = (await body(req)).nonce;
          if (typeof nonce !== "string" || !NONCE.test(nonce)) throw new HttpError(400, "'nonce' must be 32 to 128 hex digits");
          json(res, 200, { proofs: pairing.prove(nonce) });
        });
      // The phone that scanned the sign-in code has the PIN instead of a token.
      case "/api/signin":
        return post(req, async () => signIn(req, res, await body(req)));
      // Connect out to a controller elsewhere, which then shows a code to add the screen there
      // with (protocol v2): from this computer (pleatview-kiosk connect URL), from the phone
      // page with the PIN, or from a paired client.
      case "/api/connect":
        return post(req, async () => {
          const request = await body(req);
          if (!backend.connectOut) throw new HttpError(404, "not found");
          if (!local(req) && !pairing.isValid(bearer(req))) {
            const result = pairing.verify(typeof request.pin === "string" ? request.pin : "");
            if (result.result !== "verified") refused(result);
          }
          const url = request.url;
          try {
            backend.connectOut(typeof url === "string" ? url : "");
          } catch (e) {
            if (e instanceof RangeError) throw new HttpError(400, e.message);
            throw e;
          }
          json(res, 202, {});
        });
    }
    if (path.startsWith("/api/pair/request/")) {
      return get(req, () => {
        const result = pairing.collect(path.slice("/api/pair/request/".length));
        if (!result) throw new HttpError(404, "no_such_request");
        json(res, 200, result.token ? { state: result.state, token: result.token } : { state: result.state });
      });
    }
    if (!path.startsWith("/api/")) throw new HttpError(404, "not found");
    if (!pairing.isValid(bearer(req))) throw new HttpError(401, "unauthorized");
    switch (path) {
      case "/api/state":
        return get(req, () => send(res, 200, backend.stateJson(), "application/json"));
      case "/api/events":
        return get(req, () => events(req, res));
      case "/api/diagnostics":
        if (!backend.diagnostics) throw new HttpError(404, "not found");
        return get(req, async () => json(res, 200, await backend.diagnostics()));
      case "/api/command":
        return post(req, async () => {
          const request = await body(req);
          try {
            backend.dispatch(parseCommand(request));
          } catch (e) {
            if (e instanceof BadCommand) throw new HttpError(400, e.message);
            throw e;
          }
          ok(res);
        });
      case "/api/config":
        if (req.method === "GET" || req.method === "HEAD") return json(res, 200, backend.config().toJson());
        if (req.method === "POST") {
          const update = await body(req);
          let config;
          try {
            config = backend.updateConfig(update);
          } catch (e) {
            if (e instanceof RangeError) throw new HttpError(400, e.message);
            throw e;
          }
          return json(res, 200, config.toJson());
        }
        throw new HttpError(405, "method not allowed");
      case "/api/signins":
        if (req.method === "GET" || req.method === "HEAD") return json(res, 200, { sign_ins: backend.signIns() });
        if (req.method === "DELETE") {
          const site = (await body(req)).site;
          return json(res, 200, { forgotten: backend.forgetSignIns(typeof site === "string" && site.trim() ? site.trim() : null) });
        }
        throw new HttpError(405, "method not allowed");
      // A controller paired over v1 moves this screen to protocol v2 (docs/protocol-v2.md).
      case "/api/enrol":
        return post(req, async () => {
          const request = await body(req);
          const { url, token, fingerprint } = request;
          if (typeof url !== "string" || typeof token !== "string" || !token || (fingerprint != null && typeof fingerprint !== "string")) {
            throw new HttpError(400, "'url' and 'token' are required");
          }
          if (!backend.enrol) throw new HttpError(404, "not found");
          try {
            backend.enrol(url, token, fingerprint ?? null);
          } catch (e) {
            if (e instanceof RangeError) throw new HttpError(400, e.message);
            throw e;
          }
          json(res, 202, {});
        });
      case "/api/unpair":
        return post(req, () => {
          pairing.revoke(bearer(req) ?? "");
          ok(res);
        });
      default:
        throw new HttpError(404, "not found");
    }
  }

  function pair(res, request) {
    if (typeof request.pin !== "string" || !request.pin.trim()) throw new HttpError(400, "'pin' is required");
    const result = pairing.pair(request.pin, typeof request.client === "string" ? request.client : "client");
    if (result.result === "paired") return json(res, 200, { token: result.token });
    refused(result);
  }

  function signIn(req, res, request) {
    if (!pairing.isValid(bearer(req))) {
      const result = pairing.verify(typeof request.pin === "string" ? request.pin : "");
      if (result.result !== "verified") refused(result);
    }
    const password = typeof request.password === "string" ? request.password : "";
    if (!password) throw new HttpError(400, "'password' is required");
    const username = typeof request.username === "string" ? request.username.trim() : "";
    const accepted = backend.submitSignIn(username, password, request.remember !== false);
    if (!accepted) throw new HttpError(409, "no_sign_in_pending");
    ok(res);
  }

  /** Server-Sent Events: the full state on connect and after every change, pings in between. */
  function events(req, res) {
    if (openStreams >= MAX_STREAMS) throw new HttpError(503, "too many event streams");
    openStreams++;
    res.writeHead(200, { ...HEADERS, "Content-Type": "text/event-stream; charset=utf-8", "X-Accel-Buffering": "no" });
    const sendState = (state) => res.write(`event: state\ndata: ${state}\n\n`);
    res.write("retry: 3000\n\n");
    sendState(backend.stateJson());
    const remove = backend.onState(sendState);
    const ping = setInterval(() => res.write(": ping\n\n"), PING_MS);
    req.socket.setTimeout(0);
    res.on("close", () => {
      clearInterval(ping);
      remove();
      openStreams--;
    });
  }

  const server = createServer({ maxHeaderSize: 16 * 1024, requestTimeout: 30_000 }, async (req, res) => {
    try {
      await route(req, res);
    } catch (e) {
      if (res.headersSent) return res.destroy();
      if (e instanceof HttpError) return json(res, e.status, { error: e.message, ...e.extra });
      console.error("PleatView API:", e);
      json(res, 500, { error: "internal error" });
    }
  });
  server.keepAliveTimeout = 0;
  return server;
}

function refused(result) {
  if (result.result === "locked_out") throw new HttpError(429, "too_many_attempts", { retry_after: result.retryAfter });
  throw new HttpError(403, "invalid_pin");
}

function bearer(req) {
  const header = req.headers.authorization ?? "";
  return /^bearer /i.test(header) ? header.slice(7).trim() : null;
}

async function get(req, handler) {
  if (req.method !== "GET" && req.method !== "HEAD") throw new HttpError(405, "method not allowed");
  return handler();
}

async function post(req, handler) {
  if (req.method !== "POST") throw new HttpError(405, "method not allowed");
  return handler();
}

/** The JSON body: needs a Content-Length, at most 256 KiB. An empty body is `{}`. */
async function body(req) {
  if (req.headers["transfer-encoding"]) throw new HttpError(411, "Content-Length required");
  const length = Number(req.headers["content-length"] ?? 0);
  if (!Number.isInteger(length) || length < 0) throw new HttpError(400, "bad Content-Length");
  if (length > MAX_BODY_BYTES) throw new HttpError(413, "body too large");
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > MAX_BODY_BYTES) throw new HttpError(413, "body too large");
    chunks.push(chunk);
  }
  if (size === 0) return {};
  let value;
  try {
    value = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  } catch {
    throw new HttpError(400, "invalid JSON");
  }
  if (value === null || typeof value !== "object" || Array.isArray(value)) throw new HttpError(400, "expected a JSON object");
  return value;
}

const LOOPBACK = new Set(["127.0.0.1", "::1", "::ffff:127.0.0.1"]);

const HEADERS = { "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", Connection: "close" };

function send(res, status, payload, contentType) {
  const data = Buffer.isBuffer(payload) ? payload : Buffer.from(payload, "utf8");
  res.writeHead(status, { ...HEADERS, "Content-Type": contentType, "Content-Length": data.length });
  res.end(res.req?.method === "HEAD" ? undefined : data);
}

const json = (res, status, value) => send(res, status, JSON.stringify(value), "application/json");
const ok = (res) => json(res, 200, { ok: true });
