// Steps taken on a page after it loads, written in its address after `#pleatview=`: waits,
// clicks (on text, a CSS selector or a place), drags and scrolls, for cookie banners, pop-ups
// and maps with nothing in their address. The same steps as the renderer's (actions.py) and the
// Android app's (PageSteps.kt); tests/fixtures/page-steps.json holds examples all three read.
// Presses are real input events, which pages can't tell from a mouse.

import { originOf } from "./config.js";

export const PARAM = "pleatview=";
export const MAX_STEPS = 30;
export const MAX_WAIT = 60;
const FIND_MS = 20_000; // how long a click waits for what it presses: pages draw late
const PAUSE_MS = 400; // after each step, for the page's own animations
const DRAG_MOVES = 20;

/** The address to open, without its steps, and the steps; throws RangeError for wrong steps. */
export function splitSteps(url) {
  const at = url.indexOf("#");
  if (at < 0) return { page: url, steps: [] };
  const parts = url.slice(at + 1).split("&");
  const mine = parts.filter((part) => part.startsWith(PARAM));
  if (!mine.length) return { page: url, steps: [] };
  const rest = parts.filter((part) => !part.startsWith(PARAM)).join("&");
  const steps = parseSteps(decodeURIComponent(mine[0].slice(PARAM.length)));
  return { page: url.slice(0, at) + (rest ? `#${rest}` : ""), steps };
}

function numbers(arg, count, low, high, problem) {
  const values = arg.split(",").map((part) => Number(part.trim()));
  if (values.length !== count || values.some((n) => !arg.trim() || Number.isNaN(n) || n < low || n > high)) {
    throw new RangeError(problem);
  }
  return values;
}

export function parseSteps(text) {
  const steps = [];
  for (const raw of text.split(";")) {
    if (!raw.trim()) continue;
    const colon = raw.indexOf(":");
    const name = (colon < 0 ? raw : raw.slice(0, colon)).trim().toLowerCase();
    const arg = colon < 0 ? "" : raw.slice(colon + 1).trim();
    if (name === "wait") {
      steps.push({ kind: "wait", numbers: numbers(arg, 1, 0, MAX_WAIT, `wait takes seconds from 0 to ${MAX_WAIT}`) });
    } else if (name === "click") {
      if (arg.toLowerCase().startsWith("css=")) {
        const css = arg.slice(4).trim();
        if (!css || css.length > 200) throw new RangeError("click:css= takes a CSS selector, such as #consent .accept");
        steps.push({ kind: "click", css });
        continue;
      }
      try {
        steps.push({ kind: "click", numbers: numbers(arg, 2, 0, 100, "") });
      } catch {
        if (!arg || arg.length > 200) throw new RangeError("click takes the text of what to click, css= and a selector, or x,y in percent");
        steps.push({ kind: "click", text: arg });
      }
    } else if (name === "drag" || name === "rightdrag") {
      steps.push({ kind: name, numbers: numbers(arg, 4, 0, 100, `${name} takes x1,y1,x2,y2 in percent`) });
    } else if (name === "scroll") {
      const values = numbers(arg, 3, -5000, 5000, "scroll takes x,y in percent and an amount in pixels");
      if (values.slice(0, 2).some((n) => n < 0 || n > 100)) throw new RangeError("scroll takes x,y in percent and an amount in pixels");
      steps.push({ kind: "scroll", numbers: values });
    } else {
      throw new RangeError(`unknown step '${name}': use wait, click, drag, rightdrag or scroll`);
    }
  }
  if (steps.length > MAX_STEPS) throw new RangeError(`at most ${MAX_STEPS} steps`);
  return steps;
}

/**
 * Takes the steps on a page, in order; a click whose target never shows is left out.
 * @param {object} page
 * @param {(js: string) => Promise<any>} page.evaluate runs a script in the page
 * @param {(event: object) => void} page.input sends an input event (Electron's sendInputEvent)
 * @param {() => {width: number, height: number}} page.size the page's view, in its own pixels
 * @param {string} page.find find.js
 * @param {() => boolean} [page.current] false once the page has moved on: stop
 */
export async function performSteps(steps, { evaluate, input, size, find, current = () => true, sleep = (ms) => new Promise((r) => setTimeout(r, ms)), findMs = FIND_MS, pauseMs = PAUSE_MS }) {
  const view = size();
  const css = (await evaluate("[innerWidth, innerHeight]")) || [view.width, view.height];
  // CSS pixels (where find.js points) to the view's pixels (where input events land), with the zoom.
  const scale = view.width / css[0];
  const point = (x, y) => ({ x: Math.round((view.width * x) / 100), y: Math.round((view.height * y) / 100) });
  const mouse = (type, at, extra = {}) => input({ type, x: at.x, y: at.y, ...extra });
  const taken = [];
  for (const step of steps) {
    if (!current()) break;
    if (step.kind === "wait") {
      await sleep(step.numbers[0] * 1000);
      continue;
    }
    if (step.kind === "click") {
      let at = step.numbers ? point(...step.numbers) : null;
      for (let waited = 0; !at && waited <= findMs && current(); waited += 500) {
        const found = await evaluate(`(${find.trim()})(${JSON.stringify(step.css ? { css: step.css } : { text: step.text })})`);
        if (found === "bad selector") break;
        if (Array.isArray(found)) at = { x: Math.round(found[0] * scale), y: Math.round(found[1] * scale) };
        else await sleep(500);
      }
      if (!at) {
        console.log(`PleatView: nothing showing ${JSON.stringify(step.css || step.text)} to click`);
        continue;
      }
      mouse("mouseMove", at);
      mouse("mouseDown", at, { button: "left", clickCount: 1 });
      mouse("mouseUp", at, { button: "left", clickCount: 1 });
    } else if (step.kind === "drag" || step.kind === "rightdrag") {
      const button = step.kind === "rightdrag" ? "right" : "left";
      const start = point(step.numbers[0], step.numbers[1]);
      const end = point(step.numbers[2], step.numbers[3]);
      mouse("mouseMove", start);
      mouse("mouseDown", start, { button, clickCount: 1 });
      // Maps follow the moves, not a jump.
      for (let i = 1; i <= DRAG_MOVES; i++) {
        const at = { x: Math.round(start.x + ((end.x - start.x) * i) / DRAG_MOVES), y: Math.round(start.y + ((end.y - start.y) * i) / DRAG_MOVES) };
        mouse("mouseMove", at, { button, modifiers: [`${button}ButtonDown`] });
        await sleep(20);
      }
      mouse("mouseUp", end, { button, clickCount: 1 });
    } else if (step.kind === "scroll") {
      const at = point(step.numbers[0], step.numbers[1]);
      mouse("mouseMove", at);
      // A wheel turned by this many pixels: negative is up, as the page sees it.
      mouse("mouseWheel", at, { deltaX: 0, deltaY: -step.numbers[2], canScroll: true });
    }
    taken.push(step.kind);
    await sleep(pauseMs);
  }
  return taken;
}

/**
 * The steps of the page one view shows. `open(url)` gives the address to load, without its
 * steps; they are then taken after each load of that page's site, from the start (a reload
 * loses what they did). A newer `open` or `stop` ends the ones before.
 */
export class Stepper {
  /**
   * @param {object} view the view's webContents and its size
   * @param {Electron.WebContents} view.webContents
   * @param {() => {width: number, height: number}} view.size
   * @param {string} find find.js
   */
  constructor({ webContents, size }, find) {
    this.wc = webContents;
    this.size = size;
    this.find = find;
    this.page = "";
    this.list = [];
    this.run = 0;
    webContents.on("did-finish-load", () => {
      if (this.list.length && originOf(webContents.getURL()) === originOf(this.page)) this.take();
    });
  }

  open(url) {
    this.run++;
    try {
      ({ page: this.page, steps: this.list } = splitSteps(url));
    } catch (e) {
      console.warn(`PleatView: the page's steps (#pleatview=…): ${e.message}`);
      this.page = url;
      this.list = [];
    }
    return this.page;
  }

  stop() {
    this.run++;
    this.list = [];
  }

  take() {
    const wc = this.wc;
    const run = ++this.run;
    return performSteps(this.list, {
      evaluate: (js) => wc.executeJavaScript(js),
      input: (event) => wc.sendInputEvent(event),
      size: this.size,
      find: this.find,
      current: () => run === this.run && !wc.isDestroyed(),
    }).then((taken) => console.log(`PleatView: took the page's steps (${taken.join(", ") || "none"})`),
      (e) => console.warn(`PleatView: the page's steps stopped: ${e.message}`));
  }
}
