// Switches the display off and on for standby (turn_off / turn_on), with whatever this
// system has: wlopm on wlroots compositors that can put screens to sleep (Raspberry Pi OS's
// labwc, sway, Wayfire), wlr-randr on those that can't (cage), xset on X11, and HDMI-CEC
// standby for TVs when cec-ctl and CEC devices are present. The black curtain on screen
// covers anything that can't be switched off.
import { execFile } from "node:child_process";
import { readdirSync } from "node:fs";

const exec = (file, args) =>
  new Promise((resolve) => execFile(file, args, { timeout: 5000 }, (error, stdout) => resolve(error ? null : stdout)));

// A Raspberry Pi 4 or 5 has a CEC device for each HDMI port, and the TV may be on either.
function cecDevices() {
  try {
    return readdirSync("/dev").filter((name) => /^cec\d+$/.test(name)).sort().map((name) => `/dev/${name}`);
  } catch {
    return [];
  }
}

export async function displayPower(on, { cec = cecDevices(), env = process.env, run = exec } = {}) {
  const done = [];
  if (env.WAYLAND_DISPLAY) {
    // wlopm puts the screens to sleep and keeps them in the layout. Switching the only output
    // off and on again with wlr-randr crashed labwc 0.20 on a Raspberry Pi 5, logging the
    // desktop out.
    if ((await run("wlopm", [on ? "--on" : "--off", "*"])) !== null) {
      done.push("wlopm");
    } else {
      const listed = await run("wlr-randr", []);
      const outputs = listed ? [...listed.matchAll(/^(\S+)/gm)].map((m) => m[1]) : [];
      for (const output of outputs) if ((await run("wlr-randr", ["--output", output, on ? "--on" : "--off"])) !== null) done.push(`wlr-randr ${output}`);
    }
  } else if (env.DISPLAY) {
    if ((await run("xset", ["dpms", "force", on ? "on" : "off"])) !== null) done.push("xset");
  }
  for (const device of cec) {
    // The adapter needs a logical address before it can send: register as a playback device.
    await run("cec-ctl", ["-d", device, "--playback", "--osd-name", "PleatView"]);
    if ((await run("cec-ctl", ["-d", device, "--to", "0", on ? "--image-view-on" : "--standby"])) !== null) done.push(`cec-ctl ${device}`);
  }
  return done;
}
