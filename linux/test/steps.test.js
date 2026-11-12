import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { Stepper, performSteps, splitSteps } from "../src/steps.js";

const examples = JSON.parse(readFileSync(new URL("../../tests/fixtures/page-steps.json", import.meta.url), "utf8"));

test("the steps are the renderer's and the Android app's", () => {
  for (const example of examples.valid) {
    const { page, steps } = splitSteps(example.address);
    assert.equal(page, example.page, example.address);
    assert.deepEqual(steps, example.steps.map((s) => ({ ...s })), example.address);
  }
  for (const address of examples.invalid) assert.throws(() => splitSteps(address), RangeError, address);
});

test("clicks press where the page shows the target, at the view's scale; drags and wheels too", async () => {
  const events = [];
  const found = { '"Accept"': [100, 50, 960, 540], '"#close"': [10, 20, 960, 540] };
  const taken = await performSteps(splitSteps("https://a/#pleatview=click:Accept;click:Missing;click:css=#close;drag:50,50,10,50;scroll:50,50,-300").steps, {
    evaluate: async (js) => (js === "[innerWidth, innerHeight]" ? [960, 540] : Object.entries(found).find(([k]) => js.includes(k))?.[1] ?? null),
    input: (e) => events.push(e),
    size: () => ({ width: 1920, height: 1080 }), // the page at zoom 200 %
    find: "((wanted) => null)",
    sleep: async () => {},
    findMs: 0,
    pauseMs: 0,
  });
  assert.deepEqual(taken, ["click", "click", "drag", "scroll"]);
  const downs = events.filter((e) => e.type === "mouseDown");
  assert.deepEqual(downs.map((e) => [e.x, e.y, e.button]), [[200, 100, "left"], [20, 40, "left"], [960, 540, "left"]]);
  assert.equal(events.filter((e) => e.type === "mouseMove" && e.modifiers?.includes("leftButtonDown")).length, 20);
  const wheel = events.find((e) => e.type === "mouseWheel");
  assert.deepEqual([wheel.x, wheel.y, wheel.deltaY], [960, 540, 300]);
});

test("a view's stepper opens the page without its steps and takes them after each load of its site", async () => {
  const handlers = {};
  const events = [];
  let url = "";
  const wc = {
    on: (name, fn) => (handlers[name] = fn),
    getURL: () => url,
    isDestroyed: () => false,
    executeJavaScript: async (js) => (js === "[innerWidth, innerHeight]" ? [100, 100] : null),
    sendInputEvent: (e) => events.push(e),
  };
  const stepper = new Stepper({ webContents: wc, size: () => ({ width: 100, height: 100 }) }, "((wanted) => null)");
  assert.equal(stepper.open("https://a.example/#x=1&pleatview=click:50,50"), "https://a.example/#x=1");
  url = "https://b.example/"; // another site loaded meanwhile: not its steps
  handlers["did-finish-load"]();
  url = "https://a.example/#x=1";
  handlers["did-finish-load"]();
  await new Promise((r) => setTimeout(r, 900));
  assert.deepEqual(events.filter((e) => e.type === "mouseDown").map((e) => [e.x, e.y]), [[50, 50]]);
  // Wrong steps: the page opens as it is, with none.
  assert.equal(stepper.open("https://a.example/#pleatview=dance:1"), "https://a.example/#pleatview=dance:1");
  assert.deepEqual(stepper.list, []);
});
