#!/usr/bin/env python3
"""Shared Moroccan visual identity for the report and the seat simulator.

One place for the flag palette, the green interlaced pentagram (the star of
Morocco) and the eight-point zellige tile used as a watermark, so the two
HTML deliverables cannot drift apart.

The SVGs are emitted as ``data:`` URIs for CSS ``background-image``; the URL
encoding happens once, here, rather than at every use site.
"""
from __future__ import annotations

from urllib.parse import quote

# Moroccan flag colours, plus the brass/gold and warm neutrals used everywhere.
RED = "#c1272d"
RED_DEEP = "#8e1b20"
GREEN = "#006233"
GOLD = "#c8a24a"
SAND = "#efe8d8"        # table headers, chips, diagonal cells
CREAM = "#fdfaf3"       # chart and map paper
BG = "#fbf8f1"          # page background
INK = "#1c1a17"
MUTED = "#6d6459"
LINE = "#e4dbc9"
GRID = "#e4dccb"
RULE = "#cdbfa6"        # connector lines and quadrant rules
BAND = "#eaf2e5"        # "shared by 2+ parties" band

# Sequential scale for "how much": pale green (little) -> brass -> flag red (a lot).
CMAP_COLORS = ["#f2f7ef", "#cfe3cd", GOLD, RED, RED_DEEP]

# Semantic colours for status text and badges.
OK = GREEN
WARN = "#a8741a"
BAD = RED

# ``__TOKEN__`` placeholders substituted into the CSS/JS of both deliverables so
# the palette has exactly one definition.
TOKENS = {
    "__RED__": RED, "__RED_DEEP__": RED_DEEP, "__GREEN__": GREEN, "__GOLD__": GOLD,
    "__SAND__": SAND, "__CREAM__": CREAM, "__BG__": BG, "__INK__": INK,
    "__MUTED__": MUTED, "__LINE__": LINE, "__GRID__": GRID, "__RULE__": RULE,
    "__BAND__": BAND,
}


def apply_tokens(text: str) -> str:
    for key, value in TOKENS.items():
        text = text.replace(key, value)
    return text

# -- the star of Morocco: a green interlaced pentagram, as on the flag --------
STAR_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='-12 -12 24 24'>"
    "<path d='M0 -10 L5.88 8.09 L-9.51 -3.09 L9.51 -3.09 L-5.88 8.09 Z' "
    "fill='none' stroke='%s' stroke-width='2.1' stroke-linejoin='miter'/>"
    "</svg>" % GREEN)

# -- zellige: a tiling eight-point star, watermarked over the red masthead ----
ZELLIGE_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' width='74' height='74' viewBox='0 0 72 72'>"
    "<g fill='none' stroke='#f6e0aa' stroke-opacity='0.30' stroke-width='1.15'>"
    "<path d='M65 36 L47.09 40.59 L56.51 56.51 L40.59 47.09 L36 65 L31.41 47.09 "
    "L15.49 56.51 L24.91 40.59 L7 36 L24.91 31.41 L15.49 15.49 L31.41 24.91 "
    "L36 7 L40.59 24.91 L56.51 15.49 L47.09 31.41 Z'/>"
    "<circle cx='36' cy='36' r='3.4' fill='#f6e0aa' fill-opacity='0.30' stroke='none'/>"
    "</g></svg>")

STAR_URI = "data:image/svg+xml," + quote(STAR_SVG, safe="")
ZELLIGE_URI = "data:image/svg+xml," + quote(ZELLIGE_SVG, safe="")

# -- the flag rendered as a badge: green pentagram on a red disc ---------------
STAR_BADGE_SVG = (
    '<svg viewBox="0 0 120 120" role="img" aria-label="Star of Morocco">'
    '<circle cx="60" cy="60" r="57" fill="#fdfaf3"/>'
    '<circle cx="60" cy="60" r="57" fill="none" stroke="%s" stroke-width="2"/>'
    '<circle cx="60" cy="60" r="49" fill="%s"/>'
    '<path d="M60 30 L77.63 84.27 L31.47 50.73 L88.53 50.73 L42.37 84.27 Z" '
    'fill="none" stroke="%s" stroke-width="7" '
    'stroke-linejoin="miter" stroke-linecap="square"/></svg>' % (GOLD, RED, GREEN))
