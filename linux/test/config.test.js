import assert from "node:assert/strict";
import { test } from "node:test";
import { Config, isFrontendPath, normalizeBaseUrl, originOf, pathOf } from "../src/config.js";

test("merge applies a partial update and keeps Home Assistant's revision only when it sends one", () => {
  const c = new Config().merge({ ha_url: "http://HA.local:8123/lovelace/0", zoom: "80", rev: "abc" });
  assert.equal(c.haUrl, "http://ha.local:8123");
  assert.equal(c.zoom, 80);
  assert.equal(c.rev, "abc");
  assert.equal(c.merge({ kiosk: "sidebar" }).rev, "");
});

test("the token stays with the Home Assistant it was made for", () => {
  const c = new Config().merge({ ha_url: "http://ha.local:8123", ha_token: "secret" });
  assert.equal(c.merge({ zoom: 90 }).haToken, "secret");
  assert.equal(c.merge({ ha_url: "http://elsewhere:8123" }).haToken, "");
  assert.equal(c.merge({ ha_url: "http://elsewhere:8123", ha_token: "new" }).haToken, "new");
  assert.deepEqual(Object.keys(c.toJson()).includes("ha_token"), false);
  assert.equal(c.toJson().ha_token_set, true);
});

test("dashboards, the home path and resolving paths", () => {
  const c = new Config().merge({ ha_url: "http://ha:8123", dashboards: [{ title: "", path: "lovelace/cams" }] });
  assert.deepEqual(c.dashboards, [{ title: "/lovelace/cams", path: "/lovelace/cams" }]);
  assert.equal(c.homePath, "/lovelace/cams");
  assert.equal(c.resolve("/map"), "http://ha:8123/map");
  assert.equal(c.resolve("https://grafana.lan/d/1"), "https://grafana.lan/d/1");
  assert.ok(c.isHaUrl("http://ha:8123/lovelace/0"));
  assert.ok(!c.isHaUrl("http://ha:8124/"));
});

test("invalid values are rejected; stored unknown keys are ignored", () => {
  const c = new Config();
  assert.throws(() => c.merge({ kiosk: "maybe" }), RangeError);
  assert.throws(() => c.merge({ zoom: 10 }), /zoom must be between 25 and 400/);
  assert.throws(() => c.merge({ ha_url: "ftp://x" }), /ha_url must start with http/);
  assert.throws(() => c.merge({ colour: "red" }), /unknown config key: colour/);
  assert.equal(Config.fromJson({ colour: "red", zoom: 75 }).zoom, 75);
});

test("URL helpers", () => {
  assert.equal(originOf("HTTP://Ha.Local:80/x"), "http://ha.local");
  assert.equal(originOf("ftp://x"), null);
  assert.equal(pathOf("http://ha:8123/lovelace/0?edit=1#a"), "/lovelace/0?edit=1#a");
  assert.equal(normalizeBaseUrl(" https://ha.example.com/ "), "https://ha.example.com");
});

test("files Home Assistant serves itself aren't frontend pages", () => {
  assert.equal(isFrontendPath("/lovelace/0"), true);
  assert.equal(isFrontendPath("/dashboard-streams/live?kiosk"), true);
  assert.equal(isFrontendPath("/media-browser"), true);
  assert.equal(isFrontendPath("/local/streams.html?v=a,b"), false);
  assert.equal(isFrontendPath("/hacsfiles/card/card.js"), false);
  assert.equal(isFrontendPath("/api/camera_proxy_stream/camera.door"), false);
});
