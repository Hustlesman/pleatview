// What the display says (hdmi.js): whether one is plugged in, cec-ctl's account of messages read,
// the adapter on a TV's input found and a playback device's address taken, and what the TV says
// turned into what the screen reports. As PleatView Light's tests (test_cec.py).
import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { Cec, DisplayWatch, displayConnected, parse, physical } from "../src/hdmi.js";
import { Hub } from "../src/hub.js";

// As cec-ctl 1.30 prints them: the header, then the message on a line of its own.
const RECEIVED = `Received from TV to Playback Device 1 (0 to 4):
REPORT_POWER_STATUS (0x90):
\tpwr-state: standby (0x01)
\tSequence: 0 Rx Timestamp: 7.123s

Received from TV to all (0 to 15):
STANDBY (0x36)
\tSequence: 0 Rx Timestamp: 9.5s

Received from TV to Playback Device 1 (0 to 4): USER_CONTROL_PRESSED (0x44):
\tui-cmd: volume-up (0x41)

Received from Audio System to Playback Device 1 (5 to 4):
REPORT_AUDIO_STATUS (0x7a):
\taud-mute-status: on (0x01)
\taud-vol-status: 23 (0x17)

Transmit from Playback Device 1 to TV (4 to 0):
GIVE_DEVICE_POWER_STATUS (0x8f)
\tSequence: 6 Tx Timestamp: 70158.664734s
\tTx, Not Acknowledged (4), Max Retries
`;

const adapter = (address) => `Driver Info:
\tDriver Name                : vc4_hdmi
\tPhysical Address           : ${address}
\tLogical Address Mask       : 0x0000
`;

test("cec-ctl's messages, in either layout", () => {
  const messages = parse(RECEIVED.split("\n"));
  assert.deepEqual(messages.map((m) => [m.received, m.from, m.to, m.opcode]), [
    [true, 0, 4, "REPORT_POWER_STATUS"], [true, 0, 15, "STANDBY"], [true, 0, 4, "USER_CONTROL_PRESSED"],
    [true, 5, 4, "REPORT_AUDIO_STATUS"], [false, 4, 0, "GIVE_DEVICE_POWER_STATUS"]]);
  assert.equal(messages[0].fields["pwr-state"], "standby");
  assert.equal(messages[2].fields["ui-cmd"], "volume-up");
  assert.deepEqual(messages[3].fields, { "aud-mute-status": "on", "aud-vol-status": "23" });
  assert.equal(physical(adapter("1.0.0.0")), "1.0.0.0");
  assert.equal(physical(adapter("f.f.f.f")), null); // nothing plugged in
});

function make() {
  const told = [];
  const ran = [];
  let onLine = null;
  const cec = new Cec((kind, value) => told.push([kind, value]), {
    devices: ["/dev/cec0", "/dev/cec1"],
    run: async (...args) => {
      ran.push(args.join(" "));
      if (args.join(" ") === "-d /dev/cec0") return [0, adapter("f.f.f.f")];
      if (args.join(" ") === "-d /dev/cec1") return [0, adapter("1.0.0.0")];
      return [0, ""];
    },
    follow: (device, line) => {
      onLine = line;
      return () => {};
    },
  });
  return { cec, told, ran, feed: (text) => text.split("\n").forEach((l) => onLine(l)) };
}

test("takes an address on the TV's input, and never the TV's input", async () => {
  const { cec, ran } = make();
  assert.equal(await cec.start(), true);
  assert.deepEqual([cec.device, cec.address], ["/dev/cec1", "1.0.0.0"]);
  assert.ok(ran.includes("-d /dev/cec1 --playback --osd-name PleatView"));
  assert.ok(ran.includes("-d /dev/cec1 --to 0 --give-device-power-status"));
  assert.ok(!ran.some((a) => a.includes("--active-source"))); // it doesn't take the TV's input
  cec.stop();
  const none = new Cec(() => {}, { devices: [], run: async () => [0, ""] });
  assert.equal(await none.start(), false); // no CEC here: nothing follows
});

test("what the TV says", async () => {
  const { cec, told, ran, feed } = make();
  await cec.start();
  feed(RECEIVED);
  assert.deepEqual(told.map(([k]) => k), ["power", "power", "key", "audio"]);
  assert.deepEqual(told[0], ["power", "standby"]);
  assert.deepEqual([told[2][1].key, told[2][1].from], ["volume-up", "tv_remote"]);
  assert.deepEqual(told[3], ["audio", { muted: true, volume: 23 }]);
  told.length = 0;
  // Another device takes the TV: this screen isn't seen. The TV asks for this input: it says
  // it's the active source, and only then.
  cec.onMessage({ received: true, from: 8, to: 15, opcode: "ACTIVE_SOURCE", fields: { "phys-addr": "2.0.0.0" } });
  assert.deepEqual(told, [["input", "other"]]);
  cec.onMessage({ received: true, from: 0, to: 15, opcode: "REQUEST_ACTIVE_SOURCE", fields: {} });
  assert.ok(!ran.some((a) => a.includes("--active-source"))); // not showing: not it
  cec.onMessage({ received: true, from: 0, to: 15, opcode: "SET_STREAM_PATH", fields: { "phys-addr": "1.0.0.0" } });
  assert.deepEqual(told.at(-1), ["input", "this"]);
  assert.ok(ran.includes("-d /dev/cec1 --active-source phys-addr=1.0.0.0"));
  cec.stop();
});

test("whether a display is plugged in, as the kernel's ports say", () => {
  const dir = mkdtempSync(join(tmpdir(), "pleatview-drm-"));
  const port = (name, status) => {
    mkdirSync(join(dir, name), { recursive: true });
    if (status) writeFileSync(join(dir, name, "status"), `${status}\n`);
  };
  try {
    assert.equal(displayConnected(join(dir, "none")), null);
    port("card1-Writeback-1", "unknown");
    assert.equal(displayConnected(dir), null); // only a virtual port: it can't tell
    port("card1-HDMI-A-1", "disconnected");
    port("card1-HDMI-A-2", "disconnected");
    assert.equal(displayConnected(dir), false);
    port("card1-HDMI-A-2", "connected");
    assert.equal(displayConnected(dir), true);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("the screen reports what the display and the TV say", () => {
  const dir = mkdtempSync(join(tmpdir(), "pleatview-drm-"));
  mkdirSync(join(dir, "card0-HDMI-A-1"));
  writeFileSync(join(dir, "card0-HDMI-A-1", "status"), "connected\n");
  const hub = new Hub({ appVersion: "1.0.19" });
  const keys = [];
  const watch = new DisplayWatch(hub, { folder: dir, onKey: (key) => keys.push(key) });
  try {
    watch.look();
    assert.equal(hub.state.display.connected, true);
    watch.onCec("power", "standby");
    watch.onCec("input", "other");
    watch.onCec("audio", { muted: true, volume: 23 });
    watch.onCec("key", { key: "mute", from: "tv_remote", at: 1.0 });
    assert.deepEqual({ ...hub.state.display, since: null }, { connected: true, since: null, power: "standby", input: "other",
      audio: { muted: true, volume: 23 } });
    assert.deepEqual(hub.state.last_input, { key: "mute", from: "tv_remote", at: 1.0 });
    assert.deepEqual(keys, ["mute"]);
    // The cable comes out: what the TV said goes with it.
    writeFileSync(join(dir, "card0-HDMI-A-1", "status"), "disconnected\n");
    watch.look();
    assert.deepEqual([hub.state.display.connected, hub.state.display.power, hub.state.display.input], [false, null, null]);
  } finally {
    clearTimeout(hub.timer);
    rmSync(dir, { recursive: true, force: true });
  }
});
