#!/usr/bin/env python3
"""
Morocco elections - report builder.

Reads the auditable CSVs in data/, computes the CEAGI party score with a
Monte-Carlo uncertainty pass, renders SVG charts, and assembles a single
self-contained HTML report plus an audit trail.

Two rules govern this script:

1. *Nothing is invented.* A sub-index that cannot be computed from the data is
   reported as ``n/a`` together with the reason. The old behaviour of quietly
   substituting 0.5 for a missing dimension is gone: that manufactured a score
   out of nothing, which is the one thing an accountability model must never do.
2. *The two years are not the same year.* Delivery (D) is a property of a
   completed term; claim credibility (C) is a property of the current campaign;
   electoral efficiency (E) comes from the last election that actually happened.
   ``campaign_year`` and ``baseline_year`` are therefore separate settings.

Usage
-----
    python3 scripts/build.py                 # validate, then build
    python3 scripts/build.py --force         # build even if validation errors
    python3 scripts/build.py --year 2026 --samples 20000
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = ROOT / "output"
CHARTS = OUT / "charts"
SCRIPTS = ROOT / "scripts"

# Matplotlib needs a writable config/cache directory. $HOME/.config is
# read-only in this environment, which spams a warning and slows every import.
_MPL_CACHE = Path(os.environ.get("MPLCONFIGDIR") or (ROOT / ".matplotlib"))
try:
    _MPL_CACHE.mkdir(parents=True, exist_ok=True)
    os.environ["MPLCONFIGDIR"] = str(_MPL_CACHE)
except OSError:
    pass

import numpy as np  # noqa: E402
import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

sys.path.insert(0, str(SCRIPTS))
import validate as validator  # noqa: E402

DIMS = ["D", "C", "E", "G", "L", "M"]
DIM_LABEL = {
    "D": "delivery", "C": "claim credibility", "E": "electoral efficiency",
    "G": "governance", "L": "leadership", "M": "mandate coherence",
}
DONE_STATUS = {"fulfilled", "partial", "failed", "abandoned"}

DEFAULTS = {
    "campaign_year": 2026,
    "baseline_year": 2021,
    "election_date": None,
    "house_seats": 395,
    "trajectory_years": [],
    "monte_carlo": {"n_samples": 8000, "seed": 42, "sigma": 0.05},
    "composite_weights": {"D": 0.25, "C": 0.20, "E": 0.10,
                          "G": 0.20, "L": 0.15, "M": 0.10},
    "claim_weights": {"Q": 1.0, "V": 0.4, "S": 0.1, "N": 0.2},
    "default_claim_weight": 0.4,
    "min_promises_for_D": 3,
    "promise_status_numeric": {
        "fulfilled": 1.0, "partial": 0.66, "in_progress": 0.55,
        "pending": 0.33, "failed": 0.0, "abandoned": 0.0, "unverifiable": 0.25,
    },
    "publication_gate": {
        "max_fill_fraction": 0.10,
        "min_scorable_claim_share": 0.5,
        "min_corroborated_claim_share": 0.5,
        "require_all_subindices": True,
    },
}

STATUS_COLOR = {
    "fulfilled": "#2e7d32", "partial": "#9ccc65", "in_progress": "#fbc02d",
    "pending": "#ffb74d", "failed": "#c62828", "abandoned": "#616161",
    "unverifiable": "#9e9e9e",
}


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def deep_merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Build the Morocco election report.")
    ap.add_argument("--config", default=str(ROOT / "config.json"),
                    help="path to a JSON config (default: config.json)")
    ap.add_argument("--year", type=int, help="override campaign_year")
    ap.add_argument("--baseline-year", type=int, help="override baseline_year")
    ap.add_argument("--samples", type=int, help="override Monte-Carlo sample count")
    ap.add_argument("--seed", type=int, help="override the RNG seed")
    ap.add_argument("--force", action="store_true",
                    help="build even when the validation gate reports errors")
    return ap.parse_args(argv)


ARGS = parse_args()
CFG = dict(DEFAULTS)
_cfg_path = Path(ARGS.config)
if _cfg_path.exists():
    CFG = deep_merge(CFG, json.loads(_cfg_path.read_text(encoding="utf-8")))
if ARGS.year:
    CFG["campaign_year"] = ARGS.year
if ARGS.baseline_year:
    CFG["baseline_year"] = ARGS.baseline_year
if ARGS.samples:
    CFG["monte_carlo"]["n_samples"] = ARGS.samples
if ARGS.seed is not None:
    CFG["monte_carlo"]["seed"] = ARGS.seed

CAMPAIGN_YEAR = CFG["campaign_year"]
BASELINE_YEAR = CFG["baseline_year"]
HOUSE_SEATS = CFG["house_seats"]
WEIGHTS = CFG["composite_weights"]
GATE = CFG["publication_gate"]

OUT.mkdir(parents=True, exist_ok=True)
CHARTS.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Loading helpers
# ---------------------------------------------------------------------------
def esc(value):
    return (str(value).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


is_missing = validator.is_fill


def load_table(name):
    """Load a CSV, normalising header names to snake_case keys.

    Handles the space in claims.csv's ``additional notes`` column and drops the
    overflow bucket a ragged row would otherwise create.
    """
    with open(DATA / name, newline="", encoding="utf-8") as fh:
        rows = []
        for raw in csv.DictReader(fh):
            row = {}
            for key, value in raw.items():
                if key is None:
                    continue
                row[key.strip().replace(" ", "_")] = (value or "").strip()
            rows.append(row)
        return rows


def as_float(value):
    if is_missing(value):
        return None
    text = str(value).strip().replace("%", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def as_int(value):
    f = as_float(value)
    if f is None:
        return None
    try:
        return int(f)
    except (ValueError, OverflowError):
        return None


parties = load_table("parties.csv")
elections = load_table("elections.csv")
claims = load_table("claims.csv")
promises = load_table("promises.csv")
leaders = load_table("leaders.csv")
sources = load_table("sources.csv")
timeline = load_table("timeline.csv")
indicators = load_table("indicators.csv")

pids = [p["party_id"] for p in parties if p["party_id"] != "P009"]
pname = {p["party_id"]: p["name"] for p in parties}
pcolor = {p["party_id"]: p["color"] for p in parties}
pslogan = {p["party_id"]: p.get("official_slogan", "") for p in parties}

# The claims bank now spans two elections: the live 2026 campaign (feeds C) and
# the 2021 campaign (historical record, not scored). Keep them apart so the
# publication gate measures the campaign, not the archive.
campaign_claims = [c for c in claims
                   if str(c.get("election_year")) == str(CAMPAIGN_YEAR)]
historical_claims = [c for c in claims
                     if str(c.get("election_year")) != str(CAMPAIGN_YEAR)]


# ---------------------------------------------------------------------------
# Validation gate
# ---------------------------------------------------------------------------
findings, _raw_tables, fill_census, row_counts = validator.validate()
sev_count = {s: sum(1 for f in findings if f.severity == s)
             for s in (validator.ERROR, validator.WARN, validator.INFO)}

if sev_count[validator.ERROR] and not ARGS.force:
    print(f"[abort] data validation failed with {sev_count[validator.ERROR]} "
          f"error(s). Run: python3 scripts/validate.py", file=sys.stderr)
    print("[abort] fix the errors, or pass --force to build anyway.", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Sub-index computation. Each returns a dict of {value, n, year, detail} where
# value is None when the evidence is not there.
# ---------------------------------------------------------------------------
def compute_delivery():
    out = {}
    min_n = CFG["min_promises_for_D"]
    for pid in pids:
        rows = [r for r in promises if r["party_id"] == pid]
        complete = [r for r in rows
                    if (as_int(r.get("term_end")) or 0) <= CAMPAIGN_YEAR]
        done = [r for r in complete if r.get("status") in DONE_STATUS]
        terms = sorted({f"{r.get('term_start')}-{r.get('term_end')}" for r in complete})
        term_txt = ", ".join(terms) if terms else "no completed term on record"
        if len(done) < min_n:
            out[pid] = {
                "value": None, "n": len(done), "year": BASELINE_YEAR,
                "detail": (f"{len(done)} concluded promise(s) tracked, "
                           f"{min_n} required to score; {term_txt}"),
            }
            continue
        F = sum(1 for r in done if r["status"] == "fulfilled")
        P = sum(1 for r in done if r["status"] == "partial")
        X = sum(1 for r in done if r["status"] == "failed")
        A = sum(1 for r in done if r["status"] == "abandoned")
        out[pid] = {
            "value": round((F + 0.5 * P) / len(done), 3), "n": len(done),
            "year": BASELINE_YEAR,
            "detail": (f"{F} fulfilled, {P} partial, {X} failed, {A} abandoned "
                       f"across {term_txt}"),
        }
    return out


def compute_credibility():
    out = {}
    weights = CFG["claim_weights"]
    default_w = CFG["default_claim_weight"]
    for pid in pids:
        rows = [c for c in claims
                if c["party_id"] == pid
                and str(c.get("election_year")) == str(CAMPAIGN_YEAR)]
        scored = [c for c in rows if as_float(c.get("verification_score")) is not None]
        if not scored:
            out[pid] = {
                "value": None, "n": 0, "year": CAMPAIGN_YEAR,
                "detail": (f"0/{len(rows)} campaign claims have a verification "
                           f"score; C cannot be computed"),
            }
            continue
        num = den = 0.0
        for c in scored:
            w = weights.get(str(c.get("class")).strip().upper(), default_w)
            num += w * float(as_float(c["verification_score"]))
            den += w
        out[pid] = {
            "value": round(num / (5 * den), 3), "n": len(scored), "year": CAMPAIGN_YEAR,
            "detail": f"{len(scored)}/{len(rows)} campaign claims scored (1-5 rubric)",
        }
    return out


def compute_efficiency(year):
    rows = [r for r in elections
            if as_int(r.get("election_year")) == year and r["party_id"] != "P009"]
    out, ratios = {}, {}
    if not rows:
        return out, ratios, 0, 0.0
    total_seats = sum(as_int(r.get("seats")) or 0 for r in rows)
    total_votes = sum(as_float(r.get("vote_share_est")) or 0.0 for r in rows)
    if not total_seats or not total_votes:
        return out, ratios, total_seats, total_votes
    for r in rows:
        pid = r["party_id"]
        s = (as_int(r.get("seats")) or 0) / total_seats
        v = (as_float(r.get("vote_share_est")) or 0.0) / total_votes
        ratios[pid] = s / v if v else 0.0
    maxdev = max(abs(a - 1) for a in ratios.values()) if ratios else 0.0
    for pid, ratio in ratios.items():
        value = 1.0 if maxdev <= 1e-9 else round(1 - abs(ratio - 1) / maxdev, 3)
        out[pid] = {
            "value": value, "n": 1, "year": year,
            "detail": (f"seat share / vote share = {ratio:.3f} at the {year} "
                       f"election"),
        }
    return out, ratios, total_seats, total_votes


def compute_indicators(year):
    out = {}
    for r in indicators:
        if as_int(r.get("election_year")) != year:
            continue
        basis = r.get("basis", "")
        for key, col in (("G", "g_score"), ("L", "l_score"), ("M", "m_score")):
            value = as_float(r.get(col))
            out.setdefault(r["party_id"], {})[key] = {
                "value": value, "n": 1, "year": year,
                "detail": basis or "assigned in data/indicators.csv (no basis given)",
            }
    return out


Dmap = compute_delivery()
Cmap = compute_credibility()
Emap, Aratio, total_seats, total_votes = compute_efficiency(BASELINE_YEAR)
Indmap = compute_indicators(BASELINE_YEAR)

sub = {}
for pid in pids:
    entry = {"D": Dmap[pid], "C": Cmap[pid]}
    entry["E"] = Emap.get(pid, {
        "value": None, "n": 0, "year": BASELINE_YEAR,
        "detail": f"no {BASELINE_YEAR} election row for this party",
    })
    for key in ("G", "L", "M"):
        entry[key] = Indmap.get(pid, {}).get(key, {
            "value": None, "n": 0, "year": BASELINE_YEAR,
            "detail": f"no {BASELINE_YEAR} indicator row for this party",
        })
    sub[pid] = entry


def geo(values, weights=WEIGHTS):
    """Weighted geometric mean over the keys present in `values`."""
    logsum = wsum = 0.0
    for key, weight in weights.items():
        if key not in values:
            continue
        logsum += weight * math.log(max(values[key], 1e-6))
        wsum += weight
    return math.exp(logsum / wsum) if wsum else 0.0


score, partial, coverage = {}, {}, {}
for pid in pids:
    values = {d: sub[pid][d]["value"] for d in DIMS if sub[pid][d]["value"] is not None}
    coverage[pid] = len(values)
    partial[pid] = round(geo(values), 3) if len(values) >= 2 else None
    score[pid] = round(geo(values), 3) if len(values) == len(DIMS) else None

# ---------------------------------------------------------------------------
# Monte-Carlo uncertainty, only where a full six-dimension score exists.
# ---------------------------------------------------------------------------
MC = CFG["monte_carlo"]
rng = np.random.default_rng(MC["seed"])
ci = {}
for pid in pids:
    if score[pid] is None:
        ci[pid] = None
        continue
    samples = np.empty(int(MC["n_samples"]))
    for i in range(int(MC["n_samples"])):
        draw = {d: float(np.clip(rng.normal(sub[pid][d]["value"], MC["sigma"]),
                                 1e-3, 1.0)) for d in DIMS}
        samples[i] = geo(draw)
    ci[pid] = {
        "median": round(float(np.median(samples)), 3),
        "lo": round(float(np.percentile(samples, 2.5)), 3),
        "hi": round(float(np.percentile(samples, 97.5)), 3),
    }

ranked = sorted([p for p in pids if score[p] is not None],
                key=lambda p: score[p], reverse=True)

# ---------------------------------------------------------------------------
# Publication gate
# ---------------------------------------------------------------------------
total_cells = fill_cells = 0
for name, census in fill_census.items():
    header = validator.SCHEMA[name]["header"]
    total_cells += row_counts.get(name, 0) * len(header)
    fill_cells += sum(census.values())
fill_fraction = (fill_cells / total_cells) if total_cells else 1.0

claim_rows = len(campaign_claims)
scorable_claims = sum(1 for c in campaign_claims if as_float(c.get("verification_score")) is not None)
corroborated_claims = sum(
    1 for c in campaign_claims
    if len({t for t in validator.split_ids(c.get("source_ids"))
            if validator.SOURCE_ID_RE.match(t)}) >= 2)
scorable_share = (scorable_claims / claim_rows) if claim_rows else 0.0
corroborated_share = (corroborated_claims / claim_rows) if claim_rows else 0.0

gates = [
    ("No structural errors in data/", sev_count[validator.ERROR] == 0,
     f"{sev_count[validator.ERROR]} error(s)"),
    (f"FILL cells below {GATE['max_fill_fraction']:.0%}",
     fill_fraction <= GATE["max_fill_fraction"],
     f"{fill_fraction:.1%} of cells ({fill_cells}/{total_cells})"),
    (f"At least {GATE['min_scorable_claim_share']:.0%} of claims scored",
     scorable_share >= GATE["min_scorable_claim_share"],
     f"{scorable_share:.1%} ({scorable_claims}/{claim_rows})"),
    (f"At least {GATE['min_corroborated_claim_share']:.0%} of claims corroborated",
     corroborated_share >= GATE["min_corroborated_claim_share"],
     f"{corroborated_share:.1%} ({corroborated_claims}/{claim_rows})"),
]
if GATE["require_all_subindices"]:
    gates.append(("At least one party fully scored on all six dimensions",
                  len(ranked) >= 1, f"{len(ranked)} of {len(pids)} parties"))
publishable = all(ok for _label, ok, _detail in gates)

# ---------------------------------------------------------------------------
# Charts. Each returns a record; a chart with no data is skipped and the
# reason is printed in the report instead of silently vanishing.
# ---------------------------------------------------------------------------
plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "font.size": 11,
    "axes.edgecolor": "#555555",
    "axes.linewidth": 0.8,
    "axes.grid": True,
    "grid.color": "#dddddd",
    "grid.linewidth": 0.6,
    "figure.dpi": 150,
})


def save(fig, name):
    path = CHARTS / f"{name}.svg"
    fig.savefig(path, bbox_inches="tight", pad_inches=0.15)
    plt.close(fig)
    return path


def chart(title, fn):
    """Run a chart builder, capturing skips and failures."""
    try:
        result = fn()
    except Exception as exc:  # keep one bad chart from killing the report
        return {"name": title, "path": None, "note": f"chart failed: {exc!r}"}
    if result is None:
        return {"name": title, "path": None,
                "note": "not enough data to draw this chart"}
    return {"name": title, "path": result, "note": None}


def year_rows(year):
    return [r for r in elections
            if as_int(r.get("election_year")) == year and r["party_id"] != "P009"]


def chart_vote_seat():
    rows = year_rows(BASELINE_YEAR)
    if not rows:
        return None
    rows.sort(key=lambda r: -(as_int(r.get("seats")) or 0))
    names = [pname[r["party_id"]] for r in rows]
    vs = [as_float(r.get("vote_share_est")) or 0.0 for r in rows]
    ss = [(as_int(r.get("seats")) or 0) / HOUSE_SEATS * 100 for r in rows]
    colors = [pcolor[r["party_id"]] for r in rows]
    y = list(range(len(names)))[::-1]
    fig, ax = plt.subplots(figsize=(9, 6))
    for i in range(len(names)):
        ax.plot([vs[i], ss[i]], [y[i], y[i]], color="#bbbbbb", lw=2, zorder=1)
    ax.scatter(vs, y, s=110, c=colors, marker="o", edgecolors="white",
               linewidths=1.2, zorder=3, label="Vote share (%)")
    ax.scatter(ss, y, s=130, c=colors, marker="D", edgecolors="white",
               linewidths=1.2, zorder=3, label="Seat share (%)")
    ax.set_yticks(y)
    ax.set_yticklabels(names)
    ax.set_xlabel("Share of valid votes / seats (%)")
    ax.set_title(f"{BASELINE_YEAR}: how votes translated into seats (gap = distortion)")
    ax.legend(loc="lower right", frameon=False)
    ax.set_xlim(0, max(max(vs), max(ss)) * 1.12)
    return save(fig, "vote_seat_dumbbell")


def chart_advantage():
    if not Aratio:
        return None
    items = sorted(((Aratio[p], p) for p in pids if p in Aratio), reverse=True)
    names = [pname[p] for _, p in items]
    vals = [v for v, _ in items]
    colors = [pcolor[p] for _, p in items]
    fig, ax = plt.subplots(figsize=(9, 5))
    bars = ax.bar(names, vals, color=colors, alpha=0.9)
    ax.axhline(1.0, color="#111111", lw=1.2, ls="--")
    for b, v in zip(bars, vals):
        ax.text(b.get_x() + b.get_width() / 2, v + 0.02, f"{v:.2f}",
                ha="center", va="bottom", fontsize=9)
    ax.set_ylabel("Advantage ratio  (seat share / vote share)")
    ax.set_title(f"{BASELINE_YEAR}: over- and under-representation "
                 f"(1.0 = perfectly proportional)")
    ax.set_ylim(0, max(vals) * 1.18)
    return save(fig, "advantage_ratio")


def chart_composition():
    rows = year_rows(BASELINE_YEAR)
    if not rows:
        return None
    rows.sort(key=lambda r: -(as_int(r.get("seats")) or 0))
    names = [pname[r["party_id"]] for r in rows]
    votes = [as_float(r.get("vote_share_est")) or 0.0 for r in rows]
    seats = [as_int(r.get("seats")) or 0 for r in rows]
    colors = [pcolor[r["party_id"]] for r in rows]
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 4.6), sharex=True)
    left = 0.0
    for n, v, c in zip(names, votes, colors):
        ax1.barh(0, v, left=left, color=c, edgecolor="white", height=0.55)
        if v >= 4.5:
            ax1.text(left + v / 2, 0, f"{n}\n{v:.1f}%", ha="center", va="center",
                     fontsize=7.5, color="white")
        left += v
    ax1.set_xlim(0, 100)
    ax1.set_yticks([])
    ax1.set_title("Vote share (%, estimates - verify against official results)",
                  fontsize=10)
    left = 0.0
    for n, s, c in zip(names, seats, colors):
        ax2.barh(0, s, left=left, color=c, edgecolor="white", height=0.55)
        if s >= 15:
            ax2.text(left + s / 2, 0, f"{n}\n{s}", ha="center", va="center",
                     fontsize=7.5, color="white")
        left += s
    ax2.set_xlim(0, HOUSE_SEATS)
    ax2.set_yticks([])
    ax2.set_title(f"Seats ({HOUSE_SEATS} total)", fontsize=10)
    ax2.set_xlabel("Seats")
    fig.suptitle(f"{BASELINE_YEAR}: from votes to seats - same colour, different slice",
                 y=1.04)
    fig.tight_layout()
    return save(fig, "seat_composition")


def chart_trajectory():
    years = [int(y) for y in CFG.get("trajectory_years") or []]
    if len(years) < 2:
        return None
    main = [p for p in pids if any(r["party_id"] == p for r in elections)]
    fig, ax = plt.subplots(figsize=(9, 6))
    drawn = False
    for pid in main:
        ys = []
        for yr in years:
            r = next((r for r in elections
                      if r["party_id"] == pid and as_int(r.get("election_year")) == yr),
                     None)
            ys.append((as_int(r.get("seats")) or 0) / HOUSE_SEATS * 100 if r else None)
        if any(v is not None for v in ys):
            drawn = True
            ax.plot(years, ys, marker="o", lw=2.2, color=pcolor[pid], label=pname[pid])
    if not drawn:
        plt.close(fig)
        return None
    ax.set_xticks(years)
    ax.set_ylabel("Seat share (%)")
    ax.set_title(f"Seat-share trajectory, {min(years)}-{max(years)}")
    ax.legend(frameon=False, ncol=2)
    ax.set_ylim(0, 35)
    return save(fig, "trajectory")


def chart_heatmap():
    domains = sorted({r.get("domain", "") for r in promises if r.get("domain")})
    if not domains:
        return None
    active = [pid for pid in pids if any(r["party_id"] == pid for r in promises)]
    if not active:
        return None
    numeric = CFG["promise_status_numeric"]
    mat = np.zeros((len(active), len(domains)))
    text = [["" for _ in domains] for _ in active]
    for i, pid in enumerate(active):
        for j, dom in enumerate(domains):
            rows = [r for r in promises
                    if r["party_id"] == pid and r.get("domain") == dom]
            if rows:
                mat[i, j] = min(numeric.get(r.get("status", ""), 0.5) for r in rows)
                text[i][j] = rows[0].get("status", "")[:8].replace("_", " ")
    fig, ax = plt.subplots(figsize=(10, 4.5))
    im = ax.imshow(mat, cmap=plt.cm.RdYlGn, vmin=0, vmax=1, aspect="auto")
    ax.set_xticks(range(len(domains)))
    ax.set_xticklabels(domains, rotation=35, ha="right")
    ax.set_yticks(range(len(active)))
    ax.set_yticklabels([pname[p] for p in active])
    for i in range(len(active)):
        for j in range(len(domains)):
            if text[i][j]:
                ax.text(j, i, text[i][j], ha="center", va="center",
                        fontsize=7.5, color="#111111")
    ax.set_title("Promise ledger: status by party and domain "
                 "(green = delivered, red = failed)")
    fig.colorbar(im, ax=ax, fraction=0.03, label="0 = failed ... 1 = fulfilled")
    return save(fig, "promise_ledger_heatmap")


def chart_bias_reliability():
    points = []
    for s in sources:
        r = as_float(s.get("reliability"))
        lean = as_float(s.get("bias_lean"))
        if r is None or lean is None:
            continue
        points.append((r, lean, s["source_id"]))
    if not points:
        return None
    fig, ax = plt.subplots(figsize=(9, 6))
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    ax.scatter(xs, ys, s=90, c="#b32424", alpha=0.85, zorder=3)
    for x, y, sid in points:
        ax.annotate(sid, (x, y), textcoords="offset points", xytext=(6, 6),
                    fontsize=9, color="#333333")
    ax.set_xlabel("Reliability (1 = weak ... 5 = official)")
    ax.set_ylabel("Political lean (-2 left ... +2 right)")
    ax.set_title("Source map: reliability vs. lean (kept separate)")
    ax.set_xlim(2.5, 5.5)
    ax.set_ylim(-2.6, 2.6)
    ax.axhline(0, color="#bbbbbb", lw=1)
    ax.axvline(3, color="#bbbbbb", lw=1)
    return save(fig, "bias_reliability")


def chart_readiness():
    """Which CEAGI dimensions actually exist for each party. The point of the
    report right now is this picture, not a ranking."""
    labels = [f"{DIM_LABEL[d]} ({d})" for d in DIMS]
    mat = np.array([[1.0 if sub[p][d]["value"] is not None else 0.0 for d in DIMS]
                    for p in pids])
    fig, ax = plt.subplots(figsize=(8.5, 5))
    ax.imshow(mat, cmap=plt.cm.RdYlGn, vmin=0, vmax=1, aspect="auto")
    ax.set_xticks(range(len(DIMS)))
    ax.set_xticklabels(labels, rotation=30, ha="right")
    ax.set_yticks(range(len(pids)))
    ax.set_yticklabels([pname[p] for p in pids])
    for i, p in enumerate(pids):
        for j, d in enumerate(DIMS):
            ok = sub[p][d]["value"] is not None
            ax.text(j, i, "ok" if ok else "missing", ha="center", va="center",
                    fontsize=8, color="#111111" if ok else "#7a1010")
    ax.set_title(f"Evidence readiness per party: {CAMPAIGN_YEAR} campaign, "
                 f"{BASELINE_YEAR} baseline")
    return save(fig, "subindex_readiness")


def chart_ceagi():
    if not ranked:
        return None
    lo = [ci[p]["lo"] for p in ranked]
    hi = [ci[p]["hi"] for p in ranked]
    vals = [score[p] for p in ranked]
    fig, ax = plt.subplots(figsize=(9, 5.5))
    ax.bar([pname[p] for p in ranked], vals,
           yerr=[[max(0.0, vals[i] - lo[i]) for i in range(len(vals))],
                 [max(0.0, hi[i] - vals[i]) for i in range(len(vals))]],
           capsize=4, color=[pcolor[p] for p in ranked], alpha=0.9,
           error_kw=dict(ecolor="#333333", lw=1))
    for i, v in enumerate(vals):
        ax.text(i, v + 0.01, f"{v:.3f}", ha="center", va="bottom", fontsize=9)
    ax.set_ylabel("CEAGI score (0-1)")
    ax.set_title("Party accountability score (CEAGI) with 95% credible interval")
    ax.set_ylim(0, max(hi) * 1.15)
    return save(fig, "ceagi_ranking")


CHART_SPECS = [
    ("Votes to seats", chart_vote_seat),
    ("Advantage ratio", chart_advantage),
    ("Seat composition", chart_composition),
    ("Seat-share trajectory", chart_trajectory),
    ("Evidence readiness", chart_readiness),
    ("CEAGI ranking", chart_ceagi),
    ("Promise ledger", chart_heatmap),
    ("Source map: reliability vs. lean", chart_bias_reliability),
]

# Drop SVGs from a previous run so a chart that is now skipped cannot linger in
# output/charts and be mistaken for a current one.
for stale in CHARTS.glob("*.svg"):
    stale.unlink()

charts = [chart(title, fn) for title, fn in CHART_SPECS]
drawn = [c for c in charts if c["path"]]
skipped = [c for c in charts if not c["path"]]


# ---------------------------------------------------------------------------
# HTML rendering helpers
# ---------------------------------------------------------------------------
def inline_svg(path):
    txt = path.read_text(encoding="utf-8")
    txt = re.sub(r"<\?xml.*?\?>", "", txt)
    txt = re.sub(r"<!DOCTYPE.*?>", "", txt, flags=re.S)
    return txt


def cell(value):
    if is_missing(value):
        return '<span class="miss">FILL</span>'
    return esc(value)


def num_cell(value, fmt="{:.3f}"):
    if value is None:
        return '<span class="na">n/a</span>'
    return fmt.format(value)


def table(headers, rows, cls=""):
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>"
                   for r in rows)
    return (f'<table class="{cls}"><thead><tr>{head}</tr></thead>'
            f'<tbody>{body}</tbody></table>')


def status_badge(status):
    color = STATUS_COLOR.get(status, "#888888")
    return f'<span class="badge" style="background:{color}">{esc(status)}</span>'


# ---------------------------------------------------------------------------
# Report content
# ---------------------------------------------------------------------------
def days_to_election():
    if not CFG.get("election_date"):
        return None
    try:
        target = dt.date.fromisoformat(str(CFG["election_date"]))
    except ValueError:
        return None
    return (target - dt.date.today()).days


days = days_to_election()
if days is None:
    countdown = f"campaign year {CAMPAIGN_YEAR}"
elif days > 0:
    countdown = f"{days} day{'s' if days != 1 else ''} to polling day"
elif days == 0:
    countdown = "polling day"
else:
    countdown = f"polling day passed {-days} day{'s' if days != -1 else ''} ago"

gate_rows = [
    [esc(label), '<span class="ok">PASS</span>' if ok else '<span class="bad">FAIL</span>',
     esc(detail)]
    for label, ok, detail in gates
]

# Auto-generated to-do list, derived from the validator findings.
actions = []
for name, census in fill_census.items():
    for col, n in sorted(census.items(), key=lambda kv: -kv[1]):
        total = row_counts.get(name, 0)
        if n >= total and total:
            actions.append((n, f"Fill <code>{esc(col)}</code> in "
                               f"<code>data/{esc(name)}</code> - all {total} rows are FILL"))
        elif n:
            actions.append((n, f"Fill <code>{esc(col)}</code> in "
                               f"<code>data/{esc(name)}</code> - {n}/{total} rows"))
unsourced = sum(1 for f in findings if f.code == "UNSOURCED")
if unsourced:
    actions.append((unsourced, f"Re-source {unsourced} rows currently marked "
                               f"<code>UNSOURCED</code>"))
single = sum(1 for f in findings if f.code == "SINGLE_SOURCE")
if single:
    actions.append((single, f"Corroborate {single} claims with a second "
                            f"independent source (project rule: 2+ required)"))
merged = sum(1 for f in findings if f.code == "MERGED_CLAIM")
if merged:
    actions.append((merged, f"Split {merged} claims that bundle several numeric "
                            f"targets into one row"))
for f in findings:
    if f.code in ("NO_ELECTION_ROW", "NO_INDICATOR_ROW"):
        actions.append((claim_rows, esc(f.message)))
actions.sort(key=lambda a: -a[0])
actions_html = "".join(f"<li>{txt}</li>" for _weight, txt in actions[:12])

# Honest findings: state what the evidence supports, then what it does not.
supported, unsupported = [], []
if Aratio:
    over = max(Aratio, key=lambda p: Aratio[p])
    under = min(Aratio, key=lambda p: Aratio[p])
    supported.append(
        f"In {BASELINE_YEAR}, <b>{esc(pname[over])}</b> was the most over-represented "
        f"party (advantage ratio {Aratio[over]:.2f}) and <b>{esc(pname[under])}</b> the "
        f"most under-represented ({Aratio[under]:.2f}) - the PR formula did not "
        f"convert votes into seats evenly.")
if campaign_claims:
    doms = {}
    for c in campaign_claims:
        doms[c.get("domain", "")] = doms.get(c.get("domain", ""), 0) + 1
    top = sorted(doms.items(), key=lambda kv: -kv[1])[:3]
    supported.append(
        f"{claim_rows} campaign claims are registered for {CAMPAIGN_YEAR}, "
        f"{scorable_claims} of them scored and {corroborated_claims} corroborated. "
        f"The most crowded domains are "
        f"{', '.join(f'{esc(d)} ({n})' for d, n in top)}.")
if historical_claims:
    supported.append(
        f"{len(historical_claims)} claims from the {', '.join(sorted({c.get('election_year') for c in historical_claims}))} "
        f"campaign(s) are recorded in the historical claims bank (not scored in "
        f"the CEAGI model).")
if not ranked:
    unsupported.append(
        "No party can be ranked on CEAGI: claim credibility C is missing for every "
        "party because no campaign claim carries a verification score yet.")
if any(sub[p]["D"]["value"] is None for p in pids):
    d_missing = [pname[p] for p in pids if sub[p]["D"]["value"] is None]
    shown = ", ".join(d_missing[:6]) + (" ..." if len(d_missing) > 6 else "")
    unsupported.append(
        f"Delivery D is unavailable for {len(d_missing)} of {len(pids)} parties "
        f"({shown}). Scoring requires at least {CFG['min_promises_for_D']} "
        f"concluded promises per party; the outgoing term's promises are still "
        f"open, so its record cannot be judged yet.")
if not all(sub[p]["E"]["value"] is not None for p in pids):
    unsupported.append(
        f"Electoral efficiency E is missing for some parties: there is no "
        f"{BASELINE_YEAR} election row for them.")
else:
    unsupported.append(
        f"E measures the {BASELINE_YEAR} election, not {CAMPAIGN_YEAR}: how the "
        f"new {HOUSE_SEATS}-seat arithmetic will treat each party's vote geography "
        f"cannot be known before the count.")
if not corroborated_claims:
    unsupported.append(
        "No claim is corroborated. Every one rests on a single press report, so "
        "none of them should be printed as established fact.")
findings_html = "".join(f"<li>{t}</li>" for t in supported)
unfindings_html = "".join(f"<li>{t}</li>" for t in unsupported)

findings_table = []
order = {validator.ERROR: 0, validator.WARN: 1, validator.INFO: 2}
grouped = {}
for f in findings:
    grouped.setdefault(f.code, []).append(f)
for code, items in sorted(grouped.items(),
                          key=lambda kv: (order[kv[1][0].severity], -len(kv[1]))):
    findings_table.append([
        f'<span class="sev-{items[0].severity.lower()}">{items[0].severity}</span>',
        esc(code), str(len(items)), esc(items[0].message),
    ])

ceagi_rows = []
if ranked:
    for i, pid in enumerate(ranked, 1):
        ceagi_rows.append([
            str(i), esc(pname[pid]), f"{score[pid]:.3f}",
            f"{ci[pid]['lo']:.3f}-{ci[pid]['hi']:.3f}",
            *[f"{sub[pid][d]['value']:.2f}" for d in DIMS],
        ])
sub_rows = []
for pid in sorted(pids, key=lambda p: (-(coverage[p]), pname[p])):
    sub_rows.append([
        esc(pname[pid]),
        f"{coverage[pid]}/{len(DIMS)}",
        num_cell(partial[pid]),
        num_cell(score[pid]),
        *[num_cell(sub[pid][d]["value"], "{:.2f}") for d in DIMS],
    ])

claim_rows_html = []
for c in campaign_claims:
    claim_rows_html.append([
        esc(c.get("claim_id")), esc(pname.get(c.get("party_id"), c.get("party_id"))),
        cell(c.get("class")), cell(c.get("verification_score")),
        f'<span class="claimtext">{cell(c.get("claim"))}</span>',
        esc(c.get("domain")), cell(c.get("baseline")), cell(c.get("target")),
        cell(c.get("deadline")), cell(c.get("unit")), cell(c.get("source_ids")),
        cell(c.get("confidence")),
    ])

historical_rows_html = []
for c in historical_claims:
    historical_rows_html.append([
        esc(c.get("election_year")), esc(c.get("claim_id")),
        esc(pname.get(c.get("party_id"), c.get("party_id"))),
        cell(c.get("class")), cell(c.get("verification_score")),
        f'<span class="claimtext">{cell(c.get("claim"))}</span>',
        esc(c.get("domain")), cell(c.get("baseline")), cell(c.get("target")),
        cell(c.get("deadline")), cell(c.get("unit")), cell(c.get("source_ids")),
        cell(c.get("confidence")),
    ])

promise_rows = []
for r in promises:
    promise_rows.append([
        esc(r.get("promise_id")), esc(pname.get(r.get("party_id"), r.get("party_id"))),
        f"{esc(r.get('term_start'))}-{esc(r.get('term_end'))}",
        esc(r.get("promise")), esc(r.get("domain")),
        status_badge(r.get("status", "")),
        cell(r.get("outcome_metric")), cell(r.get("source_ids")),
        cell(r.get("confidence")),
    ])

leader_rows = []
for r in leaders:
    leader_rows.append([
        esc(r.get("leader")), esc(pname.get(r.get("party_id"), r.get("party_id"))),
        esc(r.get("role")), cell(r.get("achievements")), cell(r.get("ownership")),
        cell(r.get("ostensible_motive")), cell(r.get("conflicts")),
        cell(r.get("skin_in_game")), cell(r.get("source_ids")),
    ])

source_rows = []
for r in sources:
    source_rows.append([
        esc(r.get("source_id")), esc(r.get("title")), esc(r.get("author_org")),
        cell(r.get("date")), esc(r.get("type")), esc(r.get("primary_secondary")),
        cell(r.get("reliability")), cell(r.get("bias_label")), cell(r.get("status")),
    ])

timeline_rows = sorted(timeline, key=lambda r: r.get("date", ""))
timeline_html = "".join(
    f'<div class="tl"><span class="tldate">{cell(r.get("date"))}</span>'
    f'<span class="tlev">{esc(r.get("event"))}</span>'
    f'<span class="tlactor">{esc(r.get("actor"))}</span>'
    f'<span class="tlsrc">{cell(r.get("source_ids"))}</span></div>'
    for r in timeline_rows
)

chart_blocks = ""
for c in drawn:
    chart_blocks += (
        f'<figure class="chart">{inline_svg(c["path"])}'
        f'<figcaption>{esc(c["name"])}</figcaption></figure>')
if skipped:
    chart_blocks += '<p class="small">Charts not produced:</p><ul class="small">'
    chart_blocks += "".join(
        f'<li><b>{esc(c["name"])}</b> - {esc(c["note"])}</li>' for c in skipped)
    chart_blocks += "</ul>"

banner_class = "banner ok-banner" if publishable else "banner bad-banner"
banner_text = ("PUBLISHABLE - the data passes every gate below."
               if publishable else
               "NOT PUBLISHABLE - this report must not be printed as-is. "
               "The gates below say exactly what is missing.")

CSS = """
:root { --red:#b32424; --ink:#1a1a1a; --muted:#666; --line:#e3e0dc;
        --bg:#fbfaf8; --green:#2e7d32; }
* { box-sizing:border-box; }
body { margin:0; font-family:Georgia,'Times New Roman',serif; color:var(--ink);
       background:var(--bg); line-height:1.55; }
.wrap { max-width:1180px; margin:0 auto; padding:0 22px 60px; }
header { border-bottom:4px solid var(--red); padding:34px 0 20px; margin-bottom:26px; }
.kicker { font-family:Helvetica,Arial,sans-serif; text-transform:uppercase;
          letter-spacing:.14em; color:var(--red); font-size:.8rem; font-weight:700; }
h1 { font-size:2.4rem; margin:.25rem 0 .35rem; line-height:1.1; }
h2 { font-family:Helvetica,Arial,sans-serif; font-size:1.25rem;
     border-bottom:2px solid var(--line); padding-bottom:6px; margin:44px 0 16px; }
h3 { font-family:Helvetica,Arial,sans-serif; font-size:1rem; margin:26px 0 10px; }
.deck { font-size:1.12rem; color:#333; max-width:780px; }
.meta { font-family:Helvetica,Arial,sans-serif; font-size:.8rem; color:var(--muted);
        margin-top:10px; }
.banner { padding:14px 18px; margin:22px 0; font-family:Helvetica,Arial,sans-serif;
          font-size:.88rem; border-left:6px solid var(--red); }
.bad-banner { background:#fdf3f3; }
.ok-banner { background:#f2f8f2; border-left-color:var(--green); }
.banner b { color:var(--red); }
.ok-banner b { color:var(--green); }
ul.findings { font-size:1.02rem; padding-left:20px; }
ul.findings li { margin-bottom:8px; }
table { border-collapse:collapse; width:100%; margin:14px 0;
        font-family:Helvetica,Arial,sans-serif; font-size:.8rem; background:#fff; }
th { background:#f1eee8; text-align:left; padding:8px 9px;
     border-bottom:2px solid var(--ink); font-size:.72rem; text-transform:uppercase;
     letter-spacing:.04em; position:sticky; top:0; }
td { padding:7px 9px; border-bottom:1px solid var(--line); vertical-align:top; }
tr:hover td { background:#faf6f0; }
.badge { color:#fff; padding:2px 8px; border-radius:3px; font-size:.72rem;
         font-weight:600; white-space:nowrap; }
.miss { color:#b32424; background:#fdf3f3; border:1px solid #f0c9c9;
        padding:1px 6px; border-radius:3px; font-size:.72rem; font-weight:600; }
.na { color:#8a8a8a; font-style:italic; }
.ok { color:var(--green); font-weight:700; }
.bad { color:var(--red); font-weight:700; }
.sev-error { color:var(--red); font-weight:700; }
.sev-warn { color:#a86a00; font-weight:700; }
.sev-info { color:#1a6a8a; }
.claimtext { display:block; max-width:520px; }
figure.chart { margin:28px 0; text-align:center; }
figure.chart svg { max-width:100%; height:auto; }
figcaption { font-family:Helvetica,Arial,sans-serif; font-size:.78rem;
             color:var(--muted); margin-top:6px; }
.tl { display:grid; grid-template-columns:110px 1fr 150px 90px; gap:10px;
      padding:7px 0; border-bottom:1px solid var(--line);
      font-family:Helvetica,Arial,sans-serif; font-size:.85rem; }
.tldate { font-weight:700; color:var(--red); }
.tlactor, .tlsrc { color:var(--muted); }
.formula { font-family:Georgia,serif; font-style:italic; background:#fff;
           border:1px solid var(--line); padding:10px 14px; margin:10px 0;
           overflow-x:auto; }
.small { font-family:Helvetica,Arial,sans-serif; font-size:.78rem; color:var(--muted); }
.scroll { overflow-x:auto; }
footer { margin-top:50px; padding-top:16px; border-top:3px solid var(--ink);
         font-family:Helvetica,Arial,sans-serif; font-size:.78rem; color:var(--muted); }
@media (max-width:820px) { h1 { font-size:1.8rem; } .tl { grid-template-columns:1fr; } }
"""

html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Morocco {CAMPAIGN_YEAR} - Electoral Claims, Promises &amp; Accountability</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">

<header>
  <div class="kicker">Data project &middot; Morocco &middot; {esc(countdown)}</div>
  <h1>Electoral Claims, Promises &amp; Accountability</h1>
  <p class="deck">What the parties are promising for {CAMPAIGN_YEAR}, how votes
  became seats in {BASELINE_YEAR}, and which numbers we can actually stand behind.</p>
  <div class="meta">Generated from the <code>data/</code> registry by
  <code>scripts/build.py</code> &middot; model CEAGI &middot;
  {len(sources)} sources, {claim_rows} campaign claims
  ({len(historical_claims)} historical), {len(promises)} promises,
  {len(pids)} parties &middot; config: campaign {CAMPAIGN_YEAR} / baseline {BASELINE_YEAR}.</div>
  <div class="meta" style="margin-top:6px">&#9673; <a href="seat_simulator.html">Open the interactive seat-allocation simulator</a>
  (Hare quota &#8594; hemicycle, with a CEAGI reference).</div>
</header>

<div class="{banner_class}">
  <b>{esc(banner_text)}</b>
  <div style="margin-top:8px">
    {fill_fraction:.1%} of data cells are still <code>FILL</code>
    ({fill_cells}/{total_cells}) &middot; {scorable_claims}/{claim_rows} claims scored
    &middot; {corroborated_claims}/{claim_rows} corroborated
    &middot; {len(ranked)}/{len(pids)} parties fully scored.
    {sev_count[validator.ERROR]} structural error(s),
    {sev_count[validator.WARN]} warning(s).
  </div>
</div>

<section>
  <h2>1. Publication gate</h2>
  <p class="small">Every gate must pass before this report is used editorially.
  Run <code>python3 scripts/validate.py</code> for the full finding list.</p>
  {table(["Gate", "Status", "Measured"], gate_rows)}
</section>

<section>
  <h2>2. What the data supports, and what it does not</h2>
  <h3>Supportable today</h3>
  <ul class="findings">{findings_html or '<li class="small">Nothing yet.</li>'}</ul>
  <h3>Not supportable yet</h3>
  <ul class="findings">{unfindings_html or '<li class="small">No blocked statements.</li>'}</ul>
</section>

<section>
  <h2>3. To do before publishing</h2>
  <p class="small">Generated automatically, ordered by how many rows each gap blocks.</p>
  <ol class="findings">{actions_html or '<li class="small">Nothing outstanding.</li>'}</ol>
</section>

<section>
  <h2>4. CEAGI - party accountability score</h2>
  <p>CEAGI = Composite Electoral Accountability &amp; Governance Index. Six
  sub-indices - Delivery <b>D</b>, Claim credibility <b>C</b>, Electoral
  efficiency <b>E</b>, Governance <b>G</b>, Leadership <b>L</b>, Mandate
  coherence <b>M</b> - combined with a weighted geometric mean, so a party
  cannot compensate a total failure in one dimension with a strong showing in
  another. A dimension with no evidence is <span class="na">n/a</span>; it is
  never imputed. A full score is published only when all six exist.</p>
  <div class="formula">S<sub>p</sub> = ( D<sup>0.25</sup> &middot; C<sup>0.20</sup>
  &middot; E<sup>0.10</sup> &middot; G<sup>0.20</sup> &middot; L<sup>0.15</sup>
  &middot; M<sup>0.10</sup> )</div>
  <h3>Sub-index coverage and provisional values</h3>
  <p class="small">The "provisional" column averages only the dimensions that
  exist. It is <b>not comparable between parties</b> with different coverage and
  is shown for diagnosis only - it is never ranked.</p>
  <div class="scroll">
  {table(["Party", "Coverage", "Provisional (not ranked)", "CEAGI (complete only)"]
         + [f"{d} - {DIM_LABEL[d]}" for d in DIMS], sub_rows)}
  </div>
  <h3>CEAGI ranking - complete scores only</h3>
  {table(["Rank", "Party", "Score", "95% CI"] + DIMS, ceagi_rows)
   if ceagi_rows else '<p class="small">No party has all six dimensions yet, so no ranking is published. This is deliberate: ranking on partial coverage would compare unlike things.</p>'}
</section>

<section>
  <h2>5. Charts</h2>
  {chart_blocks}
</section>

<section>
  <h2>6. {CAMPAIGN_YEAR} campaign claims</h2>
  <p class="small">{len(campaign_claims)} claims from {len({c.get('party_id') for c in campaign_claims})}
  parties. <span class="miss">FILL</span> marks a cell the registry does not have
  yet - it is shown rather than hidden so the hole is visible.</p>
  <div class="scroll">
  {table(["ID", "Party", "Class", "Verif.", "Claim", "Domain", "Baseline",
          "Target", "Deadline", "Unit", "Sources", "Conf."], claim_rows_html)}
  </div>
</section>

<section>
  <h2>7. Historical campaign claims ({', '.join(sorted({c.get('election_year') for c in historical_claims}))})</h2>
  <p class="small">{len(historical_claims)} claims from the earlier campaign(s), kept
  as a traceable record of what each party promised. They are not scored in the
  current CEAGI run.</p>
  <div class="scroll">
  {table(["Year", "ID", "Party", "Class", "Verif.", "Claim", "Domain",
          "Baseline", "Target", "Deadline", "Unit", "Sources", "Conf."],
          historical_rows_html)}
  </div>
</section>

<section>
  <h2>8. Promise ledger (past terms)</h2>
  {table(["ID", "Party", "Term", "Promise", "Domain", "Status", "Outcome",
          "Sources", "Conf."], promise_rows)}
</section>

<section>
  <h2>9. Leaders - achievements, ownership, ostensible motives</h2>
  <p class="small">"Skin in the game" = 0-1 judgement of how tightly the leader's
  personal fortune or career is tied to the promised outcomes (1 = fully exposed).
  Ownership and motive cells are claims requiring corroboration.</p>
  <div class="scroll">
  {table(["Leader", "Party", "Role", "Achievements", "Ownership",
          "Ostensible motive", "Conflicts", "Skin", "Sources"], leader_rows)}
  </div>
</section>

<section>
  <h2>10. Sources registry</h2>
  <div class="scroll">
  {table(["ID", "Title", "Author/Org", "Date", "Type", "Primary?", "Rel.",
          "Lean", "Status"], source_rows)}
  </div>
  <p class="small">Every source in the registry is currently a
  <b>secondary</b> press report. The methodology requires primary documents
  (manifestos, laws, official datasets) and an archived URL per source.</p>
</section>

<section>
  <h2>11. Timeline</h2>
  {timeline_html}
</section>

<section>
  <h2>12. Validation findings</h2>
  <p class="small">Produced by <code>scripts/validate.py</code>. ERROR breaks an
  invariant, WARN is incomplete but sound, INFO is coverage.</p>
  <div class="scroll">
  {table(["Sev.", "Code", "Count", "Example"], findings_table)}
  </div>
</section>

<section>
  <h2>13. Methodology (short)</h2>
  <p class="small">Sources to facts/claims to synthesis. A claim is
  "corroborated" only with 2+ independent reliable sources. Delivery
  D = (fulfilled + 0.5&middot;partial) / (fulfilled + partial + failed +
  abandoned), computed over completed terms only, and requires at least
  {CFG['min_promises_for_D']} concluded promises before it is scored at all.
  Credibility C = &Sigma;(weight&middot;rubric) / 5&Sigma;weight with class
  weights Q=1.0, V=0.4, S=0.1, N=0.2. Electoral efficiency
  E = 1 - |A-1| / max|A-1| where A = seat share / vote share for the
  {BASELINE_YEAR} election. Aggregation is a weighted geometric mean;
  uncertainty is {int(MC['n_samples']):,} Monte-Carlo draws at
  &sigma;={MC['sigma']}. Full derivation in <code>morocco-elections.org</code>;
  per-number provenance in <code>output/audit_trail.md</code>.</p>
</section>

<footer>
  <p>Reproducible data project. Regenerate with
  <code>make check &amp;&amp; make build</code>. Settings in
  <code>config.json</code>. Published scores require passing the gate in
  section 1; until then treat every value as provisional.</p>
</footer>

</div>
</body>
</html>
"""


def write_json(path, payload):
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                    encoding="utf-8")


write_json(OUT / "ceagi_scores.json", {
    "generated": dt.datetime.now().isoformat(timespec="seconds"),
    "config": {"campaign_year": CAMPAIGN_YEAR, "baseline_year": BASELINE_YEAR,
               "house_seats": HOUSE_SEATS, "weights": WEIGHTS,
               "monte_carlo": MC},
    "publishable": publishable,
    "gates": [{"gate": g, "pass": ok, "measured": d} for g, ok, d in gates],
    "readiness": {
        "fill_fraction": round(fill_fraction, 4),
        "cells_filled": fill_cells, "cells_total": total_cells,
        "claims_total": claim_rows, "claims_scored": scorable_claims,
        "claims_corroborated": corroborated_claims,
        "claims_historical": len(historical_claims),
        "parties_fully_scored": len(ranked), "parties_total": len(pids),
    },
    "parties": [
        {
            "party_id": p, "name": pname[p], "score": score[p],
            "ci": ci[p], "coverage": coverage[p],
            "provisional_score_unranked": partial[p],
            "subindices": {d: {"value": sub[p][d]["value"],
                               "n": sub[p][d]["n"],
                               "year": sub[p][d]["year"],
                               "detail": sub[p][d]["detail"]} for d in DIMS},
            "advantage_ratio": round(Aratio.get(p, 0.0), 3),
        }
        for p in sorted(pids, key=lambda p: (-(coverage[p]), pname[p]))
    ],
})

with open(OUT / "ceagi_scores.csv", "w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh)
    w.writerow(["rank", "party_id", "party", "score", "ci_lo", "ci_hi",
                "coverage", "provisional_score_not_ranked"]
               + [f"{d}_{DIM_LABEL[d].replace(' ', '_')}" for d in DIMS])
    for i, pid in enumerate(ranked, 1):
        w.writerow([i, pid, pname[pid], score[pid], ci[pid]["lo"], ci[pid]["hi"],
                    f"{coverage[pid]}/6", partial[pid]]
                   + [sub[pid][d]["value"] for d in DIMS])
    for pid in pids:
        if score[pid] is not None:
            continue
        w.writerow(["", pid, pname[pid], "", "", "", f"{coverage[pid]}/6",
                    partial[pid]] + [sub[pid][d]["value"] for d in DIMS])

(OUT / "report.html").write_text(html_doc, encoding="utf-8")

# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------
lines = [
    "# Audit trail - Morocco electoral data project",
    "",
    f"Generated {dt.datetime.now().isoformat(timespec='seconds')} by "
    f"`scripts/build.py`.",
    f"Campaign year **{CAMPAIGN_YEAR}**, baseline year **{BASELINE_YEAR}**, "
    f"publication gate: **{'PASS' if publishable else 'FAIL'}**.",
    "",
    "Every number below is traced to a source ID and a confidence level.",
    "A value of `n/a` means the evidence does not exist yet - it is not a zero.",
    "",
    "> **Legend** - HIGH: official/verified - MEDIUM: one solid source - "
    "LOW: estimate to be verified - `UNSOURCED`: knowingly unattributed, "
    "must not be published.",
    "",
    "## 1. Sub-index provenance",
    "",
    "| Party | Dim | Value | n | Year | Detail |",
    "|---|---|---|---|---|---|",
]
for pid in pids:
    for d in DIMS:
        s = sub[pid][d]
        lines.append(f"| {pname[pid]} | {d} | "
                     f"{s['value'] if s['value'] is not None else 'n/a'} | "
                     f"{s['n']} | {s['year']} | {s['detail']} |")

lines += ["", "## 2. Composite scores", "",
          "| Party | Coverage | Provisional (unranked) | CEAGI | 95% CI |",
          "|---|---|---|---|---|"]
for pid in sorted(pids, key=lambda p: (-(coverage[p]), pname[p])):
    ci_txt = f"{ci[pid]['lo']}-{ci[pid]['hi']}" if ci[pid] else "n/a"
    lines.append(f"| {pname[pid]} | {coverage[pid]}/6 | "
                 f"{partial[pid] if partial[pid] is not None else 'n/a'} | "
                 f"{score[pid] if score[pid] is not None else 'n/a'} | {ci_txt} |")

lines += ["", "## 3. Election results (seats and vote shares)", "",
          "| Year | Party | Seats | Seats conf. | Vote share est. | Vote conf. |",
          "|---|---|---|---|---|---|"]
for r in elections:
    lines.append(f"| {r.get('election_year')} | "
                 f"{pname.get(r.get('party_id'), r.get('party_id'))} | "
                 f"{r.get('seats')} | {r.get('seats_confidence')} | "
                 f"{r.get('vote_share_est')}% | {r.get('vote_confidence')} |")

lines += ["", f"## 4. Campaign claims ({CAMPAIGN_YEAR})", ""]
for c in campaign_claims:
    status = ("scored" if as_float(c.get("verification_score")) is not None
              else "NOT SCORED")
    lines.append(
        f"- `{c.get('claim_id')}` [{pname.get(c.get('party_id'), c.get('party_id'))}] "
        f"class={c.get('class') or 'FILL'}, verification={c.get('verification_score') or 'FILL'} "
        f"({status}) - sources: {c.get('source_ids') or 'none'} "
        f"- confidence: {c.get('confidence') or 'FILL'}")

lines += ["", "## 4b. Historical claims (earlier campaigns)", ""]
for c in historical_claims:
    status = ("scored" if as_float(c.get("verification_score")) is not None
              else "NOT SCORED")
    lines.append(
        f"- `{c.get('claim_id')}` [{c.get('election_year')}] "
        f"[{pname.get(c.get('party_id'), c.get('party_id'))}] "
        f"class={c.get('class') or 'FILL'}, verification={c.get('verification_score') or 'FILL'} "
        f"({status}) - sources: {c.get('source_ids') or 'none'} "
        f"- confidence: {c.get('confidence') or 'FILL'}")

lines += ["", "## 5. Promises", ""]
for r in promises:
    lines.append(
        f"- `{r.get('promise_id')}` "
        f"[{pname.get(r.get('party_id'), r.get('party_id'))}, "
        f"{r.get('term_start')}-{r.get('term_end')}] {r.get('promise')} - "
        f"**{r.get('status')}** - {r.get('outcome_metric') or 'no metric'} "
        f"(sources: {r.get('source_ids') or 'none'}, "
        f"{r.get('confidence') or 'FILL'})")

lines += ["", "## 6. Leaders", ""]
for r in leaders:
    lines.append(f"- {r.get('leader')} "
                 f"[{pname.get(r.get('party_id'), r.get('party_id'))}] "
                 f"role {r.get('role')} - skin-in-game "
                 f"{r.get('skin_in_game') or 'FILL'} - sources: "
                 f"{r.get('source_ids') or 'none'}")

lines += ["", "## 7. Sources", "",
          "| ID | Title | Org | Date | Type | Primary | Reliability | Lean | Status |",
          "|---|---|---|---|---|---|---|---|---|"]
for s in sources:
    lines.append(f"| {s.get('source_id')} | {s.get('title')} | {s.get('author_org')} | "
                 f"{s.get('date')} | {s.get('type')} | {s.get('primary_secondary')} | "
                 f"{s.get('reliability') or 'FILL'} | {s.get('bias_label') or 'FILL'} | "
                 f"{s.get('status') or 'FILL'} |")

lines += ["", "## 8. Validation findings", "",
          f"errors={sev_count[validator.ERROR]}, "
          f"warnings={sev_count[validator.WARN]}, info={sev_count[validator.INFO]}", ""]
for code, items in sorted(grouped.items(),
                          key=lambda kv: (order[kv[1][0].severity], -len(kv[1]))):
    lines.append(f"### {items[0].severity} {code} ({len(items)})")
    lines.append("")
    for f in items[:12]:
        lines.append(f"- {f.where}: {f.message}")
    if len(items) > 12:
        lines.append(f"- ... and {len(items) - 12} more")
    lines.append("")

(OUT / "audit_trail.md").write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Console summary
# ---------------------------------------------------------------------------
print(f"[ok] Campaign year {CAMPAIGN_YEAR} / baseline {BASELINE_YEAR}")
print(f"[ok] Report    -> {OUT / 'report.html'}")
print(f"[ok] Charts    -> {len(drawn)} drawn, {len(skipped)} skipped ({CHARTS})")
if skipped:
    for c in skipped:
        print(f"     - skipped: {c['name']} ({c['note']})")
print(f"[ok] Scores    -> ceagi_scores.csv / ceagi_scores.json")
print(f"[ok] Audit     -> audit_trail.md")
print(f"[gate] errors={sev_count[validator.ERROR]} "
      f"warnings={sev_count[validator.WARN]} "
      f"fill={fill_fraction:.1%} "
      f"claims_scored={scorable_claims}/{claim_rows} "
      f"corroborated={corroborated_claims}/{claim_rows} "
      f"fully_scored_parties={len(ranked)}/{len(pids)}")
print(f"[gate] {'PUBLISHABLE' if publishable else 'NOT PUBLISHABLE'}")
if ranked:
    print(f"[ok] Top party: {pname[ranked[0]]} ({score[ranked[0]]:.3f})")
else:
    print("[ok] No party has all six dimensions - no ranking published (by design)")
sys.exit(0)
