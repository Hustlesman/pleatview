import assert from "node:assert/strict";
import { test } from "node:test";
import { BadCommand, mediaKind, needsDisplay, parseCommand } from "../src/command.js";

test("show_dashboard normalises its path", () => {
  assert.deepEqual(parseCommand({ command: "show_dashboard", path: "lovelace/cams", duration: 60 }),
    { name: "show_dashboard", path: "/lovelace/cams", duration: 60 });
  assert.equal(parseCommand({ command: "show_dashboard", path: "https://grafana.lan/d/x" }).path, "https://grafana.lan/d/x");
});

test("notify defaults to ten seconds at the top right", () => {
  assert.deepEqual(parseCommand({ command: "notify", message: " Ding dong " }),
    { name: "notify", title: null, message: "Ding dong", imageUrl: null, duration: 10, position: "top-right", level: "notice", tone: null });
});

test("notifications have a level and, past a notice, a tone", () => {
  const n = (extra) => parseCommand({ command: "notify", message: "Hi", ...extra });
  assert.equal(n({ tone: "chime" }).tone, null); // a notice is a card only
  assert.deepEqual([n({ level: "warning", tone: "beep" }).level, n({ level: "warning", tone: "beep" }).tone], ["warning", "beep"]);
  assert.equal(n({ level: "alert" }).duration, 30); // an alert stays longer
  assert.equal(n({ level: "alert", tone: "none" }).tone, null);
  assert.equal(n({ level: "emergency" }).level, "notice"); // still a card
});

test("play_media picks the layout from the media's kind", () => {
  const audio = parseCommand({ command: "play_media", url: "http://ha/a.mp3" });
  assert.equal(audio.layout.mode, "background");
  const video = parseCommand({ command: "play_media", url: "http://ha/api/hls/x/master_playlist.m3u8", mime_type: "application/vnd.apple.mpegurl" });
  assert.equal(video.layout.mode, "fullscreen");
  assert.equal(video.mimeType, "application/vnd.apple.mpegurl");
  // A picture is shown like show_image.
  assert.deepEqual(parseCommand({ command: "play_media", url: "http://ha/snap.jpg", mode: "pip", size: 40 }),
    { name: "show_web", kind: "image", url: "http://ha/snap.jpg", layout: { mode: "pip", position: "bottom-right", size: 40 }, duration: null });
});

test("announcements don't need the screen; dashboards do", () => {
  assert.ok(!needsDisplay(parseCommand({ command: "play_media", url: "http://ha/tts.mp3", announce: true })));
  assert.ok(needsDisplay(parseCommand({ command: "play_media", url: "http://ha/v.mp4" })));
  assert.ok(needsDisplay(parseCommand({ command: "home" })));
  assert.ok(!needsDisplay(parseCommand({ command: "notify", message: "x" })));
});

test("media kinds from MIME types and extensions", () => {
  assert.equal(mediaKind("http://x/stream", "multipart/x-mixed-replace; boundary=f"), "image");
  assert.equal(mediaKind("http://x/song.FLAC?sig=1", null), "audio");
  assert.equal(mediaKind("http://x/clip", null), "video");
});

test("bad commands say what's wrong", () => {
  const bad = (json, message) => assert.throws(() => parseCommand(json), (e) => e instanceof BadCommand && e.message === message);
  bad({}, "missing 'command'");
  bad({ command: "explode" }, "unknown command 'explode'");
  bad({ command: "notify" }, "'message' is required");
  bad({ command: "set_volume", level: 2 }, "'level' must be between 0 and 1");
  bad({ command: "show_url", url: "file:///etc/passwd" }, "'url' must be an http(s) URL");
  bad({ command: "play_media", url: "ftp://x/a.mp4" }, "url must be http(s) or rtsp(s)");
  bad({ command: "play_media", url: "http://x/a.mp4", mode: "sideways" }, "unknown mode 'sideways'");
  bad({ command: "mute", muted: "yes" }, "'muted' must be true or false");
  bad({ command: "seek", position: -1 }, "'position' must be at least 0");
});
