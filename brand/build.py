"""Builds every PleatView brand asset from one description of the mark and the wordmark.

Run tools/build-brand.sh. It writes:

    brand/*.svg                                       the sources, with the wordmark as outlines
    brand/pleatview-mark.png                          the mark as a transparent PNG
    android/app/src/main/res/drawable-*/              launcher icon foreground and TV banner
    site/favicon.svg, site/img/                       website icon and logo, Apple touch icon, Play Store images
    brand/console/                                    PleatView Console's web app icons (copied into its repository)
    custom_components/pleatview/brand/                Home Assistant (and its Marketplace)
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import uharfbuzz as hb
from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
FONTS = Path("/fonts")

CYAN = "#18BCF2"   # bezel, stand, front panels, "View"
DEEP = "#0C7FAB"   # the panel that folds away
INK = "#0F1417"    # backgrounds; "Pleat" on light backgrounds
WHITE = "#FFFFFF"  # "Pleat" on dark backgrounds
MUTED = "#9AA7AE"  # taglines on dark backgrounds

# The mark on a 512 grid: a TV whose screen is three pleated panels, on a stand.
MARK = f"""<rect x="71" y="121" width="370" height="226" rx="27" fill="none" stroke="{CYAN}" stroke-width="22"/>
  <polygon points="108,168 207,154 207,314 108,300" fill="{CYAN}"/>
  <polygon points="207,154 305,168 305,300 207,314" fill="{DEEP}"/>
  <polygon points="305,168 404,154 404,314 305,300" fill="{CYAN}"/>
  <rect x="240" y="352" width="32" height="28" fill="{CYAN}"/>
  <rect x="172" y="376" width="168" height="26" rx="13" fill="{CYAN}"/>"""
MARK_X, MARK_Y, MARK_W, MARK_H = 60, 110, 392, 292  # the mark's ink; its centre is the grid's centre


class Text:
    """A line of text as outlines, shaped with HarfBuzz so kerning matches the font."""

    def __init__(self, text: str, font: str) -> None:
        path = str(FONTS / font)
        tt = TTFont(path)
        self.cap = tt["OS/2"].sCapHeight
        buffer = hb.Buffer()
        buffer.add_str(text)
        buffer.guess_segment_properties()
        hb.shape(hb.Font(hb.Face(hb.Blob.from_file_path(path))), buffer, {"kern": True})
        glyphs, order = tt.getGlyphSet(), tt.getGlyphOrder()
        bounds = BoundsPen(glyphs)
        self.glyphs: list[tuple[int, str]] = []  # (character index, outline in font units, y down)
        x = 0
        for info, position in zip(buffer.glyph_infos, buffer.glyph_positions):
            glyph = glyphs[order[info.codepoint]]
            pen = SVGPathPen(glyphs)
            glyph.draw(TransformPen(pen, (1, 0, 0, -1, x + position.x_offset, -position.y_offset)))
            glyph.draw(TransformPen(bounds, (1, 0, 0, 1, x + position.x_offset, position.y_offset)))
            self.glyphs.append((info.cluster, pen.getCommands()))
            x += position.x_advance
        self.left, _, self.right, _ = bounds.bounds

    def width(self, cap_height: float) -> float:
        return (self.right - self.left) * cap_height / self.cap

    def svg(self, x: float, baseline: float, cap_height: float, colors: list[tuple[int, str]]) -> str:
        """Ink starting at x, capitals cap_height tall. colors: (first character, fill) runs."""
        scale = cap_height / self.cap
        runs: dict[str, list[str]] = {}
        for cluster, outline in self.glyphs:
            fill = [color for start, color in colors if start <= cluster][-1]
            runs.setdefault(fill, []).append(outline)
        paths = "".join(f'<path fill="{fill}" d="{"".join(outlines)}"/>' for fill, outlines in runs.items())
        return f'<g transform="translate({x - self.left * scale:.2f} {baseline:.2f}) scale({scale:.5f})">{paths}</g>'


WORDMARK = Text("PleatView", "Poppins-SemiBold.ttf")
TAGLINE = Text("Home Assistant dashboards on your TV", "Poppins-Medium.ttf")


def mark(x: float, y: float, height: float) -> str:
    """The mark with its ink at (x, y), `height` tall."""
    k = height / MARK_H
    return f'<g transform="translate({x:.2f} {y:.2f}) scale({k:.5f}) translate({-MARK_X} {-MARK_Y})">{MARK}</g>'


def lockup(x: float, y: float, height: float, pleat: str) -> tuple[str, float]:
    """Mark and wordmark, the capitals half the mark's height and centred on it. Returns (svg, width)."""
    cap = 0.5 * height
    word_x = x + MARK_W * height / MARK_H + 0.33 * height
    word = WORDMARK.svg(word_x, y + height / 2 + cap / 2, cap, [(0, pleat), (5, CYAN)])
    return mark(x, y, height) + word, word_x - x + WORDMARK.width(cap)


def document(width: float, height: float, body: str, view_box: str | None = None, background: str | None = None) -> str:
    fill = f'<rect x="-10000" y="-10000" width="20000" height="20000" fill="{background}"/>' if background else ""
    box = view_box or f"0 0 {width:g} {height:g}"
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{box}" width="{width:g}" height="{height:g}">'
            f"{fill}{body}</svg>\n")


def write(path: str, text: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    print("wrote", path)


def render(path: str, svg: str, width: int, height: int, opaque: bool = False) -> None:
    """PNG from SVG; opaque drops the alpha channel (the stores want 24-bit images)."""
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".svg") as source:
        source.write(svg)
        source.flush()
        subprocess.run(["rsvg-convert", "-w", str(width), "-h", str(height), "-o", str(target), source.name], check=True)
    if opaque:
        Image.open(target).convert("RGB").save(target, optimize=True)
    print("rendered", path, f"{width}x{height}")


def centred(width: float, height: float, mark_height: float, tagline: bool) -> str:
    """A dark canvas with the logo (and the tagline under the wordmark) in the middle."""
    cap = 0.5 * mark_height
    logo, logo_width = lockup(0, 0, mark_height, WHITE)
    text_x = MARK_W * mark_height / MARK_H + 0.33 * mark_height
    tag_cap = 0.17 * mark_height
    block_width = max(logo_width, text_x + TAGLINE.width(tag_cap)) if tagline else logo_width
    block_height = mark_height * (1.22 if tagline else 1)  # the tagline's descenders end at 1.22
    x, y = (width - block_width) / 2, (height - block_height) / 2
    body = f'<g transform="translate({x:.2f} {y:.2f})">{logo}'
    if tagline:
        # Under the wordmark: its baseline sits where the capitals end, plus a gap.
        baseline = mark_height / 2 + cap / 2 + 0.24 * mark_height + tag_cap
        body += TAGLINE.svg(text_x, baseline, tag_cap, [(0, MUTED)])
    return document(width, height, body + "</g>", background=INK)


def main() -> None:
    icon = document(512, 512, f'<rect width="512" height="512" rx="112" fill="{INK}"/>{MARK}')

    # Sources. The logo is 200 units tall; its width follows from the wordmark.
    mark = document(MARK_W, MARK_H, MARK, f"{MARK_X} {MARK_Y} {MARK_W} {MARK_H}")
    write("brand/pleatview-mark.svg", mark)
    # The mark alone on a transparent background, at 4x, for documents, slides and profiles.
    render("brand/pleatview-mark.png", mark, MARK_W * 4, MARK_H * 4)
    write("brand/pleatview-icon.svg", icon)
    for name, pleat in (("on-dark", WHITE), ("on-light", INK)):
        body, width = lockup(0, 0, 200, pleat)
        write(f"brand/pleatview-logo-{name}.svg", document(round(width, 1), 200, body))

    # Website.
    write("site/favicon.svg", icon)
    body, width = lockup(0, 0, 200, WHITE)
    write("site/img/logo.svg", document(round(width, 1), 200, body))
    render("site/img/pleatview-icon-512.png", icon, 512, 512)
    full_bleed = document(180, 180, MARK, "-24 -24 560 560", background=INK)  # iOS rounds the corners itself
    render("site/img/apple-touch-icon.png", full_bleed, 180, 180, opaque=True)

    # PleatView Console's web app (its own repository copies them from here): icons for
    # installing it, and a full-bleed one that Android masks to its own shape (the mark
    # inside the safe circle).
    console = "brand/console"
    write(f"{console}/icon.svg", icon)
    render(f"{console}/icon-192.png", icon, 192, 192)
    render(f"{console}/icon-512.png", icon, 512, 512)
    maskable = document(512, 512, MARK, "-41 -41 594 594", background=INK)
    render(f"{console}/icon-maskable-512.png", maskable, 512, 512, opaque=True)

    # Google Play: a full square icon (Play masks it), the feature graphic, the TV banner.
    render("site/img/play/icon-512.png", document(512, 512, MARK, "-41 -41 594 594", background=INK), 512, 512, opaque=True)
    render("site/img/play/feature-graphic.png", centred(1024, 500, 115, tagline=True), 1024, 500, opaque=True)
    render("site/img/play/tv-banner.png", centred(1280, 720, 140, tagline=True), 1280, 720, opaque=True)

    # GitHub: the repository's social preview (Settings → General → Social preview), 2:1.
    render("brand/github-social-preview.png", centred(1280, 640, 140, tagline=True), 1280, 640, opaque=True)

    # Android: the adaptive icon's foreground (the mark half as wide as the 108 dp canvas, inside the
    # 66 dp safe circle) as bitmaps, which launchers such as Fire TV's can decode, and the TV banner.
    foreground = document(108, 108, MARK, "-136 -136 784 784")
    for density, factor in (("mdpi", 1), ("hdpi", 1.5), ("xhdpi", 2), ("xxhdpi", 3), ("xxxhdpi", 4)):
        size = round(108 * factor)
        render(f"android/app/src/main/res/drawable-{density}/ic_launcher_foreground.png", foreground, size, size)
    render("android/app/src/main/res/drawable-xhdpi/banner.png", centred(320, 180, 48, tagline=False), 320, 180, opaque=True)

    # Home Assistant brand folder: a square icon, and logos for light and dark themes.
    brand = "custom_components/pleatview/brand"
    square = document(256, 256, MARK, f"{MARK_X} {MARK_X} {MARK_W} {MARK_W}")
    render(f"{brand}/icon.png", square, 256, 256)
    render(f"{brand}/icon@2x.png", square, 512, 512)
    for prefix, pleat in (("", INK), ("dark_", WHITE)):
        body, width = lockup(0, 0, 200, pleat)
        logo = document(round(width, 1), 200, body)
        render(f"{brand}/{prefix}logo.png", logo, round(width * 128 / 200), 128)
        render(f"{brand}/{prefix}logo@2x.png", logo, round(width * 256 / 200), 256)


if __name__ == "__main__":
    main()
