// Validated instructions from Home Assistant, as in the Android app (Command.kt).
// The wire format is documented in docs/protocol.md.
import { isHttpUrl, normalizeTarget } from "./config.js";

export const LEVELS = ["notice", "warning", "alert"];
// The tones the screen makes itself (ui/tones.js), as the Android app does (Tones.kt).
export const TONES = ["chime", "ding", "beep", "alarm", "siren", "bell"];

export class BadCommand extends Error {}

const MAX_DURATION = 24 * 60 * 60;
const VERSION = /^\d{1,4}\.\d{1,4}\.\d{1,6}$/;
const AUDIO_EXT = new Set(["mp3", "aac", "m4a", "flac", "ogg", "oga", "opus", "wav", "wma"]);
const IMAGE_EXT = new Set(["jpg", "jpeg", "png", "gif", "webp", "bmp", "svg", "avif", "heic"]);
export const MODES = ["fullscreen", "pip", "background"];
export const POSITIONS = ["top-left", "top-right", "bottom-left", "bottom-right", "center"];

/** Commands that bring PleatView to the front (on Linux: out of standby and over other windows). */
export function needsDisplay(command) {
  if (command.name === "play_media") return !command.announce;
  return ["show_dashboard", "show_web", "home", "turn_on"].includes(command.name);
}

/**
 * @param {object} json the request body
 * @returns {object} `{name, ...parameters}`; `show_url`, `show_image` and images sent to
 *   `play_media` become `{name: "show_web", kind: "page" | "image"}`
 * @throws {BadCommand}
 */
export function parseCommand(json) {
  const name = typeof json?.command === "string" ? json.command : "";
  if (!name) throw new BadCommand("missing 'command'");
  switch (name) {
    case "show_dashboard":
      return { name, path: normalizeTarget(requireString(json, "path")), duration: duration(json) };
    case "show_url":
      return { name: "show_web", kind: "page", url: httpUrl(json, "url"), layout: layout(json, "fullscreen"), duration: duration(json) };
    case "show_image":
      return { name: "show_web", kind: "image", url: httpUrl(json, "url"), layout: layout(json, "fullscreen"), duration: duration(json) };
    case "play_media":
      return parsePlayMedia(json);
    case "notify": {
      // A notice is a card; a warning adds a short tone, an alert a stronger one. An unknown
      // level (from a newer Home Assistant) is still a card.
      const level = LEVELS.includes(json.level) ? json.level : "notice";
      const tone = level !== "notice" && TONES.includes(json.tone) ? json.tone : null;
      return {
        name,
        title: optString(json, "title", 200),
        message: requireString(json, "message").slice(0, 2000),
        imageUrl: optHttpUrl(json, "image_url"),
        duration: duration(json) || (level === "alert" ? 30 : 10),
        position: position(json, "top-right"),
        level,
        tone,
      };
    }
    case "seek":
      return { name, position: number(json, "position", 0, Number.MAX_VALUE) };
    case "set_volume":
      return { name, level: number(json, "level", 0, 1) };
    case "mute":
      if (typeof json.muted !== "boolean") throw new BadCommand("'muted' must be true or false");
      return { name, muted: json.muted };
    case "forget_sign_ins":
      return { name, site: typeof json.site === "string" && json.site.trim() ? json.site.trim() : null };
    // Upkeep (docs/protocol.md): main.js carries them out, with the package's helper.
    case "restart":
      if (json.what !== "app" && json.what !== "device") throw new BadCommand("'what' is app or device");
      return { name, what: json.what };
    case "update":
      if (typeof json.version !== "string" || !VERSION.test(json.version)) throw new BadCommand("version is like 1.0.20");
      return { name, version: json.version };
    case "home":
    case "reload":
    case "play":
    case "pause":
    case "stop":
    case "turn_on":
    case "turn_off":
    case "next_dashboard":
    case "previous_dashboard":
      return { name };
    default:
      throw new BadCommand(`unknown command '${name}'`);
  }
}

function parsePlayMedia(json) {
  const url = requireString(json, "url");
  const scheme = url.includes(":") ? url.slice(0, url.indexOf(":")).toLowerCase() : "";
  if (!["http", "https", "rtsp", "rtsps"].includes(scheme)) throw new BadCommand("url must be http(s) or rtsp(s)");
  const mimeType = optString(json, "mime_type", 100)?.toLowerCase() ?? null;
  const kind = mediaKind(url, mimeType);
  const announce = json.announce === true;
  if (kind === "image" && !announce) {
    if (scheme !== "http" && scheme !== "https") throw new BadCommand("images must be http(s)");
    return { name: "show_web", kind: "image", url, layout: layout(json, "fullscreen"), duration: duration(json) };
  }
  return {
    name: "play_media",
    url,
    mimeType,
    title: optString(json, "title", 300),
    artist: optString(json, "artist", 300),
    imageUrl: optHttpUrl(json, "image_url"),
    layout: layout(json, kind === "audio" ? "background" : "fullscreen"),
    duration: duration(json),
    loop: json.loop === true,
    announce,
    volume: "volume" in json ? number(json, "volume", 0, 1) : null,
    startPosition: "start_position" in json ? number(json, "start_position", 0, Number.MAX_VALUE) : null,
  };
}

/** @returns {"audio" | "video" | "image"} */
export function mediaKind(url, mime) {
  if (mime) {
    if (mime.startsWith("audio/")) return "audio";
    if (mime.startsWith("video/")) return "video";
    if (mime.startsWith("image/") || mime.startsWith("multipart/x-mixed-replace")) return "image";
    if (mime.includes("mpegurl") || mime.includes("dash+xml")) return "video";
  }
  const file = url.split("?")[0].split("#")[0].split("/").pop() ?? "";
  const ext = file.includes(".") ? file.slice(file.lastIndexOf(".") + 1).toLowerCase() : "";
  if (AUDIO_EXT.has(ext)) return "audio";
  if (IMAGE_EXT.has(ext)) return "image";
  return "video";
}

function layout(json, defaultMode) {
  const raw = typeof json.mode === "string" ? json.mode : "";
  if (raw && !MODES.includes(raw)) throw new BadCommand(`unknown mode '${raw}'`);
  const size = "size" in json ? Math.trunc(number(json, "size", 10, 100)) : 35;
  return { mode: raw || defaultMode, position: position(json, "bottom-right"), size };
}

function position(json, fallback) {
  const raw = typeof json.position === "string" ? json.position : "";
  if (!raw) return fallback;
  if (!POSITIONS.includes(raw)) throw new BadCommand(`unknown position '${raw}'`);
  return raw;
}

function duration(json) {
  if (json.duration === undefined || json.duration === null) return null;
  return Math.trunc(number(json, "duration", 0, MAX_DURATION)) || null;
}

function requireString(json, key) {
  const value = json[key];
  if (typeof value !== "string" || !value.trim()) throw new BadCommand(`'${key}' is required`);
  return value.trim();
}

function optString(json, key, max) {
  if (json[key] === undefined || json[key] === null) return null;
  return String(json[key]).trim().slice(0, max) || null;
}

function httpUrl(json, key) {
  const url = requireString(json, key);
  if (!isHttpUrl(url)) throw new BadCommand(`'${key}' must be an http(s) URL`);
  return url;
}

function optHttpUrl(json, key) {
  const url = optString(json, key, 4096);
  if (url === null) return null;
  if (!isHttpUrl(url)) throw new BadCommand(`'${key}' must be an http(s) URL`);
  return url;
}

function number(json, key, min, max) {
  const raw = json[key];
  const value = typeof raw === "number" ? raw : typeof raw === "string" && raw.trim() !== "" ? Number(raw.trim()) : NaN;
  if (Number.isNaN(value)) throw new BadCommand(`'${key}' must be a number`);
  if (value < min || value > max) {
    throw new BadCommand(max === Number.MAX_VALUE ? `'${key}' must be at least ${min}` : `'${key}' must be between ${min} and ${max}`);
  }
  return value;
}
