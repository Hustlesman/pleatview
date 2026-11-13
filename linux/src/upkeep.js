// Upkeep (docs/protocol.md, "Upkeep"): what a controller needs to look after a screen it can't
// walk up to. The app's own recent log, a few fixed checks, and asking the package's helper
// (build/pleatview-helper, the one part that runs as root) to restart the computer or install
// a newer release. As upkeep.py does in PleatView Light.
import { execFile } from "node:child_process";
import { constants, accessSync, closeSync, existsSync, openSync, readFileSync, readdirSync, statfsSync } from "node:fs";
import { lookup } from "node:dns/promises";
import { connect as netConnect } from "node:net";
import { cpus, loadavg, networkInterfaces, release, totalmem, uptime } from "node:os";
import { join } from "node:path";

export const VERSION_TEXT = /^\d{1,4}\.\d{1,4}\.\d{1,6}$/;
const UPDATE_STATES = ["waiting", "downloading", "installing", "installed", "failed"];
const CHECK_MS = 4000;

// Addresses keep their host and path; their queries (where tokens travel) are left out.
const QUERY = /(\b[a-z][a-z0-9+.-]*:\/\/[^\s?#"']*)\?[^\s"']*/gi;
const BEARER = /(Bearer\s+)\S+/gi;
export const redact = (text) => String(text).replace(QUERY, "$1?…").replace(BEARER, "$1…");

/** The app's last log lines, from console.log, warn and error, without the queries of addresses. */
export class RecentLog {
  constructor(size = 100) {
    this.size = size;
    this.lines = [];
  }

  add(level, args) {
    try {
      const text = args.map((a) => (a instanceof Error ? a.message : typeof a === "string" ? a : JSON.stringify(a))).join(" ");
      this.lines.push(redact(`${new Date().toISOString().slice(0, 19)}Z ${level} ${text}`).slice(0, 400));
      if (this.lines.length > this.size) this.lines.splice(0, this.lines.length - this.size);
    } catch {
      // a log line must never break the app
    }
  }

  /** Keeps a copy of what the app writes to the console from now on. */
  capture(target = console) {
    for (const [method, level] of [["log", "INFO"], ["info", "INFO"], ["warn", "WARNING"], ["error", "ERROR"]]) {
      const original = target[method].bind(target);
      target[method] = (...args) => {
        this.add(level, args);
        original(...args);
      };
    }
    return this;
  }
}

/** Is `version` newer than `than`? Both like 1.0.20. */
export function newer(version, than) {
  const parts = (text) => String(text).split(/\D+/).filter(Boolean).slice(0, 3).map(Number);
  const [a, b] = [parts(version), parts(than)];
  for (let i = 0; i < 3; i++) if ((a[i] ?? 0) !== (b[i] ?? 0)) return (a[i] ?? 0) > (b[i] ?? 0);
  return false;
}

/** The package's helper, through its folder in /run (sudo pleatview-kiosk upkeep sets it up). */
export class Helper {
  constructor(run = process.env.PLEATVIEW_HELPER_RUN || "/run/pleatview") {
    this.run = run;
  }

  get ready() {
    try {
      accessSync(join(this.run, "requests"), constants.W_OK | constants.X_OK);
      return true;
    } catch {
      return false;
    }
  }

  /** A request is an empty file named for it; the helper reads only the name. */
  ask(name) {
    try {
      closeSync(openSync(join(this.run, "requests", name), "wx", 0o660));
    } catch (e) {
      if (e.code !== "EEXIST") throw e;
    }
  }

  restartDevice() {
    this.ask("restart");
  }

  update(version) {
    if (!VERSION_TEXT.test(version)) throw new RangeError("version is like 1.0.20");
    this.ask(`update-${version}`);
  }

  /** How the last update went, as the helper wrote it. */
  status() {
    let data;
    try {
      data = JSON.parse(readFileSync(join(this.run, "status.json"), "utf8"));
    } catch {
      return null;
    }
    if (!data || typeof data !== "object" || !UPDATE_STATES.includes(data.state) || !VERSION_TEXT.test(String(data.version))) return null;
    return {
      version: data.version,
      state: data.state,
      error: data.error ? String(data.error).slice(0, 200) : null,
      at: Number.isInteger(data.at) ? data.at : null,
    };
  }
}

// Diagnostics

const read = (path) => {
  try {
    return readFileSync(path, "utf8");
  } catch {
    return null;
  }
};

function memory() {
  const values = {};
  for (const line of (read("/proc/meminfo") ?? "").split("\n")) {
    const [key, value] = line.split(":");
    if (value) values[key] = Number.parseInt(value, 10) * 1024;
  }
  return { total: totalmem(), available: values.MemAvailable ?? values.MemFree ?? null };
}

function temperature() {
  let zones = [];
  try {
    zones = readdirSync("/sys/class/thermal").filter((name) => name.startsWith("thermal_zone")).sort();
  } catch {
    return null;
  }
  for (const zone of zones) {
    const kind = (read(`/sys/class/thermal/${zone}/type`) ?? "").trim();
    const value = (read(`/sys/class/thermal/${zone}/temp`) ?? "").trim();
    if (/^-?\d+$/.test(value) && (/cpu|soc|x86_pkg/.test(kind) || zone === "thermal_zone0")) return Math.round(Number(value) / 100) / 10;
  }
  return null;
}

const UNDER_VOLTAGE = 0x10001; // now (bit 0), or since it started (bit 16)
const SLOWED = 0x60006; // its processor capped or throttled, now or since it started (bits 1, 2, 17, 18)

/** Raspberry Pi firmware's throttled bits: from sysfs on older kernels, else vcgencmd (the app's user is in the video group). */
async function throttledBits() {
  let text = read("/sys/devices/platform/soc/soc:firmware/get_throttled");
  if (text === null) {
    text = await new Promise((resolve) => {
      execFile("vcgencmd", ["get_throttled"], { timeout: 2000 }, (error, stdout) => resolve(error ? null : stdout));
    });
  }
  if (!text) return null;
  const value = Number.parseInt(text.trim().split("=").pop(), 16);
  return Number.isNaN(value) ? null : value;
}

/** [power_warning, throttled]; a board that only says whether its voltage is low now (the rpi_volt sensor) gives the first. */
function power(bits) {
  if (bits !== null) return [Boolean(bits & UNDER_VOLTAGE), Boolean(bits & SLOWED)];
  let sensors = [];
  try {
    sensors = readdirSync("/sys/class/hwmon");
  } catch {
    return [null, null];
  }
  for (const sensor of sensors) {
    if ((read(`/sys/class/hwmon/${sensor}/name`) ?? "").trim() !== "rpi_volt") continue;
    const alarm = (read(`/sys/class/hwmon/${sensor}/in0_lcrit_alarm`) ?? "").trim();
    return [alarm ? alarm === "1" : null, null];
  }
  return [null, null];
}

export function gateway() {
  for (const line of (read("/proc/net/route") ?? "").split("\n").slice(1)) {
    const fields = line.trim().split(/\s+/);
    if (fields.length > 2 && fields[1] === "00000000") {
      const bytes = Buffer.from(fields[2], "hex");
      return [bytes[3], bytes[2], bytes[1], bytes[0]].join(".");
    }
  }
  return null;
}

function network() {
  const signals = {};
  for (const line of (read("/proc/net/wireless") ?? "").split("\n").slice(2)) {
    const [name, rest] = line.split(":");
    const fields = (rest ?? "").trim().split(/\s+/);
    if (fields.length > 2 && Number.isFinite(Number.parseFloat(fields[2]))) signals[name.trim()] = Math.round(Number.parseFloat(fields[2]));
  }
  return Object.entries(networkInterfaces())
    .filter(([name, addresses]) => name !== "lo" && (addresses ?? []).some((a) => !a.internal))
    .map(([name, addresses]) => {
      const wifi = name in signals || existsSync(`/sys/class/net/${name}/wireless`);
      const kind = wifi ? "wifi" : existsSync(`/sys/class/net/${name}/device`) ? "ethernet" : "other";
      return { name, kind, addresses: addresses.filter((a) => !a.internal).map((a) => a.address).slice(0, 6), signal: signals[name] ?? null };
    })
    .sort((a, b) => a.name.localeCompare(b.name));
}

function osName() {
  const line = (read("/etc/os-release") ?? "").split("\n").find((l) => l.startsWith("PRETTY_NAME="));
  return line ? line.slice(12).trim().replace(/^"|"$/g, "").slice(0, 100) : null;
}

async function timed(check, target, action) {
  const started = Date.now();
  let ok = false;
  try {
    ok = Boolean(await Promise.race([action(), new Promise((_, reject) => setTimeout(() => reject(new Error("timed out")), CHECK_MS))]));
  } catch {
    ok = false;
  }
  return { check, target, ok, ms: Date.now() - started };
}

const ping = (address) =>
  new Promise((resolve, reject) => {
    execFile("ping", ["-c", "1", "-W", "2", "-n", "--", address], { timeout: CHECK_MS }, (error) => (error ? reject(error) : resolve(true)));
  });

const open = (host, port) =>
  new Promise((resolve, reject) => {
    const socket = netConnect({ host, port });
    socket.once("connect", () => {
      socket.destroy();
      resolve(true);
    });
    socket.once("error", reject);
    socket.setTimeout(CHECK_MS, () => {
      socket.destroy();
      reject(new Error("timed out"));
    });
  });

/** The gateway answers, the home page's host resolves, and a connection to it opens. */
export async function checks(home) {
  const jobs = [];
  const gw = gateway();
  if (gw) jobs.push(timed("gateway", gw, () => ping(gw)));
  let url = null;
  try {
    url = home ? new URL(home) : null;
  } catch {
    url = null;
  }
  if (url && (url.protocol === "http:" || url.protocol === "https:") && url.hostname) {
    const host = url.hostname.replace(/^\[|\]$/g, "");
    const port = Number(url.port) || (url.protocol === "https:" ? 443 : 80);
    jobs.push(timed("dns", host, () => lookup(host)));
    jobs.push(timed("home", `${host}:${port}`, () => open(host, port)));
  }
  return Promise.all(jobs);
}

export async function diagnostics({ home, log, dataDir }) {
  let storage = null;
  try {
    const stats = statfsSync(dataDir);
    storage = { total: stats.blocks * stats.bsize, free: stats.bavail * stats.bsize };
  } catch {
    storage = null;
  }
  const [powerWarning, throttled] = power(await throttledBits());
  return {
    uptime: Math.round(uptime()),
    app_uptime: Math.round(process.uptime()),
    os: osName(),
    kernel: release(),
    load: Math.round(loadavg()[0] * 100) / 100,
    processors: cpus().length || null,
    memory: memory(),
    storage,
    temperature: temperature(),
    power_warning: powerWarning,
    throttled,
    network: network(),
    gateway: gateway(),
    checks: await checks(home),
    log: log ? [...log.lines] : [],
  };
}
