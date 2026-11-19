import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

// The page's script, as the stream page loads it.
const context = { module: { exports: {} } };
vm.runInNewContext(readFileSync(new URL("../src/ui/instep.js", import.meta.url), "utf8"), context);
const { inStep, wallClock, drawnAt } = context.module.exports;

test("a wall's piece is kept the delay its playlist asks for behind the time it was drawn", () => {
  assert.deepEqual({ ...inStep(4.0, 4) }, { rate: 1 });
  assert.deepEqual({ ...inStep(4.001, 4) }, { rate: 1 });  // on the mark
  assert.deepEqual({ ...inStep(4.005, 4) }, { rate: 1.01 });  // 5 ms late: 1% faster
  assert.deepEqual({ ...inStep(4.013, 4) }, { rate: 1.026 });
  assert.deepEqual({ ...inStep(4.2, 4) }, { rate: 1.05 });  // late: a little faster
  assert.deepEqual({ ...inStep(3.99, 4) }, { rate: 0.98 });  // early: a little slower
  assert.deepEqual({ ...inStep(2.0, 4) }, { wait: 2, rate: 1 });  // joined near the live edge: a pause, then in step
  assert.ok(Math.abs(inStep(3.7, 4).wait - 0.3) < 1e-9);
  assert.deepEqual({ ...inStep(9.5, 4) }, { seek: 5.5, rate: 1 });  // after a stall: forward to it
  assert.deepEqual({ ...inStep(3.9, 4) }, { rate: 0.95 });  // at most 5%
  assert.deepEqual({ ...inStep(NaN, 4) }, { rate: 1 });
});

test("a wall's piece's clock comes from its playlist; a stream of its own has none", () => {
  const piece = "#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXT-X-SERVER-CONTROL:HOLD-BACK=4.000\n#EXT-X-PLEATVIEW-CLOCK:1795060000.125\nt1-4.ts\n";
  assert.deepEqual({ ...wallClock(piece) }, { clock: 1795060000.125, holdBack: 4 });
  assert.equal(wallClock("#EXTM3U\n#EXT-X-TARGETDURATION:1\ns1.ts\n"), null);
  const wrap = 2 ** 33 / 90000;
  const clock = 1795000000;
  const now = clock + wrap + 100;
  assert.ok(Math.abs(drawnAt(clock, 96, now) - (now - 4)) < 1e-6);  // the timestamps went round
  assert.ok(Math.abs(drawnAt(clock, 96 + wrap, now) - (now - 4)) < 1e-6);
});
