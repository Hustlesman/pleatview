import assert from "node:assert/strict";
import { test } from "node:test";
import { displayPower } from "../src/power.js";

// Records the commands run; `fails` lists the programs that aren't there or refuse.
function fake(fails = [], outputs = "HDMI-A-1 \"Some TV\"\n  Enabled: yes\nHDMI-A-2 \"Other\"\n") {
  const ran = [];
  const run = async (file, args) => {
    ran.push([file, ...args].join(" "));
    if (fails.includes(file)) return null;
    return file === "wlr-randr" && args.length === 0 ? outputs : "";
  };
  return { ran, run };
}

test("Wayland screens go to sleep with wlopm, not by switching outputs off", async () => {
  const { ran, run } = fake();
  assert.deepEqual(await displayPower(false, { cec: [], env: { WAYLAND_DISPLAY: "wayland-0" }, run }), ["wlopm"]);
  assert.deepEqual(ran, ["wlopm --off *"]);
});

test("compositors without power management (cage) switch each output off with wlr-randr", async () => {
  const { ran, run } = fake(["wlopm"]);
  assert.deepEqual(await displayPower(true, { cec: [], env: { WAYLAND_DISPLAY: "wayland-0" }, run }),
    ["wlr-randr HDMI-A-1", "wlr-randr HDMI-A-2"]);
  assert.deepEqual(ran.slice(1), ["wlr-randr", "wlr-randr --output HDMI-A-1 --on", "wlr-randr --output HDMI-A-2 --on"]);
});

test("X11 uses DPMS", async () => {
  const { ran, run } = fake();
  assert.deepEqual(await displayPower(false, { cec: [], env: { DISPLAY: ":0" }, run }), ["xset"]);
  assert.deepEqual(ran, ["xset dpms force off"]);
});

test("every CEC device gets the command: the TV may be on either of a Pi's HDMI ports", async () => {
  const { ran, run } = fake();
  assert.deepEqual(await displayPower(false, { cec: ["/dev/cec0", "/dev/cec1"], env: {}, run }),
    ["cec-ctl /dev/cec0", "cec-ctl /dev/cec1"]);
  assert.ok(ran.includes("cec-ctl -d /dev/cec1 --playback --osd-name PleatView"));
  assert.ok(ran.includes("cec-ctl -d /dev/cec1 --to 0 --standby"));
  const { ran: woke, run: wake } = fake();
  await displayPower(true, { cec: ["/dev/cec0"], env: {}, run: wake });
  assert.ok(woke.includes("cec-ctl -d /dev/cec0 --to 0 --image-view-on"));
});
