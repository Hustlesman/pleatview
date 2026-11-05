import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { test } from "node:test";
import { LOCKOUT_MS, MAX_FAILURES, MAX_TOKENS, Pairing, sha256 } from "../src/pairing.js";

function pairing({ stored = null, clock = () => 0 } = {}) {
  const saved = [];
  const p = new Pairing({ load: () => stored, save: (json) => saved.push(json), clock });
  return { p, saved };
}

test("the right PIN pairs, issues a token and changes the PIN", () => {
  const { p, saved } = pairing();
  const pin = p.pin;
  assert.match(pin, /^\d{6}$/);
  const result = p.pair(pin.slice(0, 3) + " " + pin.slice(3), "Home Assistant");
  assert.equal(result.result, "paired");
  assert.match(result.token, /^[0-9a-f]{64}$/);
  assert.ok(p.isValid(result.token));
  assert.notEqual(p.pin, pin);
  // Only the token's hash is stored.
  assert.ok(!saved.at(-1).includes(result.token));
  assert.ok(saved.at(-1).includes(sha256(result.token)));
});

test("wrong PINs lock pairing for a minute and change the PIN", () => {
  let now = 1_000;
  const { p } = pairing({ clock: () => now });
  const pin = p.pin;
  for (let i = 0; i < MAX_FAILURES - 1; i++) assert.equal(p.pair("000000" === pin ? "111111" : "000000", "x").result, "invalid_pin");
  assert.equal(p.pin, pin);
  p.pair("bad", "x");
  assert.notEqual(p.pin, pin);
  assert.deepEqual(p.pair(p.pin, "x"), { result: "locked_out", retryAfter: 60 });
  now += LOCKOUT_MS;
  assert.equal(p.pair(p.pin, "x").result, "paired");
});

test("keeps the newest tokens and reloads them", () => {
  const { p, saved } = pairing();
  const tokens = [];
  for (let i = 0; i < MAX_TOKENS + 1; i++) tokens.push(p.pair(p.pin, `client ${i}`).token);
  assert.ok(!p.isValid(tokens[0]));
  assert.ok(p.isValid(tokens.at(-1)));
  const reloaded = pairing({ stored: saved.at(-1) }).p;
  assert.ok(reloaded.isValid(tokens.at(-1)));
  assert.ok(!reloaded.isValid("not-a-token"));
  reloaded.revoke(tokens.at(-1));
  assert.ok(!reloaded.isValid(tokens.at(-1)));
});

test("verify uses up the PIN without pairing anyone", () => {
  const { p } = pairing();
  const pin = p.pin;
  assert.deepEqual(p.verify(pin), { result: "verified" });
  assert.notEqual(p.pin, pin);
  assert.ok(!p.isPaired);
});

test("proofs match the protocol's test vector", () => {
  const { p } = pairing({ stored: JSON.stringify([{ hash: sha256("token-1"), client: "HA", created: 0 }]) });
  assert.deepEqual(p.prove("00112233445566778899aabbccddeeff"), [
    "cfd2cb0b0606646c50484f870216c1870afd7f225109845b6435fdfd40887c7e",
  ]);
  // What a client computes from its own token.
  const client = createHmac("sha256", sha256("token-1")).update("00112233445566778899aabbccddeeff").digest("hex");
  assert.equal(client, p.prove("00112233445566778899aabbccddeeff")[0]);
});

test("unreadable stored records start unpaired", () => {
  assert.ok(!pairing({ stored: "{not json" }).p.isPaired);
});
