import assert from "node:assert/strict";
import { test } from "node:test";
import { rotateDisplay } from "../src/rotation.js";

function fake(listing) {
  const ran = [];
  const run = async (file, args) => {
    ran.push([file, ...args].join(" "));
    return args.length === 0 || args[0] === "--query" ? listing : "";
  };
  return { ran, run };
}

test("Wayland: a screen turned clockwise gets the picture turned anticlockwise (transform 90)", async () => {
  const { ran, run } = fake('HDMI-A-1 "Some TV"\n  Enabled: yes\nHDMI-A-2 "Other"\n');
  assert.deepEqual(await rotateDisplay(90, { env: { WAYLAND_DISPLAY: "wayland-0" }, run }), ["HDMI-A-1", "HDMI-A-2"]);
  assert.deepEqual(ran.slice(1), ["wlr-randr --output HDMI-A-1 --transform 90", "wlr-randr --output HDMI-A-2 --transform 90"]);
  await rotateDisplay(0, { env: { WAYLAND_DISPLAY: "wayland-0" }, run });
  assert.equal(ran.at(-1), "wlr-randr --output HDMI-A-2 --transform normal");
});

test("X11: xrandr turns the connected outputs", async () => {
  const { ran, run } = fake("Screen 0: minimum 8 x 8\nHDMI-1 connected primary 1920x1080+0+0\nHDMI-2 disconnected\n");
  assert.deepEqual(await rotateDisplay(270, { env: { DISPLAY: ":0" }, run }), ["HDMI-1"]);
  assert.equal(ran.at(-1), "xrandr --output HDMI-1 --rotate right");
});
