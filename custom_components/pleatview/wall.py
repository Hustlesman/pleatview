"""A wall of YouTube streams, served by Home Assistant itself: /api/pleatview/wall?v=ID,ID,…

A Webpage dashboard with that address shows the streams side by side, muted, on any
screen: the TV app (which builds the same wall itself, one process per row), the
renderer, the Linux app and browsers. Nothing leaves your network but the players
themselves, which come from YouTube's privacy-enhanced mode.
"""

from __future__ import annotations

import html
import math
import re

from aiohttp import web

from homeassistant.components.http import HomeAssistantView

MAX_STREAMS = 16
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")

HELP = """<p>This page shows YouTube streams side by side, muted. Put their video IDs after <code>?v=</code>,
separated by commas, such as <code>/api/pleatview/wall?v=ID,ID,ID,ID</code>.</p>
<p>An ID is the part of a video's address after <code>watch?v=</code>, <code>/live/</code> or <code>youtu.be/</code>.
The <a href="https://github.com/Hustlesman/pleatview/blob/main/docs/ideas/live-streams.md">live streams guide</a>
shows how to put a wall on a TV.</p>"""


def video_ids(value: str) -> list[str]:
    """The valid video IDs in a comma-separated list, at most MAX_STREAMS."""
    return [part for part in (p.strip() for p in value.split(",")) if VIDEO_ID.fullmatch(part)][:MAX_STREAMS]


def wall_html(ids: list[str]) -> str:
    """The page: a grid as square as it can be, one muted player per stream."""
    if not ids:
        body = f'<main style="max-width:40em;margin:3em auto;font:18px/1.5 sans-serif">{HELP}</main>'
        style = "html,body{margin:0;background:#fff;color:#222}"
    else:
        columns = math.ceil(math.sqrt(len(ids)))
        rows = math.ceil(len(ids) / columns)
        # Home Assistant sends no referrer (Referrer-Policy: no-referrer), and YouTube won't
        # play without one (error 153): each player asks for one of its own.
        # IDs are checked already (VIDEO_ID); escaped all the same, so the page never depends on it.
        body = "".join(
            f'<iframe src="https://www.youtube-nocookie.com/embed/{video}?autoplay=1&amp;mute=1&amp;controls=0'
            f'&amp;playsinline=1&amp;rel=0&amp;loop=1&amp;playlist={video}" allow="autoplay; encrypted-media"'
            ' referrerpolicy="strict-origin-when-cross-origin"></iframe>'
            for video in (html.escape(i, quote=True) for i in ids)
        )
        style = (
            "html,body{margin:0;height:100%;background:#000;overflow:hidden}"
            f"body{{display:grid;gap:4px;grid-template-columns:repeat({columns},1fr);grid-template-rows:repeat({rows},1fr)}}"
            "iframe{width:100%;height:100%;border:0}"
        )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>PleatView wall</title><style>{style}</style></head><body>{body}</body></html>"
    )


class WallView(HomeAssistantView):
    """The wall page. It holds no data of Home Assistant's, so it needs no sign-in."""

    url = "/api/pleatview/wall"
    name = "api:pleatview:wall"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        return web.Response(
            text=wall_html(video_ids(request.query.get("v", ""))),
            content_type="text/html",
            headers={"Cache-Control": "no-cache"},
        )
