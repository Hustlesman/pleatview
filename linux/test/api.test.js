import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { createApi } from "../src/api.js";
import { Config } from "../src/config.js";
import { Pairing } from "../src/pairing.js";

const commands = [];
const listeners = new Set();
let config = new Config();
let pendingSignIn = false;
const pairing = new Pairing({ load: () => null, save: () => {} });
const backend = {
  info: () => ({ id: "abc", name: "Kitchen screen", manufacturer: "PleatView", model: "Linux", app_version: "1.0.8", api_version: 1 }),
  stateJson: () => JSON.stringify({ power: "on", commands: commands.length }),
  onState: (listener) => (listeners.add(listener), () => listeners.delete(listener)),
  dispatch: (command) => commands.push(command),
  config: () => config,
  updateConfig: (update) => (config = config.merge(update)),
  submitSignIn: () => pendingSignIn,
  signIns: () => [{ kind: "form", site: "http://grafana.lan", username: "kiosk" }],
  forgetSignIns: (site) => (site ? 1 : 2),
  setupPage: () => Buffer.from("<!doctype html><title>Setup</title>"),
};
const server = createApi(backend, pairing);
let base;

before(async () => {
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  base = `http://127.0.0.1:${server.address().port}`;
});
after(() => server.close());

async function call(method, path, body, token) {
  const res = await fetch(base + path, {
    method,
    headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  const type = res.headers.get("content-type") ?? "";
  return { status: res.status, body: type.startsWith("application/json") ? await res.json() : await res.text(), headers: res.headers };
}

async function paired() {
  return (await call("POST", "/api/pair", { pin: pairing.pin, client: "test" })).body.token;
}

test("info and the setup page need no token", async () => {
  const info = await call("GET", "/api/info");
  assert.equal(info.status, 200);
  assert.equal(info.body.name, "Kitchen screen");
  assert.equal(typeof info.body.paired, "boolean");
  assert.equal(info.headers.get("cache-control"), "no-store");
  const page = await call("GET", "/");
  assert.match(page.body, /<title>Setup<\/title>/);
});

test("pairing: wrong PIN, missing PIN, then the right one", async () => {
  assert.deepEqual(await call("POST", "/api/pair", { pin: "x" }).then((r) => [r.status, r.body]), [403, { error: "invalid_pin" }]);
  assert.equal((await call("POST", "/api/pair", {})).status, 400);
  assert.equal((await call("GET", "/api/pair")).status, 405);
  const token = await paired();
  assert.match(token, /^[0-9a-f]{64}$/);
});

test("everything else needs a token", async () => {
  assert.equal((await call("GET", "/api/state")).status, 401);
  assert.equal((await call("GET", "/api/state", null, "nope")).status, 401);
  const token = await paired();
  assert.equal((await call("GET", "/api/state", null, token)).body.power, "on");
  assert.equal((await call("GET", "/api/nothing", null, token)).status, 404);
  assert.equal((await call("GET", "/elsewhere")).status, 404);
});

test("commands are validated, then dispatched", async () => {
  const token = await paired();
  const bad = await call("POST", "/api/command", { command: "explode" }, token);
  assert.deepEqual([bad.status, bad.body], [400, { error: "unknown command 'explode'" }]);
  assert.deepEqual((await call("POST", "/api/command", { command: "notify", message: "Hi" }, token)).body, { ok: true });
  assert.equal(commands.at(-1).name, "notify");
});

test("config: partial updates, invalid values and secrets", async () => {
  const token = await paired();
  const updated = await call("POST", "/api/config", { ha_url: "http://ha:8123", ha_token: "secret" }, token);
  assert.equal(updated.status, 200);
  assert.equal(updated.body.ha_token_set, true);
  assert.ok(!("ha_token" in updated.body));
  const invalid = await call("POST", "/api/config", { zoom: 5 }, token);
  assert.deepEqual([invalid.status, invalid.body.error], [400, "zoom must be between 25 and 400"]);
  assert.equal((await call("POST", "/api/config", [1, 2], token)).status, 400);
});

test("verify answers with proofs for paired clients", async () => {
  await paired();
  assert.equal((await call("POST", "/api/verify", { nonce: "xyz" })).status, 400);
  const proofs = (await call("POST", "/api/verify", { nonce: "00112233445566778899aabbccddeeff" })).body.proofs;
  assert.ok(proofs.length >= 1 && proofs.every((p) => /^[0-9a-f]{64}$/.test(p)));
});

test("sign-ins from the phone page take the PIN instead of a token", async () => {
  pendingSignIn = false;
  assert.equal((await call("POST", "/api/signin", { pin: pairing.pin, password: "pw" })).status, 409);
  pendingSignIn = true;
  assert.equal((await call("POST", "/api/signin", { pin: pairing.pin })).status, 400);
  assert.equal((await call("POST", "/api/signin", { pin: pairing.pin, username: "kiosk", password: "pw" })).status, 200);
  const token = await paired();
  assert.equal((await call("GET", "/api/signins", null, token)).body.sign_ins[0].username, "kiosk");
  assert.deepEqual((await call("DELETE", "/api/signins", { site: "http://grafana.lan" }, token)).body, { forgotten: 1 });
});

test("events stream the state, then every change", async () => {
  const token = await paired();
  const res = await fetch(`${base}/api/events`, { headers: { Authorization: `Bearer ${token}` } });
  assert.equal(res.headers.get("content-type"), "text/event-stream; charset=utf-8");
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let text = "";
  while (!text.includes("event: state")) text += decoder.decode((await reader.read()).value);
  for (const listener of listeners) listener('{"power":"standby"}');
  while (!text.includes('"standby"')) text += decoder.decode((await reader.read()).value);
  await reader.cancel();
});

test("bodies need a Content-Length and stay small", async () => {
  const token = await paired();
  const big = await call("POST", "/api/command", { command: "notify", message: "x".repeat(300 * 1024) }, token);
  assert.equal(big.status, 413);
});
