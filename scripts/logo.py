# /// script
# requires-python = ">=3.11"
# dependencies = ["fonttools[woff]>=4.50"]
# ///
"""DreamTeam logo: a star assembled by a swarm of dots, a few of them still flying in.

    uv run scripts/logo.py --font JetBrainsMono-Bold.ttf

writes docs/assets/logo-mark.svg (the star alone, one file for light and dark backgrounds) and
docs/assets/logo-light.svg / logo-dark.svg (the star with the "DreamTeam" wordmark, for GitHub's light and dark
themes). The wordmark is JetBrains Mono Bold (SIL OFL, github.com/JetBrains/JetBrainsMono) turned into outlines,
because an SVG shown through <img> can't load fonts. Any .ttf/.otf/.woff/.woff2 of it will do.

Every dot's place comes from a formula (a sunflower spiral cut to the star's outline), not from randomness, so the
star is the same on every run and its dots can later be animated from a scattered cloud into place."""

from __future__ import annotations

import argparse
import math
from pathlib import Path

ACCENT = "#d97757"           # the accent of the board and the terminal UI
GREY = "#85858d"             # readable on white and on GitHub's dark background alike
TEXT_ON_LIGHT = "#1f2328"    # GitHub's text colours, light and dark theme
TEXT_ON_DARK = "#e6edf3"
GOLDEN_ANGLE = math.pi * (3 - math.sqrt(5))

SIZE = 64                    # the mark's viewBox is SIZE x SIZE
STAR = (31.0, 33.0)          # centre of the star
STAR_R = 31.5                # half its height; its width is STAR_W of that
STAR_W = 0.88
STAR_P = 0.56                # outline |x|^p + |y|^p = 1: below 1 the sides curve in and the rays sharpen
SPACING = 1.66               # distance between neighbouring dots, roughly SPACING * 1.77
DOT_R = (0.45, 1.4)          # dot radius at the outline and in the middle
# dots still on their way: (x, y, radius, opacity), along two arcs into the star
STRAGGLERS = [
    (46.6, 16.4, 1.2, .9), (50.6, 12.3, 1.05, .75), (54.9, 9.1, 0.92, .6), (59.4, 6.8, 0.78, .45), (63.4, 5.6, 0.62, .3),
    (14.6, 52.4, 1.1, .8), (10.8, 56.0, 0.9, .6), (7.0, 58.8, 0.72, .4),
]

WORD = "DreamTeam"
CAP = 24.0                   # cap height of the wordmark, in the mark's units
GAP = 16.0                   # between the mark and the wordmark


def num(x: float) -> str:
    return f"{x:.2f}".rstrip("0").rstrip(".")


def swarm_dots() -> list[tuple[float, float, float]]:
    """(x, y, r) of the star's dots: a sunflower spiral, kept inside the outline, larger towards the middle."""
    cx, cy = STAR
    dots, i = [], 0
    while (r := SPACING * math.sqrt(i + 0.5)) <= STAR_R:
        x, y = cx + r * math.cos(i * GOLDEN_ANGLE), cy + r * math.sin(i * GOLDEN_ANGLE)
        i += 1
        v = (abs(x - cx) / (STAR_R * STAR_W)) ** STAR_P + (abs(y - cy) / STAR_R) ** STAR_P
        if v <= 1:
            dots.append((x, y, DOT_R[0] + (DOT_R[1] - DOT_R[0]) * (1 - v) ** 0.7))
    return dots


def mark_body() -> str:
    star = "".join(f'<circle cx="{num(x)}" cy="{num(y)}" r="{num(r)}"/>' for x, y, r in swarm_dots())
    flying = "".join(f'<circle cx="{num(x)}" cy="{num(y)}" r="{num(r)}" opacity="{num(o)}"/>'
                     for x, y, r, o in STRAGGLERS)
    return f'<g fill="{ACCENT}">{star}</g><g fill="{GREY}">{flying}</g>'


def wordmark(font_path: Path) -> tuple[str, float]:
    """The wordmark as one path with its cap height CAP and baseline at y=CAP; returns (d, width)."""
    from fontTools.pens.svgPathPen import SVGPathPen
    from fontTools.pens.transformPen import TransformPen
    from fontTools.ttLib import TTFont

    font = TTFont(str(font_path))
    glyphs, cmap = font.getGlyphSet(), font.getBestCmap()
    scale = CAP / (font["OS/2"].sCapHeight or font["head"].unitsPerEm * 0.7)
    x, parts = 0.0, []
    for ch in WORD:
        name = cmap[ord(ch)]
        pen = SVGPathPen(glyphs, ntos=num)
        glyphs[name].draw(TransformPen(pen, (scale, 0, 0, -scale, x, CAP)))
        parts.append(pen.getCommands())
        x += glyphs[name].width * scale
    return " ".join(parts), x


def svg(width: float, body: str) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {num(width)} {SIZE}" width="{num(width)}" '
            f'height="{SIZE}" role="img" aria-label="DreamTeam"><title>DreamTeam</title>{body}</svg>\n')


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--font", type=Path, required=True, help="JetBrains Mono Bold: .ttf, .otf, .woff or .woff2")
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parents[1] / "docs" / "assets")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    mark = mark_body()
    (args.out / "logo-mark.svg").write_text(svg(SIZE, mark), encoding="utf-8")
    d, w = wordmark(args.font)
    for theme, color in (("light", TEXT_ON_LIGHT), ("dark", TEXT_ON_DARK)):
        text = f'<path transform="translate({num(SIZE + GAP)} {num((SIZE - CAP) / 2)})" d="{d}" fill="{color}"/>'
        (args.out / f"logo-{theme}.svg").write_text(svg(SIZE + GAP + w + 1, mark + text), encoding="utf-8")
    print(f"{len(swarm_dots())} dots; wrote logo-mark.svg, logo-light.svg, logo-dark.svg to {args.out}")


if __name__ == "__main__":
    main()
