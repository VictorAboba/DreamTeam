# /// script
# requires-python = ">=3.11"
# dependencies = ["fonttools[woff]>=4.50"]
# ///
"""DreamTeam logo: a star assembled by a swarm of dots, a few of them still flying in.

    uv run scripts/logo.py --font jetbrains-mono-latin-700-normal.woff

writes docs/assets/logo-mark.svg (the star alone, one file for light and dark backgrounds) and
docs/assets/logo-light.svg / logo-dark.svg (the star with the "DreamTeam" wordmark, for GitHub's light and dark
themes), plus an -animated copy of each. The wordmark is JetBrains Mono Bold (SIL OFL,
github.com/JetBrains/JetBrainsMono) turned into outlines, because an SVG shown through <img> can't load fonts. Any
.ttf/.otf/.woff/.woff2 of it will do, but builds of the font differ by hundredths of a unit: the committed files come
from jetbrains-mono-latin-700-normal.woff of @fontsource/jetbrains-mono 5.3.0.

Every dot's place comes from a formula (a sunflower spiral cut to the star's outline), not from randomness, so the
star is the same on every run. In the animated files the dots start as a pale cloud around the star and swirl into
those places, the middle first and the rays last, and the grey stragglers arrive after them: CSS, played once, about
2.3 s, ending on exactly the static picture. With prefers-reduced-motion the static picture is shown at once. The cloud
and the timings come from seeded generators, so these files are also the same on every run."""

from __future__ import annotations

import argparse
import math
import random
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

# the animation
CLOUD_R = 30.5               # the cloud's radius: it stays inside the mark's square and off the wordmark
TURN = (70.0, 30.0)          # degrees a dot turns about the star's centre on its way: +TURN[1] in the middle, ±9
FLY = 1.2                    # seconds a star dot flies; it sets off within 0.6 s, the middle first
STRAY = (0.55, 1.75)         # the stragglers: when the first sets off, how long each flies
STRAY_PATH = (14.0, 10.0)    # they come from that much further out along their arcs: radius, degrees
EASE = "cubic-bezier(.45,.05,.25,1)"


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


def turned(vx: float, vy: float, a: float) -> tuple[float, float]:
    """The vector turned by a radians (clockwise on screen, as CSS rotate() turns it)."""
    c, s = math.cos(a), math.sin(a)
    return c * vx - s * vy, s * vx + c * vy


def offset(x: float, y: float, sx: float, sy: float, a: float) -> tuple[float, float]:
    """(dx, dy) such that rotate(a) translate(dx, dy) about the star's centre moves (x, y) to (sx, sy)."""
    cx, cy = STAR
    vx, vy = turned(sx - cx, sy - cy, -a)
    return vx - (x - cx), vy - (y - cy)


def cloud(n: int) -> list[tuple[float, float]]:
    """n points spread loosely over a disc around the star: a sunflower shaken a little."""
    cx, cy = STAR
    rnd, points = random.Random(5), []
    for i in range(n):
        r = CLOUD_R * math.sqrt((i + 0.5) / n) * (0.85 + 0.3 * rnd.random())
        a = i * GOLDEN_ANGLE * 1.37 + rnd.uniform(-0.5, 0.5)
        points.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return points


def flights() -> list[tuple[float, float, float, float]]:
    """For each star dot: (dx, dy, turn in degrees, delay in seconds). rotate(turn) translate(dx, dy) about the star's
    centre puts the dot in the cloud; the animation takes that transform back to none, which carries the dot along an
    arc. Each dot gets the free cloud point nearest to where its turn would start it, the outer dots choosing first."""
    cx, cy = STAR
    dots, rnd = swarm_dots(), random.Random(11)
    dist = [math.hypot(x - cx, y - cy) / STAR_R for x, y, _ in dots]
    turns, wanted = [], []
    for (x, y, _), d in zip(dots, dist):
        a = math.radians(TURN[0] + TURN[1] * (1 - d) + rnd.uniform(-9, 9))
        k = 1.25 + 0.5 * (1 - d)  # the middle starts further out, so the cloud is a cloud and not a bigger star
        vx, vy = turned(x - cx, y - cy, a)
        turns.append(a)
        wanted.append((cx + vx * k, cy + vy * k))
    free, start = cloud(len(dots)), {}
    for i in sorted(range(len(dots)), key=lambda i: -dist[i]):
        wx, wy = wanted[i]
        start[i] = free.pop(min(range(len(free)), key=lambda j: (free[j][0] - wx) ** 2 + (free[j][1] - wy) ** 2))
    res = []
    for i, ((x, y, _), d, a) in enumerate(zip(dots, dist, turns)):
        res.append((*offset(x, y, *start[i], a), math.degrees(a), 0.4 * d + rnd.uniform(0, 0.2)))
    return res


def stray_flights() -> list[tuple[float, float, float, float]]:
    """The same for the stragglers: each comes from further out along its own arc, the inner ones of an arc first."""
    cx, cy = STAR
    res = []
    for k, (x, y, _, _) in enumerate(STRAGGLERS):
        r, a = math.hypot(x - cx, y - cy) + STRAY_PATH[0], math.atan2(y - cy, x - cx)
        turn = math.radians(STRAY_PATH[1])
        dx, dy = offset(x, y, cx + r * math.cos(a + turn), cy + r * math.sin(a + turn), turn)
        res.append((dx, dy, STRAY_PATH[1], STRAY[0] + 0.05 * (k if k < 5 else k - 5)))
    return res


def animated_mark_body() -> str:
    cx, cy = STAR

    def circle(attrs: str, dx: float, dy: float, turn: float, delay: float) -> str:
        return f'<circle {attrs} style="--x:{num(dx)};--y:{num(dy)};--a:{round(turn)};--t:{num(delay)}s"/>'

    star = "".join(circle(f'cx="{num(x)}" cy="{num(y)}" r="{num(r)}"', *f)
                   for (x, y, r), f in zip(swarm_dots(), flights()))
    flying = "".join(circle(f'cx="{num(x)}" cy="{num(y)}" r="{num(r)}" opacity="{num(o)}"', *f)
                     for (x, y, r, o), f in zip(STRAGGLERS, stray_flights()))
    # Without the media query (or with reduced motion) nothing moves and the circles simply stand in their places.
    style = ("<style>@media (prefers-reduced-motion:no-preference){"
             f"circle{{transform-box:view-box;transform-origin:{num(cx)}px {num(cy)}px;"
             f"animation:f var(--d,{num(FLY)}s) {EASE} var(--t) both}}"
             "@keyframes f{from{transform:rotate(calc(var(--a)*1deg)) translate(calc(var(--x)*1px),calc(var(--y)*1px));"
             "opacity:var(--o,.35)}}}</style>")
    return (f'{style}<g fill="{ACCENT}">{star}</g>'
            f'<g fill="{GREY}" style="--o:0;--d:{num(STRAY[1])}s">{flying}</g>')


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

    d, w = wordmark(args.font)
    written = []
    for suffix, mark in (("", mark_body()), ("-animated", animated_mark_body())):
        (args.out / f"logo-mark{suffix}.svg").write_text(svg(SIZE, mark), encoding="utf-8")
        written.append(f"logo-mark{suffix}.svg")
        for theme, color in (("light", TEXT_ON_LIGHT), ("dark", TEXT_ON_DARK)):
            text = f'<path transform="translate({num(SIZE + GAP)} {num((SIZE - CAP) / 2)})" d="{d}" fill="{color}"/>'
            (args.out / f"logo-{theme}{suffix}.svg").write_text(svg(SIZE + GAP + w + 1, mark + text), encoding="utf-8")
            written.append(f"logo-{theme}{suffix}.svg")
    print(f"{len(swarm_dots())} dots; wrote {', '.join(written)} to {args.out}")


if __name__ == "__main__":
    main()
