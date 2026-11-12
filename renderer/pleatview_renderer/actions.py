"""Steps the renderer takes on a page after it loads: clicks, drags, scrolls and waits.

For pages with no settings in their address, such as a map's view, a cookie banner or a side
bar. They're written in the page's address, after `#pleatview=`, so a dashboard, a Webpage
dashboard or `pleatview.show_url` carries them with no setting anywhere else:

    https://example.com/map#pleatview=click:Reject all;click:Collapse;drag:70,60,35,45

A click names the text it presses, a CSS selector (`click:css=#consent .accept`), or a place.
Places are percentages of the screen's width and height, so the steps fit every TV size. The
Android and Linux apps take the same steps on the pages they show themselves.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
from pathlib import Path
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
    css: str = ""  # a click on what this CSS selector matches


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
            if arg.lower().startswith("css="):
                selector = arg[4:].strip()
                if not selector or len(selector) > 200:
                    raise ValueError("click:css= takes a CSS selector, such as #consent .accept")
                steps.append(Step("click", css=selector))
                continue
            try:
                steps.append(Step("click", numbers=_numbers(arg, 2, 0, 100, "")))
            except ValueError:
                if not arg or len(arg) > 200:
                    raise ValueError("click takes the text of what to click, css= and a selector, or x,y in percent") from None
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


# Where a click presses: shared with the Android and Linux apps (find.js).
FIND = (Path(__file__).parent / "find.js").read_text()


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
            where = point(*step.numbers) if step.numbers else await _find(browser, step)
            if where is None:
                _LOGGER.info("%s: nothing showing %r to click", name, step.css or step.text)
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


async def _find(browser: Browser, step: Step) -> tuple[float, float] | None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + FIND_SECONDS
    wanted = {"css": step.css} if step.css else {"text": step.text}
    while True:
        found = await _value(browser, f"({FIND.strip()})({json.dumps(wanted)})")
        if found == "bad selector":
            _LOGGER.info("the page can't read the selector %r", step.css)
            return None
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
