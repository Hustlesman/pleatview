// Turns the picture for how the screen is mounted (degrees turned clockwise), with whatever
// this system has: wlr-randr on wlroots compositors (cage, Raspberry Pi OS's labwc), xrandr
// on X11. A screen turned clockwise needs the picture turned anticlockwise, which is what a
// Wayland output transform of 90 does, and xrandr's "left".
import { execFile } from "node:child_process";

const exec = (file, args) =>
  new Promise((resolve) => execFile(file, args, { timeout: 5000 }, (error, stdout) => resolve(error ? null : stdout)));

const WAYLAND = { 0: "normal", 90: "90", 180: "180", 270: "270" };
const X11 = { 0: "normal", 90: "left", 180: "inverted", 270: "right" };

/** Turns every screen; returns the outputs it turned. */
export async function rotateDisplay(degrees, { env = process.env, run = exec } = {}) {
  const done = [];
  if (env.WAYLAND_DISPLAY) {
    const listed = await run("wlr-randr", []);
    const outputs = listed ? [...listed.matchAll(/^(\S+)/gm)].map((m) => m[1]) : [];
    for (const output of outputs) {
      if ((await run("wlr-randr", ["--output", output, "--transform", WAYLAND[degrees]])) !== null) done.push(output);
    }
  } else if (env.DISPLAY) {
    const listed = await run("xrandr", ["--query"]);
    const outputs = listed ? [...listed.matchAll(/^(\S+) connected/gm)].map((m) => m[1]) : [];
    for (const output of outputs) {
      if ((await run("xrandr", ["--output", output, "--rotate", X11[degrees]])) !== null) done.push(output);
    }
  }
  return done;
}
