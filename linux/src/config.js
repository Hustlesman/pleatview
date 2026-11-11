// User-facing configuration, as in the Android app (Config.kt). Written by Home Assistant
// through the integration, or by the phone setup page, via `POST /api/config`.

export const KIOSK_MODES = ["off", "sidebar", "full"];

export const DEFAULTS = Object.freeze({
  deviceName: "",
  haUrl: "",
  haToken: "",
  dashboards: [],
  homeDashboard: "",
  kiosk: "full",
  zoom: 100,
  keepScreenOn: true,
  launchOnBoot: true,
  reloadMinutes: 0,
  rotateSeconds: 0,
  returnHomeMinutes: 0,
  reopenMinutes: 10,
  rev: "",
});

export const isHttpUrl = (value) => /^https?:\/\//i.test(String(value));

/** `scheme://host[:port]` of an http(s) URL, or null. */
export function originOf(url) {
  try {
    const u = new URL(url);
    return u.protocol === "http:" || u.protocol === "https:" ? u.origin : null;
  } catch {
    return null;
  }
}

/** Home Assistant serves these itself, outside its frontend: files in www, HACS, the API. */
const SERVED_PATHS = ["/local/", "/hacsfiles/", "/api/", "/auth/", "/static/", "/media/"];

/** A page of Home Assistant's frontend, which it can switch to without a page load. */
export const isFrontendPath = (path) => !SERVED_PATHS.some((prefix) => path.startsWith(prefix));

/** Path, query and fragment of a URL, e.g. `/lovelace/0?edit=1`. */
export function pathOf(url) {
  try {
    const u = new URL(url);
    return (u.pathname || "/") + u.search + u.hash;
  } catch {
    return "/";
  }
}

/** Validates a Home Assistant base URL and strips any path, e.g. `http://ha.local:8123`. */
export function normalizeBaseUrl(raw) {
  const value = String(raw ?? "").trim();
  if (!value) return "";
  if (!isHttpUrl(value)) throw new RangeError("ha_url must start with http:// or https://");
  let u;
  try {
    u = new URL(value);
  } catch {
    throw new RangeError("ha_url is not a valid URL");
  }
  if (!u.hostname) throw new RangeError("ha_url is not a valid URL");
  return `${u.protocol}//${u.host}`;
}

export function normalizePath(raw) {
  const value = String(raw ?? "").trim();
  if (!value) return "/";
  return value.startsWith("/") ? value : `/${value}`;
}

/** A dashboard target is either an absolute http(s) URL or a path on Home Assistant. */
export function normalizeTarget(raw) {
  const value = String(raw ?? "").trim();
  if (!value) return "";
  return isHttpUrl(value) ? value : normalizePath(value);
}

export class Config {
  constructor(values = {}) {
    Object.assign(this, DEFAULTS, values);
    Object.freeze(this);
  }

  /** Set up: Home Assistant's address, or a home page of its own from another controller
   *  (PleatView Console), which leaves the setup screen without Home Assistant. */
  get isConfigured() {
    return this.haUrl !== "" || isHttpUrl(this.homeDashboard);
  }

  get haOrigin() {
    return originOf(this.haUrl);
  }

  /** Path (or URL) of the dashboard PleatView returns to. "/" lets Home Assistant pick the user's default. */
  get homePath() {
    return this.homeDashboard || this.dashboards[0]?.path || "/";
  }

  /** Turns a dashboard path such as `/lovelace/cameras` into an absolute URL. */
  resolve(pathOrUrl) {
    const value = String(pathOrUrl).trim();
    return isHttpUrl(value) ? value : this.haUrl + normalizePath(value);
  }

  isHaUrl(url) {
    const origin = this.haOrigin;
    return origin !== null && url != null && originOf(url) === origin;
  }

  toJson(includeSecrets = false) {
    return {
      device_name: this.deviceName,
      ha_url: this.haUrl,
      ...(includeSecrets ? { ha_token: this.haToken } : { ha_token_set: this.haToken !== "" }),
      dashboards: this.dashboards.map(({ title, path }) => ({ title, path })),
      home_dashboard: this.homeDashboard,
      kiosk: this.kiosk,
      zoom: this.zoom,
      keep_screen_on: this.keepScreenOn,
      launch_on_boot: this.launchOnBoot,
      reload_minutes: this.reloadMinutes,
      rotate_seconds: this.rotateSeconds,
      return_home_minutes: this.returnHomeMinutes,
      reopen_minutes: this.reopenMinutes,
      rev: this.rev,
    };
  }

  /**
   * Applies a partial update. Only keys present change. Any update that doesn't carry its
   * own `rev` clears it, so Home Assistant can tell someone else changed the configuration.
   * @throws {RangeError} when a value is invalid
   */
  merge(update, strict = true) {
    if (update === null || typeof update !== "object" || Array.isArray(update)) throw new RangeError("config must be an object");
    const c = { ...this };
    for (const [key, value] of Object.entries(update)) {
      switch (key) {
        case "device_name": c.deviceName = string(key, value).trim().slice(0, 64); break;
        case "ha_url": c.haUrl = normalizeBaseUrl(string(key, value)); break;
        case "ha_token": c.haToken = string(key, value).trim(); break;
        case "dashboards": c.dashboards = parseDashboards(value); break;
        case "home_dashboard": c.homeDashboard = normalizeTarget(string(key, value)); break;
        case "kiosk":
          if (!KIOSK_MODES.includes(value)) throw new RangeError(`kiosk must be one of ${KIOSK_MODES.join(", ")}`);
          c.kiosk = value;
          break;
        case "zoom": c.zoom = intIn(key, value, 25, 400); break;
        case "keep_screen_on": c.keepScreenOn = boolean(key, value); break;
        case "launch_on_boot": c.launchOnBoot = boolean(key, value); break;
        case "reload_minutes": c.reloadMinutes = intIn(key, value, 0, 7 * 24 * 60); break;
        case "rotate_seconds": c.rotateSeconds = intIn(key, value, 0, 24 * 60 * 60); break;
        case "return_home_minutes": c.returnHomeMinutes = intIn(key, value, 0, 24 * 60); break;
        case "reopen_minutes": c.reopenMinutes = intIn(key, value, 0, 24 * 60); break;
        case "rev": c.rev = string(key, value).slice(0, 64); break;
        case "ha_token_set": break; // read-only, echoed back by clients that round-trip GET /api/config
        default:
          if (strict) throw new RangeError(`unknown config key: ${key}`);
      }
    }
    // The token only ever goes to the Home Assistant it was made for: whoever points the
    // screen somewhere else has to bring a token for that place too.
    if (c.haToken && !("ha_token" in update) && originOf(c.haUrl) !== this.haOrigin) c.haToken = "";
    if (!("rev" in update)) c.rev = "";
    return new Config(c);
  }

  /** Loads stored configuration, ignoring keys this version doesn't know. */
  static fromJson(json) {
    return new Config().merge(json ?? {}, false);
  }
}

function parseDashboards(value) {
  if (!Array.isArray(value)) throw new RangeError("dashboards must be a list");
  if (value.length > 50) throw new RangeError("at most 50 dashboards are supported");
  return value.map((item, i) => {
    const path = normalizeTarget(item?.path ?? "");
    if (!path) throw new RangeError(`dashboard ${i} has no path`);
    return Object.freeze({ title: (String(item?.title ?? "").trim() || path).slice(0, 64), path });
  });
}

function string(key, value) {
  if (typeof value !== "string") throw new RangeError(`${key} must be text`);
  return value;
}

function boolean(key, value) {
  if (typeof value !== "boolean") throw new RangeError(`${key} must be true or false`);
  return value;
}

function intIn(key, value, min, max) {
  const n = typeof value === "number" ? value : typeof value === "string" && value.trim() !== "" ? Number(value.trim()) : NaN;
  if (Number.isNaN(n)) throw new RangeError(`${key} must be a number`);
  if (n < min || n > max) throw new RangeError(`${key} must be between ${min} and ${max}`);
  return Math.trunc(n);
}
