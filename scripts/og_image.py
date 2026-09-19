#!/usr/bin/env python3
"""Shared Open Graph preview-card renderer (1200 x 630 px).

One Moroccan-styled card per deliverable, drawn with matplotlib so the link
previews on Discord / WhatsApp / X / Facebook carry the report's identity.
"""
from __future__ import annotations

import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.image as mpimg
import numpy as np
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.patches import Circle, Polygon, Rectangle

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from moroccan_theme import CREAM, GOLD, GREEN, RED, RED_DEEP  # noqa: E402

W, H = 1200, 630


def _pentagram(cx, cy, r, theta0=90.0):
    """Vertices of the interlaced pentagram, in the flag's draw order."""
    pts = []
    for k in range(5):
        th = np.radians(theta0 + k * 72)
        pts.append((cx + r * np.cos(th), cy + r * np.sin(th)))
    return [pts[i] for i in (0, 2, 4, 1, 3)]


def render_card(path: str, kicker: str, title: str, subtitle: str) -> str:
    """Render one 1200x630 card to *path* and return it."""
    fig = plt.figure(figsize=(W / 100, H / 100), dpi=100)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, W)
    ax.set_ylim(0, H)
    ax.axis("off")

    # Flag-red gradient (deep bottom -> bright top).
    grad = np.linspace(0, 1, 256).reshape(256, 1)
    ax.imshow(grad, extent=[0, W, 0, H], aspect="auto",
              cmap=LinearSegmentedColormap.from_list("mor", [RED_DEEP, RED]),
              origin="lower", zorder=0)

    # Thin gold frame.
    ax.add_patch(Rectangle((14, 14), W - 28, H - 28, fill=False,
                           edgecolor=GOLD, linewidth=3, alpha=0.85, zorder=1))

    # Star of Morocco: green interlaced pentagram, left third.
    cx, cy, r = 215, 315, 150
    ax.add_patch(Polygon(_pentagram(cx, cy, r), closed=True, fill=False,
                         edgecolor=GREEN, linewidth=30, joinstyle="miter",
                         capstyle="butt", zorder=3))
    ax.add_patch(Circle((cx, cy), 13, facecolor=CREAM, edgecolor="none",
                        alpha=0.9, zorder=4))

    # Text block on the right two-thirds.
    tx = 430
    ax.text(tx, 468, kicker, fontsize=29, color=GOLD, fontweight="bold",
            va="center", ha="left", zorder=5, family="DejaVu Sans")
    ax.add_patch(Rectangle((tx, 448), 88, 6, facecolor=GOLD, zorder=5))
    ax.text(tx, 340, title, fontsize=50, color="white", fontweight="bold",
            va="center", ha="left", zorder=5, family="DejaVu Sans")
    ax.text(tx, 250, subtitle, fontsize=27, color=CREAM,
            va="center", ha="left", zorder=5, family="DejaVu Sans")

    fig.savefig(path, dpi=100, facecolor=RED)
    plt.close(fig)

    # Flatten any alpha channel onto flag red, so crawlers that dislike RGBA
    # get an opaque RGB card.
    _arr = mpimg.imread(path)
    if _arr.ndim == 3 and _arr.shape[2] == 4:
        _rgb = _arr[..., :3]
        _a = _arr[..., 3:4]
        _bg = np.array([193, 39, 45], dtype=float) / 255.0
        _rgb = _rgb * _a + _bg * (1 - _a)
        mpimg.imsave(path, _rgb)
    return str(path)


if __name__ == "__main__":
    OUT = ROOT / "output"
    OUT.mkdir(parents=True, exist_ok=True)
    render_card(
        str(OUT / "og-report.png"),
        "MOROCCO \u00b7 LEGISLATIVE ELECTION \u00b7 23 SEPT 2026",
        "Electoral Claims & Promises",
        "A sourced, reproducible accountability report",
    )
    render_card(
        str(OUT / "og-simulator.png"),
        "MOROCCO \u00b7 CHAMBRE DES REPR\u00c9SENTANTS \u00b7 395 SEATS",
        "Seat Simulator",
        "Edit parties, enter votes, watch the seats fall",
    )
    print("[ok] OG images -> output/og-report.png, output/og-simulator.png")
