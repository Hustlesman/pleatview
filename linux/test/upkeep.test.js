import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { createCalls } from "../src/api.js";
import { BadCommand, parseCommand } from "../src/command.js";
import { Helper, RecentLog, checks, diagnostics, newer, redact } from "../src/upkeep.js";

test("the log keeps the last lines, without queries or bearer tokens", () => {
  const log = new RecentLog(2);
  const fake = { log() {}, info() {}, warn() {}, error() {} };
  log.capture(fake);
  fake.log("Playing http://ha.local:8123/api/hls/x/master.m3u8?authSig=SECRET now");
  fake.warn("Sent Authorization: Bearer SECRET2 to https://x.test/a?b=c");
  fake.error(new Error("boom"));
  assert.equal(log.lines.length, 2);
  assert.ok(log.lines.every((line) => !line.includes("SECRET")));
  assert.match(log.lines[0], /WARNING Sent Authorization: Bearer … to https:\/\/x\.test\/a\?…$/);
  assert.match(log.lines[1], /ERROR boom$/);
  assert.equal(redact("Playing http://ha.local:8123/api/hls/x/master.m3u8?authSig=SECRET now"),
    "Playing http://ha.local:8123/api/hls/x/master.m3u8?… now");
});

test("versions only move forward", () => {
  assert.ok(newer("1.0.20", "1.0.19"));
  assert.ok(newer("1.1.0", "1.0.99"));
  assert.ok(!newer("1.0.19", "1.0.19"));
  assert.ok(!newer("1.0.9", "1.0.10"));
});

test("upkeep's commands are checked like any other", () => {
  assert.deepEqual(parseCommand({ command: "restart", what: "app" }), { name: "restart", what: "app" });
  assert.deepEqual(parseCommand({ command: "update", version: "1.0.20" }), { name: "update", version: "1.0.20" });
  assert.throws(() => parseCommand({ command: "restart", what: "everything" }), BadCommand);
  assert.throws(() => parseCommand({ command: "update", version: "latest" }), BadCommand);
  assert.throws(() => parseCommand({ command: "update", version: "1.0.20;reboot" }), BadCommand);
});

test("the helper takes requests as empty files, and its status is read carefully", (t) => {
  const run = mkdtempSync(join(tmpdir(), "pleatview-helper-"));
  t.after(() => rmSync(run, { recursive: true, force: true }));
  const helper = new Helper(run);
  assert.equal(helper.ready, false);
  mkdirSync(join(run, "requests"));
  assert.equal(helper.ready, true);
  helper.update("1.0.20");
  helper.restartDevice();
  helper.restartDevice();
  assert.deepEqual(readdirSync(join(run, "requests")).sort(), ["restart", "update-1.0.20"]);
  assert.throws(() => helper.update("../../etc"), RangeError);
  assert.equal(helper.status(), null);
  writeFileSync(join(run, "status.json"), '{"version": "1.0.20", "state": "installing", "error": null, "at": 7}');
  assert.deepEqual(helper.status(), { version: "1.0.20", state: "installing", error: null, at: 7 });
  writeFileSync(join(run, "status.json"), '{"version": "<b>", "state": "installed"}');
  assert.equal(helper.status(), null);
});

test("diagnostics: what it reports, and the home check", async (t) => {
  const dir = mkdtempSync(join(tmpdir(), "pleatview-data-"));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const log = new RecentLog();
  log.add("INFO", ["a line"]);
  const report = await diagnostics({ home: "http://127.0.0.1:9/", log, dataDir: dir });
  for (const key of ["uptime", "app_uptime", "os", "kernel", "load", "processors", "memory", "storage", "temperature",
    "power_warning", "throttled", "network", "gateway", "checks", "log"]) assert.ok(key in report, key);
  assert.ok(report.storage.total > 0);
  assert.equal(report.log.length, 1);
  const byName = Object.fromEntries(report.checks.map((c) => [c.check, c]));
  assert.equal(byName.dns.ok, true);
  assert.equal(byName.home.ok, false);
  assert.equal(byName.home.target, "127.0.0.1:9");
  assert.ok(!(await checks(null)).some((c) => c.check === "home"));
});

test("protocol v2's diagnostics call, and refusals from upkeep's commands", async () => {
  const backend = {
    diagnostics: async () => ({ uptime: 5 }),
    dispatch: (command) => {
      if (command.name === "update") throw new BadCommand("not_supported");
    },
  };
  const call = createCalls(backend, { isPaired: true });
  assert.deepEqual(await call("diagnostics", {}), { uptime: 5 });
  await assert.rejects(call("command", { command: "update", version: "1.0.20" }), /not_supported/);
  assert.deepEqual(await call("command", { command: "restart", what: "app" }), { ok: true });
});
