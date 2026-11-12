#!/usr/bin/env python3
"""Puts the latest release's version and date into the website.

The website's deploy (.github/workflows/pages.yml) runs this on every release, so the
site always says which version is current and when it came out. Elements mark what to
fill in:

    <b data-release="version">v1.0.17</b>
    <time data-release="date" datetime="2026-11-12">12 November 2026</time>
    <a data-release="notes" href="https://github.com/…/releases/tag/v1.0.17">What's new</a>

    tools/site-release.py site                         # from GitHub's latest release
    tools/site-release.py site --tag v1.0.18 --date 2026-11-13

The date is the day it was published in New Zealand, where releases are made.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime
import json
import os
from pathlib import Path
import re
import urllib.request
from zoneinfo import ZoneInfo

REPO = "Hustlesman/pleatview"
ZONE = ZoneInfo("Pacific/Auckland")


def latest() -> tuple[str, date]:
    request = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases/latest",
                                     headers={"Accept": "application/vnd.github+json", "User-Agent": "pleatview-site"})
    if token := os.environ.get("GH_TOKEN"):
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=30) as response:
        release = json.load(response)
    published = datetime.fromisoformat(release["published_at"].replace("Z", "+00:00"))
    return release["tag_name"], published.astimezone(ZONE).date()


def fill(html: str, tag: str, day: date) -> str:
    """The marked elements, with this release in them."""
    notes = f"https://github.com/{REPO}/releases/tag/{tag}"
    written = f"{day.day} {day:%B %Y}"
    html = re.sub(r'(<(\w+)[^>]*\bdata-release="version"[^>]*>)[^<]*(</\2>)', rf"\g<1>{tag}\g<3>", html)
    html = re.sub(r'(<time[^>]*\bdata-release="date"[^>]*>)[^<]*(</time>)', rf"\g<1>{written}\g<2>", html)
    html = re.sub(r'(<time[^>]*\bdata-release="date"[^>]*\bdatetime=")[^"]*(")', rf"\g<1>{day.isoformat()}\g<2>", html)
    html = re.sub(r'(<a[^>]*\bdata-release="notes"[^>]*\bhref=")[^"]*(")', rf"\g<1>{notes}\g<2>", html)
    return html


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("site", type=Path, help="the website's folder")
    parser.add_argument("--tag", help="a release tag, instead of GitHub's latest")
    parser.add_argument("--date", type=date.fromisoformat, help="its release date (YYYY-MM-DD)")
    args = parser.parse_args()
    if bool(args.tag) != bool(args.date):
        parser.error("--tag and --date go together")
    tag, day = (args.tag, args.date) if args.tag else latest()
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise SystemExit(f"unexpected release tag {tag!r}")
    changed = []
    for page in sorted(args.site.glob("*.html")):
        html = page.read_text()
        filled = fill(html, tag, day)
        if filled != html:
            page.write_text(filled)
            changed.append(page.name)
    print(f"{tag}, {day.isoformat()}: " + (", ".join(changed) if changed else "nothing to change"))


if __name__ == "__main__":
    main()
