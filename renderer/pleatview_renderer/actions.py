"""Steps the renderer takes on a page after it loads: clicks, drags, scrolls and waits.

For pages with no settings in their address, such as a map's view, a cookie banner or a side
bar. They're written in the page's address, after `#pleatview=`, so a dashboard, a Webpage
dashboard or `pleatview.show_url` carries them with no setting anywhere else:

    https://example.com/map#pleatview=click:Reject all;click:Collapse;drag:70,60,35,45

Places are percentages of the screen's width and height, so the steps fit every TV size.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
from typing import Any, Protocol
from urllib.parse import unquote

_LOGGER = logging.getLogger(__name__)

PARAM = "pleatview="
MAX_STEPS = 30
MAX_WAIT = 60
# How long a click waits for its text to appear (pages draw their buttons late).
FIND_SECONDS = 20
# A breath after each step, for the page's own animations.
PAUSE = 0.4
DRAG_MOVES = 20
BUTTONS = {"left": 1, "right": 2}


@dataclass(frozen=True)
class Step:
    kind: str  # wait, click, drag, rightdrag, scroll
    text: str = ""
    numbers: tuple[float, ...] = ()


def split(url: str) -> tuple[str, tuple[Step, ...]]:
    """The address to open, without its steps, and the steps; ValueError says what's wrong."""
    base, hash_sign, fragment = url.partition("#")
    if not hash_sign:
        return url, ()
    parts = fragment.split("&")
    mine = [part for part in parts if part.startswith(PARAM)]
    if not mine:
        return url, ()
    rest = "&".join(part for part in parts if not part.startswith(PARAM))
    return (f"{base}#{rest}" if rest else base), parse(unquote(mine[0][len(PARAM):]))


def parse(text: str) -> tuple[Step, ...]:
    steps: list[Step] = []
    for raw in text.split(";"):
        if not raw.strip():
            continue
        name, _, arg = raw.partition(":")
        name, arg = name.strip().lower(), arg.strip()
        if name == "wait":
            seconds = _numbers(arg, 1, 0, MAX_WAIT, f"wait takes seconds from 0 to {MAX_WAIT}")[0]
            steps.append(Step("wait", numbers=(seconds,)))
        elif name == "click":
            try:
                steps.append(Step("click", numbers=_numbers(arg, 2, 0, 100, "")))
            except ValueError:
                if not arg or len(arg) > 200:
                    raise ValueError("click takes the text of what to click, or x,y in percent") from None
                steps.append(Step("click", text=arg))
        elif name in ("drag", "rightdrag"):
            steps.append(Step(name, numbers=_numbers(arg, 4, 0, 100, f"{name} takes x1,y1,x2,y2 in percent")))
        elif name == "scroll":
            numbers = _numbers(arg, 3, -5000, 5000, "scroll takes x,y in percent and an amount in pixels")
            if not all(0 <= n <= 100 for n in numbers[:2]):
                raise ValueError("scroll takes x,y in percent and an amount in pixels")
            steps.append(Step("scroll", numbers=numbers))
        else:
            raise ValueError(f"unknown step {name!r}: use wait, click, drag, rightdrag or scroll")
    if len(steps) > MAX_STEPS:
        raise ValueError(f"at most {MAX_STEPS} steps")
    return tuple(steps)


def _numbers(arg: str, count: int, low: float, high: float, problem: str) -> tuple[float, ...]:
    try:
        numbers = tuple(float(part) for part in arg.split(","))
    except ValueError:
        raise ValueError(problem) from None
    if len(numbers) != count or not all(low <= n <= high for n in numbers):
        raise ValueError(problem)
    return numbers


class Browser(Protocol):
    async def call(self, method: str, **params: Any) -> dict[str, Any]: ...


# The middle of the first thing on the page that shows [text] (case aside): buttons and links
# first, then any element of its own; null while there's none on the screen.
FIND = """
((wanted) => {
  const label = (el) => (el.innerText || el.value || el.getAttribute('aria-label') || el.title || '')
    .trim().replace(/\\s+/g, ' ').toLowerCase();
  const shown = (el) => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.pointerEvents !== 'none'
      && r.right > 0 && r.bottom > 0 && r.left < innerWidth && r.top < innerHeight;
  };
  const pick = (selector, own) => [...document.querySelectorAll(selector)]
    .find((el) => (!own || el.children.length === 0) && label(el) === wanted && shown(el));
  const el = pick('button, a, [role=button], input[type=button], input[type=submit], summary, label', false)
    || pick('body *', true);
  if (!el) return null;
  const r = el.getBoundingClientRect();
  return [r.left + r.width / 2, r.top + r.height / 2];
})(%s)
"""


async def perform(browser: Browser, steps: tuple[Step, ...], name: str = "") -> None:
    """Takes [steps] in order; a click whose text never shows is left out."""
    size = (await _value(browser, "[innerWidth, innerHeight]")) or [1920, 1080]

    def point(x: float, y: float) -> tuple[float, float]:
        return size[0] * x / 100, size[1] * y / 100

    for step in steps:
        if step.kind == "wait":
            await asyncio.sleep(step.numbers[0])
            continue
        if step.kind == "click":
            where = point(*step.numbers) if step.numbers else await _find(browser, step.text)
            if where is None:
                _LOGGER.info("%s: nothing showing %r to click", name, step.text)
                continue
            await _press(browser, *where)
        elif step.kind in ("drag", "rightdrag"):
            x1, y1, x2, y2 = step.numbers
            await _drag(browser, point(x1, y1), point(x2, y2), "right" if step.kind == "rightdrag" else "left")
        elif step.kind == "scroll":
            x, y = point(*step.numbers[:2])
            await _mouse(browser, "mouseMoved", x, y)
            await browser.call("Input.dispatchMouseEvent", type="mouseWheel", x=x, y=y, deltaX=0, deltaY=step.numbers[2])
        await asyncio.sleep(PAUSE)


async def _value(browser: Browser, expression: str) -> Any:
    result = await browser.call("Runtime.evaluate", expression=expression, returnByValue=True)
    return result.get("result", {}).get("value")


async def _find(browser: Browser, text: str) -> tuple[float, float] | None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + FIND_SECONDS
    while True:
        found = await _value(browser, FIND % json.dumps(text.lower()))
        if found:
            return found[0], found[1]
        if loop.time() > deadline:
            return None
        await asyncio.sleep(0.5)


async def _mouse(browser: Browser, kind: str, x: float, y: float, button: str = "none", **extra: Any) -> None:
    await browser.call("Input.dispatchMouseEvent", type=kind, x=x, y=y, button=button, **extra)


async def _press(browser: Browser, x: float, y: float) -> None:
    await _mouse(browser, "mouseMoved", x, y)
    await _mouse(browser, "mousePressed", x, y, "left", buttons=1, clickCount=1)
    await _mouse(browser, "mouseReleased", x, y, "left", buttons=0, clickCount=1)


async def _drag(browser: Browser, start: tuple[float, float], end: tuple[float, float], button: str) -> None:
    """Pressed at [start], moved smoothly to [end] (maps follow the moves, not the jump)."""
    held = BUTTONS[button]
    await _mouse(browser, "mouseMoved", *start)
    await _mouse(browser, "mousePressed", *start, button, buttons=held, clickCount=1)
    for i in range(1, DRAG_MOVES + 1):
        x = start[0] + (end[0] - start[0]) * i / DRAG_MOVES
        y = start[1] + (end[1] - start[1]) * i / DRAG_MOVES
        await _mouse(browser, "mouseMoved", x, y, button, buttons=held)
        await asyncio.sleep(0.02)
    await _mouse(browser, "mouseReleased", *end, button, buttons=0, clickCount=1)
