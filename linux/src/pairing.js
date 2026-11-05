// PIN-based pairing, as in the Android app (Pairing.kt). The PIN is shown on the screen;
// a client that knows it receives a bearer token for the control API. Only SHA-256
// hashes of tokens are stored.
import { createHash, createHmac, randomBytes, randomInt, timingSafeEqual } from "node:crypto";

export const PIN_LENGTH = 6;
export const MAX_FAILURES = 5;
export const LOCKOUT_MS = 60_000;
export const MAX_TOKENS = 8;

export const sha256 = (value) => createHash("sha256").update(value, "utf8").digest("hex");

/** Constant-time comparison of two strings of any length. */
const same = (a, b) => timingSafeEqual(createHash("sha256").update(a).digest(), createHash("sha256").update(b).digest());

export class Pairing {
  /**
   * @param {object} options
   * @param {() => string | null} options.load stored records (JSON), if any
   * @param {(json: string) => void} options.save
   * @param {() => number} [options.clock] milliseconds
   * @param {() => void} [options.onChanged] the PIN or the paired clients changed
   */
  constructor({ load, save, clock = Date.now, onChanged = () => {} }) {
    this.save = save;
    this.clock = clock;
    this.onChanged = onChanged;
    this.records = [];
    this.failures = 0;
    this.lockedUntil = 0;
    this.pin = newPin();
    try {
      const stored = JSON.parse(load() ?? "[]");
      for (const r of Array.isArray(stored) ? stored : []) {
        if (typeof r?.hash === "string") this.records.push({ hash: r.hash, client: String(r.client ?? ""), created: Number(r.created) || 0 });
      }
    } catch {
      // Unreadable records: start unpaired, as Android does.
    }
  }

  get isPaired() {
    return this.records.length > 0;
  }

  /** @returns {{result: "paired", token: string} | {result: "invalid_pin"} | {result: "locked_out", retryAfter: number}} */
  pair(candidate, client) {
    const refused = this.checkPin(candidate);
    if (refused) return refused;
    const token = randomBytes(32).toString("hex");
    this.records.push({ hash: sha256(token), client: String(client ?? "").trim().slice(0, 64) || "client", created: this.clock() });
    while (this.records.length > MAX_TOKENS) this.records.shift();
    this.persist();
    this.rotatePin();
    return { result: "paired", token };
  }

  /** Uses up the PIN for a one-off action, such as signing in to a site, without pairing a client. */
  verify(candidate) {
    const refused = this.checkPin(candidate);
    if (refused) return refused;
    this.rotatePin();
    return { result: "verified" };
  }

  /** null when the candidate is the PIN. Wrong PINs count towards a lockout, which also changes the PIN. */
  checkPin(candidate) {
    const now = this.clock();
    if (now < this.lockedUntil) return { result: "locked_out", retryAfter: Math.ceil((this.lockedUntil - now) / 1000) };
    const digits = String(candidate ?? "").replace(/\D/g, "");
    if (!same(digits, this.pin)) {
      this.failures++;
      if (this.failures >= MAX_FAILURES) {
        this.failures = 0;
        this.lockedUntil = now + LOCKOUT_MS;
        this.rotatePin();
      }
      return { result: "invalid_pin" };
    }
    this.failures = 0;
    return null;
  }

  isValid(token) {
    if (!token) return false;
    const hash = sha256(token);
    // Compare against every record so timing doesn't reveal which one matched.
    let match = false;
    for (const record of this.records) match = same(record.hash, hash) || match;
    return match;
  }

  /**
   * Proves this is the screen a client paired with, without the client sending its token:
   * one HMAC-SHA256 of the nonce per paired client, keyed with the stored hash of its token.
   */
  prove(nonce) {
    return this.records.map((r) => createHmac("sha256", Buffer.from(r.hash, "utf8")).update(nonce, "utf8").digest("hex"));
  }

  revoke(token) {
    const hash = sha256(String(token ?? ""));
    const before = this.records.length;
    this.records = this.records.filter((r) => r.hash !== hash);
    if (this.records.length !== before) this.persist();
    this.onChanged();
  }

  revokeAll() {
    this.records = [];
    this.persist();
    this.rotatePin();
  }

  rotatePin() {
    this.pin = newPin();
    this.onChanged();
  }

  persist() {
    this.save(JSON.stringify(this.records));
  }
}

function newPin() {
  let pin = "";
  for (let i = 0; i < PIN_LENGTH; i++) pin += randomInt(10);
  return pin;
}
