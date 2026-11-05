// Switches the display off and on for standby (turn_off / turn_on), with whatever this
// system has: wlr-randr on wlroots compositors (Raspberry Pi OS's labwc, cage), xset on X11,
// and HDMI-CEC standby for TVs when cec-ctl and a CEC device are present. The black
// curtain on screen covers anything that can't be switched off.
import { execFile } from "node:child_process";
import { existsSync } from "node:fs";

const run = (file, args) =>
  new Promise((resolve) => execFile(file, args, { timeout: 5000 }, (error, stdout) => resolve(error ? null : stdout)));

async function wlrOutputs() {
  const out = await run("wlr-randr", []);
  return out ? [...out.matchAll(/^(\S+)/gm)].map((m) => m[1]) : [];
}

export async function displayPower(on, { cec = existsSync("/dev/cec0") } = {}) {
  const done = [];
  if (process.env.WAYLAND_DISPLAY) {
    const outputs = await wlrOutputs();
    for (const output of outputs) if ((await run("wlr-randr", ["--output", output, on ? "--on" : "--off"])) !== null) done.push(`wlr-randr ${output}`);
  } else if (process.env.DISPLAY) {
    if ((await run("xset", on ? ["dpms", "force", "on"] : ["dpms", "force", "off"])) !== null) done.push("xset");
  }
  if (cec) {
    // The adapter needs a logical address before it can send: register as a playback device.
    await run("cec-ctl", ["-d", "/dev/cec0", "--playback", "--osd-name", "PleatView"]);
    const args = on ? ["-d", "/dev/cec0", "--to", "0", "--image-view-on"] : ["-d", "/dev/cec0", "--to", "0", "--standby"];
    if ((await run("cec-ctl", args)) !== null) done.push("cec-ctl");
  }
  return done;
}
