// What the display says, for a controller keeping proof of play: whether one is plugged in, as
// the kernel sees its ports, and over HDMI-CEC, through cec-ctl (v4l-utils, recommended by the
// package), whether the TV is on or in standby, whether it shows this screen's input or another,
// the TV remote's keys that reach this screen, and a sound system's volume and mute. A TV in
// standby, or on another input, shows nobody this screen. As PleatView Light does (cec.py).
//
// The screen takes a playback device's address with its name, and never takes the TV's input by
// itself: it says it's the active source only when the TV asks for this input (Set Stream Path),
// or asks who is and this input is the one showing. A TV that doesn't speak CEC (or has it off)
// answers nothing: then nothing is known, and nothing changes.
import { execFile, spawn } from "node:child_process";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

export const DRM = "/sys/class/drm";
const CEC_CTL = "cec-ctl";
export const POLL_EVERY_MS = 60_000; // between asking the TV whether it's on
const ANSWER_MS = 6000;
const TV = 0;

/**
 * Whether a display is plugged in, as the kernel sees its ports (an HDMI port's status): true or
 * false, or null where no port says (virtual ones, such as Writeback, never do).
 */
export function displayConnected(folder = DRM) {
  let names;
  try {
    names = readdirSync(folder).filter((n) => /^card\d+-/.test(n) && !/Writeback|Virtual/.test(n)).sort();
  } catch {
    return null;
  }
  const seen = [];
  for (const name of names) {
    let text;
    try {
      text = readFileSync(join(folder, name, "status"), "utf8").trim();
    } catch {
      continue;
    }
    if (text === "connected" || text === "disconnected") seen.push(text === "connected");
  }
  return seen.length ? seen.some(Boolean) : null;
}

const HEADER = /^(Received from|Transmit(?:ted)? (?:from|by)) (.+?) to (.+?) \((\d+) to (\d+)\):\s*(.*)$/;
const OPCODE = /^([A-Z][A-Z0-9_]+)(?: \(0x[0-9a-fA-F]+\))?:?$/;
const FIELD = /^\s+([a-z][a-z0-9-]*): (.+?)(?: \(0x[0-9a-fA-F]+\))?$/;
const PHYSICAL = /Physical Address\s*:\s*([0-9a-f]\.[0-9a-f]\.[0-9a-f]\.[0-9a-f])/;
const POWER = { on: "on", standby: "standby", "to-on": "on", "to-standby": "standby",
  "in transition standby to on": "on", "in transition on to standby": "standby" };

/** cec-ctl's account of messages: who to whom, the message (its opcode) and its fields. */
export function parse(lines) {
  const out = [];
  let current = null;
  for (const raw of lines) {
    const line = raw.replace(/\n$/, "");
    const header = HEADER.exec(line.trim());
    if (header) {
      current = { received: header[1].startsWith("Received"), from: Number(header[4]), to: Number(header[5]), opcode: null, fields: {} };
      out.push(current);
      const rest = header[6].trim() ? OPCODE.exec(header[6].trim()) : null;
      if (rest) current.opcode = rest[1];
      continue;
    }
    if (!current) continue;
    if (current.opcode === null) {
      const opcode = OPCODE.exec(line.trim());
      if (opcode) current.opcode = opcode[1];
      continue;
    }
    const field = FIELD.exec(line);
    if (field) current.fields[field[1]] = field[2].trim();
  }
  return out.filter((m) => m.opcode);
}

export function physical(text) {
  const found = PHYSICAL.exec(text);
  return found && found[1] !== "f.f.f.f" ? found[1] : null;
}

/** cec-ctl, once: its exit code and what it said (127: no cec-ctl here, 124: no answer in time). */
function runCec(...args) {
  return new Promise((resolve) => {
    execFile(CEC_CTL, args, { timeout: ANSWER_MS }, (error, stdout, stderr) => {
      if (error?.code === "ENOENT") return resolve([127, ""]);
      if (error?.killed) return resolve([124, ""]);
      resolve([typeof error?.code === "number" ? error.code : 0, `${stdout}${stderr}`]);
    });
  });
}

/**
 * Follows the TV over HDMI-CEC and tells the screen what changes: `power` (on, standby), `input`
 * (this, other), `key` (a remote's key that reached this screen) and `audio` (a sound system's
 * volume and mute).
 */
export class Cec {
  constructor(tell, { name = "PleatView", devices = null, run = runCec, follow = null } = {}) {
    this.tell = tell;
    this.name = name.slice(0, 14); // a CEC name is at most 14 characters
    this.devices = devices ?? Cec.devices();
    this.run = run;
    this.followWith = follow; // for the tests: (device, onLine) => stop
    this.device = null;
    this.address = null; // this screen's physical address on the TV's inputs
    this.input = null;
    this.stopped = false;
    this.timer = null;
    this.follower = null;
  }

  static devices() {
    try {
      return readdirSync("/dev").filter((n) => /^cec\d+$/.test(n)).sort().map((n) => `/dev/${n}`);
    } catch {
      return [];
    }
  }

  /** Finds the adapter on a TV's input and takes a playback device's address on it; false where there's none. */
  async start() {
    for (const device of this.devices) {
      const [code, text] = await this.run("-d", device);
      if (code === 0 && physical(text)) {
        this.device = device;
        this.address = physical(text);
        break;
      }
    }
    if (!this.device) return false;
    await this.run("-d", this.device, "--playback", "--osd-name", this.name);
    console.log(`PleatView: HDMI-CEC on ${this.device}, this input ${this.address}`);
    this.follow();
    this.poll();
    return true;
  }

  stop() {
    this.stopped = true;
    clearTimeout(this.timer);
    this.follower?.();
  }

  async poll() {
    if (this.stopped) return;
    const [, text] = await this.run("-d", this.device, "--to", String(TV), "--give-device-power-status");
    const answered = parse(text.split("\n")).filter((m) => m.received && m.opcode === "REPORT_POWER_STATUS");
    if (answered.length) this.onMessage(answered.at(-1));
    if (!this.stopped) this.timer = setTimeout(() => this.poll(), POLL_EVERY_MS);
  }

  /** What the TV and the others on HDMI say, as it comes (cec-ctl --wait-for-msgs, which needs no more than the video group). */
  follow() {
    let block = [];
    const flush = () => {
      for (const message of parse(block)) this.onMessage(message);
      block = [];
    };
    const onLine = (line) => {
      if (HEADER.test(line.trim()) && block.length) flush();
      block.push(line);
      if (!line.trim() && block.length) flush();
    };
    if (this.followWith) {
      this.follower = this.followWith(this.device, onLine);
      return;
    }
    const child = spawn(CEC_CTL, ["-d", this.device, "--wait-for-msgs"], { stdio: ["ignore", "pipe", "ignore"] });
    let rest = "";
    child.stdout.on("data", (chunk) => {
      const lines = (rest + chunk.toString()).split("\n");
      rest = lines.pop();
      lines.forEach(onLine);
    });
    child.on("error", () => {}); // no cec-ctl: nothing to follow
    child.on("exit", () => {
      if (!this.stopped) this.timer = setTimeout(() => this.follow(), 5000); // it ended: again, in a moment
    });
    this.follower = () => child.kill();
  }

  onMessage(message) {
    if (!message.received) return;
    const { opcode, fields, from } = message;
    if (opcode === "STANDBY" && from === TV) this.tell("power", "standby");
    else if (opcode === "REPORT_POWER_STATUS" && from === TV) {
      const power = POWER[fields["pwr-state"] ?? ""];
      if (power) this.tell("power", power);
    } else if (["ACTIVE_SOURCE", "ROUTING_CHANGE", "ROUTING_INFORMATION", "SET_STREAM_PATH"].includes(opcode)) {
      const shown = fields["phys-addr"] || fields["new-phys-addr"];
      if (shown) {
        this.setInput(shown === this.address ? "this" : "other");
        if (opcode === "SET_STREAM_PATH" && shown === this.address) this.active(); // the TV asks for this input
      }
    } else if (opcode === "REQUEST_ACTIVE_SOURCE" && this.input === "this") {
      this.active();
    } else if (opcode === "USER_CONTROL_PRESSED") {
      const key = fields["ui-cmd"];
      if (key) this.tell("key", { key: key.split(" (")[0].trim(), from: "tv_remote", at: Math.round(Date.now()) / 1000 });
    } else if (opcode === "REPORT_AUDIO_STATUS") {
      const muted = fields["aud-mute-status"];
      const volume = fields["aud-vol-status"];
      this.tell("audio", { muted: muted ? muted === "on" : null, volume: volume && /^\d+$/.test(volume) ? Number(volume) : null });
    }
  }

  setInput(which) {
    if (which !== this.input) {
      this.input = which;
      this.tell("input", which);
    }
  }

  active() {
    if (this.device && this.address) return this.run("-d", this.device, "--active-source", `phys-addr=${this.address}`);
  }
}

/** The display's part of the state, and what the TV says changing it (as Light's screen.py). */
export class DisplayWatch {
  constructor(hub, { folder = DRM, everyMs = 2000, onKey = null } = {}) {
    this.hub = hub;
    this.folder = folder;
    this.everyMs = everyMs;
    this.onKey = onKey; // a TV remote's key, for the screen's own uses (answering a question)
    this.timer = null;
  }

  get display() {
    return this.hub.state.display;
  }

  look() {
    const connected = displayConnected(this.folder);
    if (connected !== this.display.connected) {
      if (connected === false) console.warn("PleatView: no display is plugged in");
      else if (connected && this.display.connected === false) console.log("PleatView: a display is plugged in again");
      this.hub.update({ display: { ...this.display, connected, since: Math.round(Date.now()) / 1000,
        ...(connected === false ? { power: null, input: null } : {}) } });
    }
  }

  start() {
    this.look();
    this.timer = setInterval(() => this.look(), this.everyMs);
  }

  stop() {
    clearInterval(this.timer);
  }

  /** What the TV says over HDMI-CEC (Cec). The screen shows on whatever it says. */
  onCec(kind, value) {
    if (kind === "power" || kind === "input" || kind === "audio") this.hub.update({ display: { ...this.display, [kind]: value } });
    else if (kind === "key") {
      this.hub.update({ last_input: value });
      this.onKey?.(value?.key);
    }
  }
}
