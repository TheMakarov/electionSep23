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
from urllib.parse import quote

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
    # The convergence section leads with one shared theme. An explicit choice
    # (config.json) beats a hard-coded favourite; fall back to the most shared
    # theme when the configured id is absent or not actually shared.
    "convergence": {"spotlight_theme": None},
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
    "fulfilled": GREEN, "partial": "#7fae6a", "in_progress": GOLD,
    "pending": "#dfc98a", "failed": RED, "abandoned": "#8a8378",
    "unverifiable": "#b3aa9b",
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


def esc_attr(value):
    """Like esc, but safe inside a double-quoted HTML attribute.

    Claim texts contain quoted phrases (e.g. ``"rentree scolaire"``); an
    unescaped quote would terminate the attribute early.
    """
    return esc(value).replace('"', "&quot;").replace("'", "&#39;")


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
themes = load_table("themes.csv")
claim_themes = load_table("claim_themes.csv")
legal_basis = load_table("legal_basis.csv")

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
# Claim convergence - who is promising the same thing.
#
# A party's programme is a bag of claims. Two parties "duplicate" each other
# when their claims land on the same policy theme. The claim -> theme mapping
# is an explicit editorial layer stored in data/claim_themes.csv, never a
# keyword guess hidden in this script: every mapping row carries a literal
# anchor copied from the claim text, so the finding can be audited back to the
# words the party actually used.
# ---------------------------------------------------------------------------
theme_meta = {t["theme_id"]: t for t in themes}
claim_by_id = {c["claim_id"]: c for c in claims}
ct_claim: dict = {}
for _r in claim_themes:
    ct_claim.setdefault(_r["claim_id"], []).append(_r["theme_id"])


def convergence(claim_set):
    """{theme_id: {party_id: [claim_id, ...]}} for a set of claim rows."""
    cells: dict = {}
    for c in claim_set:
        pid = c.get("party_id")
        for tid in ct_claim.get(c["claim_id"], []):
            cells.setdefault(tid, {}).setdefault(pid, []).append(c["claim_id"])
    return cells


campaign_cells = convergence(campaign_claims)
history_cells = convergence(historical_claims)

# Themes ordered by how many parties crowd onto them - the most duplicated
# theme sits at the top of the matrix, which is the whole point of the chart.
campaign_theme_order = sorted(
    campaign_cells,
    key=lambda t: (-len(campaign_cells[t]),
                   -sum(len(v) for v in campaign_cells[t].values()),
                   theme_meta[t]["label"]))
conv_parties = [p for p in pids if any(p in campaign_cells[t] for t in campaign_cells)]
if not conv_parties:
    conv_parties = list(pids)

shared_themes = [t for t in campaign_theme_order if len(campaign_cells[t]) >= 2]
exclusive_themes = [t for t in campaign_theme_order if len(campaign_cells[t]) == 1]
unused_themes = [t for t in theme_meta if t not in campaign_cells]

party_themes = {p: {t for t in campaign_cells if p in campaign_cells[t]}
                for p in conv_parties}
party_shared = {p: {t for t in party_themes[p] if len(campaign_cells[t]) >= 2}
                for p in conv_parties}
party_exclusive = {p: party_themes[p] - party_shared[p] for p in conv_parties}
# Themes a party also ran on in the previous campaign: recycled promises.
party_carried = {}
for p in conv_parties:
    previous = {t for t in history_cells if p in history_cells[t]}
    party_carried[p] = sorted(previous & party_themes[p])

conv_claims_total = sum(len(v) for t in campaign_cells for v in campaign_cells[t].values())
conv_shared_cells = sum(len(campaign_cells[t][p]) for t in shared_themes
                        for p in campaign_cells[t])
conv_duplication = (len(shared_themes) / len(campaign_theme_order)
                    if campaign_theme_order else 0.0)
conv_top_theme = campaign_theme_order[0] if campaign_theme_order else None
conv_top_parties = (sorted(campaign_cells[conv_top_theme],
                           key=lambda p: -len(campaign_cells[conv_top_theme][p]))
                    if conv_top_theme else [])
conv_most_derivative = max(
    (p for p in conv_parties if party_themes[p]),
    key=lambda p: (len(party_shared[p]) / len(party_themes[p]), len(party_shared[p])),
    default=None)
conv_most_distinctive = max(
    conv_parties,
    key=lambda p: (len(party_exclusive[p]), len(party_themes[p])),
    default=None)
# Best single "same theme, two parties" example to lead the section with. The
# preference is explicit config, not a hidden favourite; if it is unset, absent
# or not actually shared, fall back to the most crowded theme.
_spot_pref = (CFG.get("convergence") or {}).get("spotlight_theme")
conv_spotlight = (
    _spot_pref if _spot_pref in campaign_cells and len(campaign_cells[_spot_pref]) >= 2
    else (shared_themes[0] if shared_themes else None))


def theme_claims(tid, pid):
    return campaign_cells[tid][pid]


def short_claim(cid, limit=110):
    text = re.sub(r"\s+", " ", claim_by_id[cid].get("claim", "")).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "\u2026"



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
# The fill gate measures the EVIDENCE that feeds the scores. Ancillary tables
# that document the method (themes, legal basis, constituencies) are complete by
# construction; counting them would dilute the measure and let the gate pass
# while the claims still lack baselines. They are excluded on purpose.
GATE_EXCLUDE = {"themes.csv", "claim_themes.csv", "legal_basis.csv",
                "constituencies.csv", "results_2021.csv"}

total_cells = fill_cells = 0
counted_tables = []
for name, census in fill_census.items():
    if name in GATE_EXCLUDE:
        continue
    header = validator.SCHEMA[name]["header"]
    total_cells += row_counts.get(name, 0) * len(header)
    fill_cells += sum(census.values())
    counted_tables.append(name)
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
    "axes.edgecolor": "#8a7f6c",
    "axes.linewidth": 0.8,
    "axes.grid": True,
    "grid.color": "#e6dfd0",
    "grid.linewidth": 0.6,
    "figure.dpi": 150,
    # Warm paper tone keeps the exported SVG charts in the same family as the
    # Moroccan-palette report instead of floating on stark white.
    "figure.facecolor": "#fffdf8",
    "axes.facecolor": "#fffdf8",
    "savefig.facecolor": "#fffdf8",
})


# The palette comes from scripts/moroccan_theme.py, so the charts, the report
# chrome and the simulator cannot drift apart.
MOROC_RED, MOROC_RED_DEEP, MOROC_GREEN = RED, RED_DEEP, GREEN
MOROC_GOLD, MOROC_SAND, MOROC_CREAM = GOLD, SAND, CREAM
MOROC_BAND, MOROC_RULE, MOROC_GRID = BAND, RULE, GRID
MOROC_CMAP = matplotlib.colors.LinearSegmentedColormap.from_list(
    "morocco", CMAP_COLORS)
MOROC_CMAP_R = MOROC_CMAP.reversed()   # red at the low end, green at the high
MOROC_FLAG = matplotlib.colors.ListedColormap([MOROC_SAND, MOROC_GREEN])


def moroccan_spines(ax):
    """Green spines, cream plot area - the shared chart furniture."""
    ax.set_facecolor(MOROC_CREAM)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(MOROC_GREEN)
        ax.spines[side].set_linewidth(1.1)


def moroccan_axes(ax, title, pad=12, fontsize=10.5):
    moroccan_spines(ax)
    ax.set_title(title, color=MOROC_RED, fontsize=fontsize,
                 fontweight="bold", pad=pad)


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
        ax.plot([vs[i], ss[i]], [y[i], y[i]], color=MOROC_RULE, lw=2.2, zorder=1)
    ax.scatter(vs, y, s=110, c=colors, marker="o", edgecolors="white",
               linewidths=1.2, zorder=3, label="Vote share (%)")
    ax.scatter(ss, y, s=130, c=colors, marker="D", edgecolors="white",
               linewidths=1.2, zorder=3, label="Seat share (%)")
    ax.set_yticks(y)
    ax.set_yticklabels(names)
    ax.set_xlabel("Share of valid votes / seats (%)")
    moroccan_axes(ax, f"{BASELINE_YEAR}: how votes translated into seats "
                      f"(gap = distortion)")
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
    ax.axhline(1.0, color=MOROC_GREEN, lw=1.4, ls="--")
    for b, v in zip(bars, vals):
        ax.text(b.get_x() + b.get_width() / 2, v + 0.02, f"{v:.2f}",
                ha="center", va="bottom", fontsize=9)
    ax.set_ylabel("Advantage ratio  (seat share / vote share)")
    moroccan_axes(ax, f"{BASELINE_YEAR}: over- and under-representation "
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
                  fontsize=10, color=MOROC_GREEN)
    left = 0.0
    for n, s, c in zip(names, seats, colors):
        ax2.barh(0, s, left=left, color=c, edgecolor="white", height=0.55)
        if s >= 15:
            ax2.text(left + s / 2, 0, f"{n}\n{s}", ha="center", va="center",
                     fontsize=7.5, color="white")
        left += s
    ax2.set_xlim(0, HOUSE_SEATS)
    ax2.set_yticks([])
    ax2.set_title(f"Seats ({HOUSE_SEATS} total)", fontsize=10, color=MOROC_GREEN)
    ax2.set_xlabel("Seats")
    fig.suptitle(f"{BASELINE_YEAR}: from votes to seats - same colour, different slice",
                 y=1.04, color=MOROC_RED, fontweight="bold")
    moroccan_spines(ax1); moroccan_spines(ax2)
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
    moroccan_axes(ax, f"Seat-share trajectory, {min(years)}-{max(years)}")
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
    im = ax.imshow(mat, cmap=MOROC_CMAP_R, vmin=0, vmax=1, aspect="auto")
    ax.set_xticks(range(len(domains)))
    ax.set_xticklabels(domains, rotation=35, ha="right")
    ax.set_yticks(range(len(active)))
    ax.set_yticklabels([pname[p] for p in active])
    for i in range(len(active)):
        for j in range(len(domains)):
            if text[i][j]:
                ax.text(j, i, text[i][j], ha="center", va="center",
                        fontsize=7.5, color="#241f18")
    moroccan_axes(ax, "Promise ledger: status by party and domain "
                      "(green = delivered, red = failed)")
    cbar = fig.colorbar(im, ax=ax, fraction=0.03, label="0 = failed ... 1 = fulfilled")
    cbar.outline.set_edgecolor(MOROC_GREEN)
    cbar.ax.tick_params(colors="#4a4132")
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
    ax.scatter(xs, ys, s=90, c="#c1272d", alpha=0.85, zorder=3)
    for x, y, sid in points:
        ax.annotate(sid, (x, y), textcoords="offset points", xytext=(6, 6),
                    fontsize=9, color="#4a4132")
    ax.set_xlabel("Reliability (1 = weak ... 5 = official)")
    ax.set_ylabel("Political lean (-2 left ... +2 right)")
    moroccan_axes(ax, "Source map: reliability vs. lean (kept separate)")
    ax.set_xlim(2.5, 5.5)
    ax.set_ylim(-2.6, 2.6)
    ax.axhline(0, color=MOROC_RULE, lw=1)
    ax.axvline(3, color=MOROC_RULE, lw=1)
    return save(fig, "bias_reliability")


def chart_readiness():
    """Which CEAGI dimensions actually exist for each party. The point of the
    report right now is this picture, not a ranking."""
    labels = [f"{DIM_LABEL[d]} ({d})" for d in DIMS]
    mat = np.array([[1.0 if sub[p][d]["value"] is not None else 0.0 for d in DIMS]
                    for p in pids])
    fig, ax = plt.subplots(figsize=(8.5, 5))
    ax.imshow(mat, cmap=MOROC_FLAG, vmin=0, vmax=1, aspect="auto")
    ax.set_xticks(range(len(DIMS)))
    ax.set_xticklabels(labels, rotation=30, ha="right")
    ax.set_yticks(range(len(pids)))
    ax.set_yticklabels([pname[p] for p in pids])
    for i, p in enumerate(pids):
        for j, d in enumerate(DIMS):
            ok = sub[p][d]["value"] is not None
            ax.text(j, i, "ok" if ok else "missing", ha="center", va="center",
                    fontsize=8, fontweight="bold",
                    color="#fdfaf3" if ok else MOROC_RED_DEEP)
    moroccan_axes(ax, f"Evidence readiness per party: {CAMPAIGN_YEAR} campaign, "
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
           error_kw=dict(ecolor=MOROC_GREEN, lw=1.1))
    for i, v in enumerate(vals):
        ax.text(i, v + 0.01, f"{v:.3f}", ha="center", va="bottom", fontsize=9)
    ax.set_ylabel("CEAGI score (0-1)")
    moroccan_axes(ax, "Party accountability score (CEAGI) with 95% credible interval")
    ax.set_ylim(0, max(hi) * 1.15)
    return save(fig, "ceagi_ranking")


# ---------------------------------------------------------------------------
# Convergence charts. They join the chart wall like any other export; the
# convergence section carries the interactive version of the same matrix.
# ---------------------------------------------------------------------------
def chart_claim_convergence():
    """Bubble matrix: one row per theme, one column per party, area ~ claims.

    Shared rows (two or more parties) are banded. This is the chart that shows,
    at a glance, that e.g. child-and-family commitments are not a PJD-only
    position. A plain heatmap would say the same thing less legibly; a bubble
    matrix keeps "how many claims" and "which party" in one cell.
    """
    if not campaign_theme_order or not conv_parties:
        return None
    rows, cols = campaign_theme_order, conv_parties
    fig, ax = plt.subplots(figsize=(10.5, 0.44 * len(rows) + 2.0))
    for i, tid in enumerate(rows):
        n_parties = len(campaign_cells[tid])
        if n_parties >= 2:
            ax.axhspan(i - 0.5, i + 0.5, color=MOROC_BAND, zorder=0)
        for j, pid in enumerate(cols):
            cids = campaign_cells[tid].get(pid)
            if not cids:
                continue
            n = len(cids)
            ax.scatter(j, i, s=110 + 105 * (n - 1), color=pcolor[pid],
                       edgecolors=MOROC_GOLD, linewidths=0.9, zorder=3)
            ax.text(j, i, str(n), ha="center", va="center", fontsize=7.5,
                    fontweight="bold", color="#241f18", zorder=4)
    ax.set_xlim(-0.6, len(cols) - 0.4)
    ax.set_ylim(len(rows) - 0.5, -0.5)
    ax.set_xticks(range(len(cols)))
    ax.set_xticklabels([pname[p] for p in cols], fontsize=9)
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels([theme_meta[t]["label"] for t in rows], fontsize=8.5)
    ax.set_xticks(np.arange(-0.5, len(cols), 1), minor=True)
    ax.set_yticks(np.arange(-0.5, len(rows), 1), minor=True)
    ax.grid(which="minor", color="#e4dccb", linewidth=0.7)
    ax.grid(which="major", visible=False)
    ax.tick_params(which="minor", length=0)
    ax.tick_params(which="major", length=0)
    handles = [plt.Line2D([], [], marker="o", linestyle="",
                          markersize=math.sqrt(110 + 105 * (n - 1)) / 2.4,
                          markerfacecolor=MOROC_GOLD, markeredgecolor=MOROC_RED_DEEP,
                          label=f"{n} claim{'s' if n > 1 else ''}")
               for n in (1, 2, 3)]
    handles.append(plt.Rectangle((0, 0), 1, 1, facecolor=MOROC_BAND,
                                 edgecolor=MOROC_GREEN, linewidth=0.8,
                                 label="claimed by 2+ parties"))
    ax.legend(handles=handles, loc="lower right", frameon=False, fontsize=8,
              ncol=4, bbox_to_anchor=(1.0, 1.005))
    moroccan_axes(ax, f"{CAMPAIGN_YEAR} claim convergence: a filled bubble means "
                       f"the party has that theme, its size is how many claims",
                  pad=34)
    return save(fig, "claim_convergence_matrix")


def chart_party_echo():
    """Party x party matrix: how many themes any two parties both claim.

    The number is the count of shared themes; the percentage is the Jaccard
    overlap (shared / union), which does not reward a party merely for being
    large.
    """
    cols = conv_parties
    if len(cols) < 2:
        return None
    n = len(cols)
    mat = np.full((n, n), np.nan)
    for i, a in enumerate(cols):
        for j, b in enumerate(cols):
            if i != j:
                mat[i, j] = len(party_themes[a] & party_themes[b])
    cmap = MOROC_CMAP.copy()
    cmap.set_bad(MOROC_SAND)
    fig, ax = plt.subplots(figsize=(1.05 * n + 2.2, 0.85 * n + 1.6))
    im = ax.imshow(np.ma.masked_invalid(mat), cmap=cmap, aspect="auto",
                   vmin=0, vmax=max(1.0, float(np.nanmax(mat))))
    ax.set_xticks(range(n))
    ax.set_xticklabels([pname[p] for p in cols], fontsize=9)
    ax.set_yticks(range(n))
    ax.set_yticklabels([pname[p] for p in cols], fontsize=9)
    for i, a in enumerate(cols):
        for j, b in enumerate(cols):
            if i == j:
                ax.text(j, i, "self", ha="center", va="center", fontsize=7.5,
                        color="#8a7f6c")
                continue
            shared = len(party_themes[a] & party_themes[b])
            union = len(party_themes[a] | party_themes[b])
            jac = (shared / union) if union else 0.0
            dark = shared >= 0.5 * max(1.0, float(np.nanmax(mat)))
            ax.text(j, i, f"{shared}\n{jac:.0%}", ha="center", va="center",
                    fontsize=8, color="white" if dark else "#222222")
    moroccan_axes(ax, "Echo matrix: shared themes between each pair of parties "
                      "(count / Jaccard overlap)")
    cbar = fig.colorbar(im, ax=ax, fraction=0.035, label="shared themes")
    cbar.outline.set_edgecolor(MOROC_GREEN)
    cbar.ax.tick_params(colors="#4a4132")
    ax.grid(False)
    return save(fig, "party_echo_matrix")


CHART_SPECS = [
    ("Votes to seats", chart_vote_seat),
    ("Advantage ratio", chart_advantage),
    ("Seat composition", chart_composition),
    ("Seat-share trajectory", chart_trajectory),
    ("Evidence readiness", chart_readiness),
    ("CEAGI ranking", chart_ceagi),
    ("Promise ledger", chart_heatmap),
    ("Source map: reliability vs. lean", chart_bias_reliability),
    ("Claim convergence matrix", chart_claim_convergence),
    ("Party echo matrix", chart_party_echo),
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
# Grading transparency. The rules behind every class (Q/V/S/N), every
# verification score (1-5) and every confidence level live here, next to the
# code that prints them, so the report can explain itself.
# ---------------------------------------------------------------------------
CLASS_RULE = {
    "Q": ("Quantified", "a target with a number and a unit", 1.0),
    "V": ("Directional", "a commitment in principle, with no number", 0.4),
    "S": ("Slogan / identity", "a value statement, not a measurable promise", 0.1),
    "N": ("Negative / attack", "a criticism of a rival, not an offer", 0.2),
}
RUBRIC_RULE = {
    1: ("Unverifiable", "no baseline, no unit, no deadline"),
    2: ("Weak", "a target, but a missing baseline or deadline, and/or a single source"),
    3: ("Moderate", "complete, but single-source or contested"),
    4: ("Strong", "complete, corroborated by 2+ independent sources, feasible"),
    5: ("Audited", "independently verified by a neutral body"),
}
CONFIDENCE_RULE = {
    "HIGH": "official or verified, with corroborating sources",
    "MEDIUM": "one solid source",
    "LOW": "estimate or headline-only capture, still to verify",
    "ILLUSTRATIVE": "placeholder that demonstrates the pipeline - never cite",
}
source_by_id = {s["source_id"]: s for s in sources}


def source_links(raw):
    """Render a source_ids cell as links into the sources registry."""
    if is_missing(raw):
        return '<span class="miss">FILL</span>'
    out = []
    for tok in validator.split_ids(raw):
        if tok in source_by_id:
            out.append(f'<a class="src" href="#source-{esc(tok)}">{esc(tok)}</a>')
        else:
            out.append(f'<span class="unresolved">{esc(tok)}</span>')
    return ", ".join(out) if out else '<span class="miss">FILL</span>'


def grade_cell(c):
    """Plain-language 'why this grade' for one claim, from its own row."""
    cls = str(c.get("class") or "").strip()
    vs = as_float(c.get("verification_score"))
    n = len({t for t in validator.split_ids(c.get("source_ids"))
             if validator.SOURCE_ID_RE.match(t)})
    bits = []
    if cls in CLASS_RULE:
        bits.append(f"<b>{esc(cls)}</b> {esc(CLASS_RULE[cls][0].lower())}")
    if vs is not None and int(vs) in RUBRIC_RULE:
        bits.append(f"<b>{int(vs)}/5</b> {esc(RUBRIC_RULE[int(vs)][0].lower())}")
    if n >= 2:
        bits.append(f"{n} sources")
    elif n == 1:
        bits.append("single source")
    else:
        bits.append("no source")
    return '<span class="why">' + " &middot; ".join(bits) + "</span>"


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
        grade_cell(c), source_links(c.get("source_ids")), cell(c.get("confidence")),
        esc(c.get("domain")), cell(c.get("baseline")), cell(c.get("target")),
        cell(c.get("deadline")), cell(c.get("unit")),
    ])

historical_rows_html = []
for c in historical_claims:
    historical_rows_html.append([
        esc(c.get("election_year")), esc(c.get("claim_id")),
        esc(pname.get(c.get("party_id"), c.get("party_id"))),
        cell(c.get("class")), cell(c.get("verification_score")),
        f'<span class="claimtext">{cell(c.get("claim"))}</span>',
        grade_cell(c), source_links(c.get("source_ids")), cell(c.get("confidence")),
        esc(c.get("domain")), cell(c.get("baseline")), cell(c.get("target")),
        cell(c.get("deadline")), cell(c.get("unit")),
    ])

promise_rows = []
for r in promises:
    promise_rows.append([
        esc(r.get("promise_id")), esc(pname.get(r.get("party_id"), r.get("party_id"))),
        f"{esc(r.get('term_start'))}-{esc(r.get('term_end'))}",
        esc(r.get("promise")), esc(r.get("domain")),
        status_badge(r.get("status", "")),
        cell(r.get("outcome_metric")), source_links(r.get("source_ids")),
        cell(r.get("confidence")),
    ])

leader_rows = []
for r in leaders:
    leader_rows.append([
        esc(r.get("leader")), esc(pname.get(r.get("party_id"), r.get("party_id"))),
        esc(r.get("role")), cell(r.get("achievements")), cell(r.get("ownership")),
        cell(r.get("ostensible_motive")), cell(r.get("conflicts")),
        cell(r.get("skin_in_game")), source_links(r.get("source_ids")),
    ])

source_rows = []
for r in sources:
    sid = r.get("source_id")
    url = (r.get("url") or "").strip()
    title = esc(r.get("title"))
    if url and not is_missing(url):
        title = (f'<a href="{esc_attr(url)}" target="_blank" rel="noopener">'
                 f'{title}</a>')
        link = (f'<a href="{esc_attr(url)}" target="_blank" rel="noopener">'
                f'open &#8599;</a>')
    else:
        link = '<span class="miss">no URL</span>'
    source_rows.append([
        f'<span id="source-{esc(sid)}"></span>{esc(sid)}', title,
        esc(r.get("author_org")), cell(r.get("date")), esc(r.get("type")),
        esc(r.get("primary_secondary")), cell(r.get("reliability")),
        cell(r.get("bias_label")), cell(r.get("status")), link,
    ])

n_primary = sum(1 for s_ in sources if s_.get("primary_secondary") == "primary")
n_secondary = sum(1 for s_ in sources if s_.get("primary_secondary") == "secondary")

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


# ---------------------------------------------------------------------------
# Transparency blocks. Everything a reader needs to check the report without
# opening another file: the grading rules, the sourcing rule, the legal basis,
# the per-number provenance and a one-minute summary.
# ---------------------------------------------------------------------------
def n_sources(claim_row):
    return len({t for t in validator.split_ids(claim_row.get("source_ids"))
                if validator.SOURCE_ID_RE.match(t)})


class_counts: dict = {}
score_counts: dict = {}
for _c in campaign_claims:
    _k = str(_c.get("class") or "FILL").strip()
    class_counts[_k] = class_counts.get(_k, 0) + 1
    _v = as_float(_c.get("verification_score"))
    _sk = str(int(_v)) if _v is not None else "FILL"
    score_counts[_sk] = score_counts.get(_sk, 0) + 1

corr_buckets = {"0": 0, "1": 0, "2": 0, "3+": 0}
for _c in campaign_claims:
    _n = n_sources(_c)
    corr_buckets["3+" if _n >= 3 else str(_n)] += 1


def rule_table():
    rows = []
    for k in ("Q", "V", "S", "N"):
        label, meaning, _w = CLASS_RULE[k]
        rows.append([f"<b>{k}</b>", esc(label), esc(meaning),
                     f"{CFG['claim_weights'].get(k, CFG['default_claim_weight']):.1f}",
                     str(class_counts.get(k, 0))])
    return table(["Class", "Name", "The rule", "Weight",
                  f"{CAMPAIGN_YEAR} claims"], rows)


def rubric_table():
    rows = []
    for k in sorted(RUBRIC_RULE):
        label, meaning = RUBRIC_RULE[k]
        rows.append([f"<b>{k}/5</b>", esc(label), esc(meaning),
                     str(score_counts.get(str(k), 0))])
    return table(["Score", "Name", "What it requires",
                  f"{CAMPAIGN_YEAR} claims"], rows)


GLOSSARY = [
    ("CEAGI", "Composite Electoral Accountability &amp; Governance Index - this "
     "project's six-dimension score. It measures how evidenced a party's promises "
     "are and how well documented its record is, not how good its policies are."),
    ("Sub-index", "One of the six dimensions D, C, E, G, L, M. Each is normalised "
     "to 0-1; a dimension with no evidence is shown as n/a."),
    ("Claim", "A single promise or commitment taken from a party programme - one "
     "per row in <code>data/claims.csv</code>."),
    ("Quantified (Q)", "A claim with a number and a unit, e.g. 'raise the minimum "
     "wage to 5,000 dirhams'."),
    ("Verification score", "A 1-5 grade of how checkable a claim is: 1 = "
     "unverifiable, 5 = independently audited."),
    ("Corroborated", "Carried by at least two independent sources. A claim resting "
     "on one source is <b>not</b> corroborated under this project's rule."),
    ("Confidence", "How solid the sourcing is: HIGH, MEDIUM, LOW or ILLUSTRATIVE."),
    ("FILL", "A cell the registry does not have yet. It is printed, not hidden, so "
     "the hole stays visible."),
    ("n/a", "Not computable from the evidence. It is never replaced by a guess or "
     "by zero."),
    ("Quotient électoral", "The electoral quota. In Moroccan law it is the number "
     "of registered voters in a constituency divided by the seats allocated to it."),
    ("Plus fort reste", "Largest remainder - the rule that assigns the seats left "
     "over after the quota to the lists with the largest remainders."),
    ("Circonscription", "A voting district (constituency). 305 seats are filled in "
     "local constituencies and 90 in the twelve regional ones."),
    ("AMO", "Assurance Maladie Obligatoire - Morocco's compulsory health insurance."),
    ("Promise ledger", "The record of what parties promised in past terms and what "
     "happened (fulfilled, partial, failed, still open)."),
]

grades_html = f"""
<section>
  <h2>3. How every grade in this report is decided</h2>
  <p>Nothing here is a black box. Each claim carries a <b>class</b> (what kind of
  commitment it is) and a <b>verification score</b> (how checkable it is), both
  stored in <code>data/claims.csv</code>; each source carries a confidence level
  and a reliability score. The tables below are the exact rules, and the last
  column shows how many of this year's {len(campaign_claims)} campaign claims fall
  in each bucket.</p>
  <h3>Class - what kind of commitment is it?</h3>
  <div class="scroll">{rule_table()}</div>
  <p class="small">The class sets how much the claim counts in the credibility
  sub-index <b>C</b>: a quantified target counts fully, a vague commitment about
  40%, a slogan about 10%.</p>
  <h3>Verification score - how checkable is it?</h3>
  <div class="scroll">{rubric_table()}</div>
  <p class="small">Today no claim scores above 2, because most
  <code>baseline</code> cells are still <span class="miss">FILL</span>: the rubric
  ties a higher score to completeness (target + baseline + deadline). That is why
  the credibility sub-index is low for every party - it is a statement about the
  evidence, not about the party.</p>
  <h3>Confidence - how solid is the sourcing?</h3>
  <div class="scroll">
  {table(["Level", "Meaning"], [[f"<b>{k}</b>", esc(v)]
                                for k, v in CONFIDENCE_RULE.items()])}
  </div>
  <h3>Corroboration - the project's own rule</h3>
  <p>A claim is called <b>corroborated</b> only when two or more independent
  sources carry it. In this campaign, {corr_buckets['0']} claims have no source
  linked, {corr_buckets['1']} rest on a single source (so they are <b>not</b>
  corroborated), {corr_buckets['2']} have two, and {corr_buckets['3+']} have three
  or more.</p>
  <h3>What a score does <i>not</i> mean</h3>
  <ul class="findings">
    <li><b>Not a prediction.</b> A high CEAGI does not mean a party will keep its
    promises; it means its promises are better evidenced and its record better
    documented.</li>
    <li><b>Not a judgement of the policy.</b> We grade whether a claim can be
    checked, not whether it is wise, affordable or desirable.</li>
    <li><b>Not comparable across different coverage.</b> Where a sub-index is
    missing it is shown as <span class="na">n/a</span> and never guessed.</li>
  </ul>
  <h3>Glossary</h3>
  <dl class="glossary">
  {''.join(f'<dt>{t}</dt><dd>{d}</dd>' for t, d in GLOSSARY)}
  </dl>
</section>
"""

_law_blocks = []
for _lb in legal_basis:
    _sid = _lb.get("source_id")
    _src = source_by_id.get(_sid, {})
    _url = (_src.get("url") or "").strip()
    _link = (f'<a href="{esc_attr(_url)}" target="_blank" rel="noopener">'
             f'official text &#8599;</a>') if (_url and not is_missing(_url)) else ""
    _law_blocks.append(f"""
  <figure class="lawquote">
    <blockquote lang="ar" dir="rtl">{esc(_lb.get("quote_ar"))}</blockquote>
    <p class="lawen">{esc(_lb.get("quote_en"))}</p>
    <figcaption><span class="lawtag">{esc(_lb.get("article"))}</span>
    <b>{esc(_lb.get("instrument"))}</b> &middot;
    source <a class="src" href="#source-{esc(_sid)}">{esc(_sid)}</a>
    {("&middot; " + _link) if _link else ""}
    <br><span class="small">{esc(_lb.get("note"))}</span></figcaption>
  </figure>""")

legal_html = f"""
<section>
  <h2>6. Legal basis - what the law actually says</h2>
  <p>The seat arithmetic in the charts and the simulator comes from Morocco's
  organic law, not from convention. The text in force is <b>loi organique
  n&deg; 27.11 on the House of Representatives</b>, consolidated on 29 January
  2026 and amended most recently by <b>loi organique n&deg; 53.25</b> (January
  2026), and before that by n&deg; 04.21 (2021) and n&deg; 20.16 (2016). Two
  articles carry the arithmetic that decides the 395 seats:</p>
  {''.join(_law_blocks)}
  <h3>What this means in practice</h3>
  <ul class="findings">
    <li><b>395 seats, two levels.</b> 305 members are elected in local
    constituencies and 90 in regional constituencies; the law fixes the 90
    regional seats in a table across the twelve regions.</li>
    <li><b>Proportional, largest remainder.</b> Seats go to lists in proportion to
    their support, and the seats left over after the quota go to the lists with
    the largest remainders.</li>
    <li><b>The quota is based on registered voters, not votes cast.</b> That is
    the distinctive Moroccan rule. In practice few lists reach the quota, so most
    seats are decided at the largest-remainder step.</li>
    <li><b>No electoral threshold.</b> The 3%/6% threshold that applied from 2002
    to 2016 was removed by the 2021 reform; the current law sets none.</li>
    <li><b>The former national list is gone.</b> The 90 seats that used to be
    elected on a single national list are now elected in the regional
    constituencies.</li>
  </ul>
  <p class="small">The interactive seat simulator on this site applies the
  largest-remainder method to national vote totals that you enter. It is an
  illustration at national level: the real count runs constituency by
  constituency, on the registered-voter quotient. Read it with that in mind.</p>
</section>
"""

prov_rows = []
for _pid in sorted(pids, key=lambda p: (-(coverage[p]), pname[p])):
    for _d in DIMS:
        _s = sub[_pid][_d]
        prov_rows.append([
            esc(pname[_pid]), f"<b>{_d}</b> {esc(DIM_LABEL[_d])}",
            num_cell(_s["value"], "{:.2f}"), str(_s["n"]),
            esc(str(_s["year"])), esc(str(_s["detail"])),
        ])

_best_pair = (0, None, None)
for _i, _a in enumerate(conv_parties):
    for _b in conv_parties[_i + 1:]:
        _n = len(party_themes[_a] & party_themes[_b])
        if _n > _best_pair[0]:
            _best_pair = (_n, _a, _b)

key_findings = [
    f"<b>Read this as a workbench, not a verdict.</b> {fill_fraction:.1%} of the "
    f"registry is still empty and no party has a complete, well-evidenced score "
    f"(section 1).",
    f"The campaign is crowded: <b>{len(shared_themes)} of "
    f"{len(campaign_theme_order)}</b> policy themes are claimed by two or more "
    f"parties"
    + (f", and <b>{esc(pname[_best_pair[1]])}</b> and "
       f"<b>{esc(pname[_best_pair[2]])}</b> overlap on {_best_pair[0]} of them."
       if _best_pair[1] else "."),
    (f"The most crowded theme is <b>{esc(theme_meta[conv_top_theme]['label'])}</b>, "
     f"claimed by {len(conv_top_parties)} of {len(conv_parties)} parties.")
    if conv_top_theme else "",
    f"All {claim_rows} campaign claims carry a grade, but <b>{corr_buckets['1']}</b> "
    f"still rest on a single source, so they are not corroborated under the "
    f"project's own rule.",
    (f"In {BASELINE_YEAR} <b>{esc(pname[max(Aratio, key=lambda p: Aratio[p])])}</b> "
     f"was the most over-represented party (advantage ratio "
     f"{max(Aratio.values()):.2f}) and "
     f"<b>{esc(pname[min(Aratio, key=lambda p: Aratio[p])])}</b> the most "
     f"under-represented ({min(Aratio.values()):.2f}): the seat formula did not "
     f"turn votes into seats evenly.") if Aratio else "",
]
key_box = ('<div class="keybox"><div class="keyk">In one minute</div><ul>'
           + "".join(f"<li>{t}</li>" for t in key_findings if t) + "</ul></div>")


# ---------------------------------------------------------------------------
# Section - claim convergence. Rendered as HTML rather than a flat image so a
# reader can hover a bubble and read the exact claim behind it.
# ---------------------------------------------------------------------------
def stat_card(value, label, note=""):
    note_html = f'<div class="statn">{note}</div>' if note else ""
    return (f'<div class="stat"><div class="statv">{value}</div>'
            f'<div class="statl">{label}</div>{note_html}</div>')


conv_head = "".join(f'<th scope="col">{esc(pname[p])}</th>' for p in conv_parties)
conv_rows_html = []
for _tid in campaign_theme_order:
    _pm = campaign_cells[_tid]
    _cells = []
    for _p in conv_parties:
        _cids = _pm.get(_p)
        if not _cids:
            _cells.append('<td class="ccell cempty"></td>')
            continue
        _tip = "&#10;".join(esc_attr(f"{cid}: {short_claim(cid, 160)}") for cid in _cids)
        _size = 15 + min(len(_cids) - 1, 5) * 8
        _cells.append(
            f'<td class="ccell" title="{esc_attr(pname[_p])} &#10;{_tip}">'
            f'<span class="bub" style="width:{_size}px;height:{_size}px;'
            f'background:{pcolor[_p]}">{len(_cids)}</span></td>')
    _n = len(_pm)
    _badge = (f'<span class="pill">{_n} parties</span>' if _n >= 2
              else '<span class="pill lone">alone</span>')
    _rowcls = ' class="shared-row"' if _n >= 2 else ''
    conv_rows_html.append(
        f'<tr{_rowcls}><th class="th-left">{esc(theme_meta[_tid]["label"])}'
        f'<span class="tgrp">{esc(theme_meta[_tid]["group"])}</span>{_badge}</th>'
        + "".join(_cells) + "</tr>")
conv_matrix = (
    '<table class="conv"><thead><tr><th class="th-left">Theme &middot; group</th>'
    + conv_head + "</tr></thead><tbody>" + "".join(conv_rows_html)
    + "</tbody></table>")

ledger_rows = []
for _tid in campaign_theme_order:
    _pm = campaign_cells[_tid]
    if len(_pm) < 2:
        continue
    _ids = "<br>".join(
        f'<b>{esc(pname[_p])}</b> '
        + " ".join(f'<span class="cid">{esc(c)}</span>' for c in _pm[_p])
        for _p in conv_parties if _p in _pm)
    ledger_rows.append([
        esc(theme_meta[_tid]["label"]), esc(theme_meta[_tid]["group"]),
        f'<span class="pill">{len(_pm)}</span>',
        esc(", ".join(pname[_p] for _p in conv_parties if _p in _pm)), _ids])
conv_ledger = table(["Theme", "Group", "Parties", "Who", "Claim IDs (hover a bubble above)"],
                    ledger_rows, "smalltbl")

party_stat_rows = []
for _p in sorted(conv_parties, key=lambda p: (-len(party_shared[p]), pname[p])):
    _share = (len(party_shared[_p]) / len(party_themes[_p])) if party_themes[_p] else 0.0
    _excl = ", ".join(esc(theme_meta[t]["label"]) for t in sorted(party_exclusive[_p]))
    _carr = ", ".join(esc(theme_meta[t]["label"]) for t in party_carried[_p])
    party_stat_rows.append([
        f'<span class="swatch" style="background:{pcolor[_p]}"></span>{esc(pname[_p])}',
        str(len(party_themes[_p])), str(len(party_shared[_p])), f"{_share:.0%}",
        _excl or '<span class="na">none</span>',
        str(len(party_carried[_p])),
        _carr or '<span class="na">none</span>'])
conv_party_table = table(
    ["Party", "Themes", "Shared", "Convergence", "Themes nobody else claims",
     f"Carried from {BASELINE_YEAR}", "Which ones"],
    party_stat_rows, "smalltbl")

conv_stats = (
    stat_card(f"{len(shared_themes)}<span class='den'>/{len(campaign_theme_order)}</span>",
              "themes claimed by 2+ parties",
              f"{conv_duplication:.0%} of the themes in play are contested ground")
    + stat_card(f"{len(exclusive_themes)}", "single-party themes",
                "a position only one party is running on")
    + stat_card(f"{conv_claims_total}", "theme-tagged claims",
                f"{conv_shared_cells} of them sit on a shared theme")
    + stat_card(esc(pname.get(conv_most_derivative, "n/a")),
                "most converged party",
                f"{len(party_shared[conv_most_derivative])}/"
                f"{len(party_themes[conv_most_derivative])} of its themes "
                f"are shared" if conv_most_derivative else "")
    + stat_card(esc(pname.get(conv_most_distinctive, "n/a")),
                "most distinctive party",
                f"{len(party_exclusive[conv_most_distinctive])} theme(s) no "
                f"other party claims" if conv_most_distinctive else "")
)

if conv_spotlight:
    _sp = conv_spotlight
    _sp_parties = [p for p in conv_parties if p in campaign_cells[_sp]]
    _sp_list = "; ".join(
        f'<b>{esc(pname[p])}</b> '
        + ", ".join(f'<span class="cid">{esc(c)}</span>' for c in campaign_cells[_sp][p])
        for p in _sp_parties)
    conv_spotlight_html = (
        f'<div class="spotlight"><div class="spotk">Same promise, different '
        f'party &middot; spotlight</div>'
        f'<p><b>{esc(theme_meta[_sp]["label"])}</b> is claimed by '
        f'<b>{len(_sp_parties)} parties</b>: {_sp_list}. The mapping is not a '
        f'guess: each claim is tied to this theme in '
        f'<code>data/claim_themes.csv</code>, with the exact phrase that '
        f'justifies it.</p></div>')
else:
    conv_spotlight_html = ""

conv_top_line = ""
if conv_top_theme:
    conv_top_line = (
        f"The single most crowded theme is <b>{esc(theme_meta[conv_top_theme]['label'])}</b>: "
        f"{len(conv_top_parties)} of {len(conv_parties)} parties "
        f"({esc(', '.join(pname[p] for p in conv_top_parties))}) each carry at "
        f"least one claim on it.")

convergence_html = f"""
<section id="convergence">
  <h2>9. Claim convergence - who is promising the same thing</h2>
  <p>{conv_claims_total} claims from {len(conv_parties)} parties are tagged onto
  {len(campaign_theme_order)} policy themes. Where two or more parties land on
  the same theme, their programmes overlap - the promise is duplicated even when
  the wording is not. {conv_top_line}</p>
  <p class="small">Method: every claim is mapped to one or more themes in
  <code>data/claim_themes.csv</code>, an explicit editorial layer whose rows
  quote the phrase in the claim that justifies the tag. The controlled
  vocabulary is <code>data/themes.csv</code>. Nothing is inferred by keyword at
  build time; change the CSV and the picture changes. A theme is
  <b>shared</b> when two or more parties claim it, <b>alone</b> when only one
  does.</p>
  <div class="stats">{conv_stats}</div>

  {conv_spotlight_html}
  <div class="scroll convwrap">{conv_matrix}</div>
  <p class="small">Each bubble is one party on one theme; the number in it is
  how many of that party's claims touch the theme. Gold-banded rows are the
  shared themes. Hover a bubble for the claim text behind it. The same matrix is
  exported as <code>output/charts/claim_convergence_matrix.svg</code>, and the
  pairwise view as <code>output/charts/party_echo_matrix.svg</code>.</p>

  <h3>Duplication ledger - the shared themes in full</h3>
  <p class="small">Every theme claimed by two or more parties, with the claim
  IDs on each side. This is the list to read before saying a promise is
  "owned" by one party.</p>
  <div class="scroll">{conv_ledger}</div>

  <h3>Convergence and distinctiveness by party</h3>
  <p class="small"><b>Convergence</b> is the share of a party's themes that at
  least one other party also claims: high means a crowded, undifferentiated
  platform; low means a distinctive one. <b>Carried from {BASELINE_YEAR}</b>
  counts themes the same party already ran on in the previous campaign - a
  promise repeated is not a promise added.</p>
  <div class="scroll">{conv_party_table}</div>
</section>
"""


from moroccan_theme import (  # noqa: E402
    STAR_URI, ZELLIGE_URI, STAR_BADGE_SVG, apply_tokens,
    RED, RED_DEEP, GREEN, GOLD, SAND, CREAM, INK, MUTED, GRID, RULE, BAND,
    CMAP_COLORS, OK, WARN, BAD,
)

CSS = """
:root {
  --red:#c1272d;          /* Moroccan flag red */
  --red-deep:#8e1b20;
  --green:#006233;        /* Moroccan flag green */
  --green-bright:#0a7d43;
  --gold:#c8a24a;
  --sand:#f6efe0;
  --ink:#1c1a17;
  --muted:#6d6459;
  --line:#e4dbc9;
  --bg:#fbf8f1;
}
* { box-sizing:border-box; }
html { scroll-behavior:smooth; }
body { margin:0; font-family:Georgia,'Times New Roman',serif; color:var(--ink);
       background:var(--bg); line-height:1.55; }
body::before { content:""; display:block; height:5px;
  background:linear-gradient(90deg,var(--green) 0 34%,var(--gold) 34% 66%,var(--red) 66% 100%); }
a { color:var(--green); }
a:hover { color:var(--red); }

/* -- masthead: Moroccan flag red, zellige tile, pentagram star -- */
.masthead { position:relative; overflow:hidden; color:#fff;
  background:radial-gradient(125% 145% at 86% 6%,#d8434a 0%,var(--red) 46%,var(--red-deep) 100%);
  border-bottom:5px solid var(--green);
  box-shadow:0 3px 0 var(--gold); }
.masthead-bg { position:absolute; inset:0; pointer-events:none;
  background-image:url("__ZELLIGE__"); background-size:68px 68px;
  opacity:.95; }
.masthead-inner { position:relative; max-width:1180px; margin:0 auto;
  padding:34px 22px 30px; display:flex; gap:28px; align-items:center; }
.flagmark { flex:0 0 auto; width:116px; height:116px;
  filter:drop-shadow(0 8px 16px rgba(0,0,0,.38)); }
.flagmark svg { width:100%; height:100%; display:block; }
.masthead-copy { min-width:0; }
.kicker { font-family:Helvetica,Arial,sans-serif; text-transform:uppercase;
  letter-spacing:.2em; color:var(--gold); font-size:.74rem; font-weight:700; }
.kicker .ar { font-family:'Amiri','Traditional Arabic','Noto Naskh Arabic',serif;
  letter-spacing:0; font-size:1.08rem; text-transform:none; vertical-align:-2px;
  direction:rtl; unicode-bidi:isolate; }
.masthead h1 { font-size:2.7rem; margin:.3rem 0 .45rem; line-height:1.06;
  color:#fffdf7; text-shadow:0 2px 0 rgba(0,0,0,.2); }
.masthead .deck { font-size:1.1rem; color:#fbe6e6; max-width:760px; margin:.15rem 0 1.05rem; }
.chips { display:flex; flex-wrap:wrap; gap:8px; }
.chip { font-family:Helvetica,Arial,sans-serif; font-size:.71rem; font-weight:700;
  letter-spacing:.05em; text-transform:uppercase; color:#fff;
  background:rgba(0,0,0,.2); border:1px solid rgba(255,255,255,.3);
  border-radius:999px; padding:4px 11px; }
.chip-link { text-decoration:none; background:var(--green); border-color:var(--gold); }
.chip-link:hover { background:var(--gold); color:#3a2c07; }

.wrap { max-width:1180px; margin:0 auto; padding:0 22px 60px; }
.colophon { font-family:Helvetica,Arial,sans-serif; font-size:.78rem;
  color:var(--muted); background:#fff; border-left:4px solid var(--gold);
  padding:9px 14px; margin:20px 0 0; }
.colophon code { color:var(--green); }

h2 { font-family:Helvetica,Arial,sans-serif; font-size:1.25rem;
     color:var(--green); border-bottom:2px solid var(--green);
     padding-bottom:7px; margin:44px 0 16px;
     display:flex; align-items:center; gap:.55em; }
h2::before { content:""; flex:0 0 auto; width:1.05em; height:1.05em;
     background:url("__STAR__") center/contain no-repeat; }
h3 { font-family:Helvetica,Arial,sans-serif; font-size:1rem; color:var(--red);
     margin:26px 0 10px; }
.deck { font-size:1.12rem; color:#333; max-width:780px; }
.meta { font-family:Helvetica,Arial,sans-serif; font-size:.8rem; color:var(--muted);
        margin-top:10px; }

.banner { padding:14px 18px; margin:22px 0; font-family:Helvetica,Arial,sans-serif;
          font-size:.88rem; border-left:6px solid var(--red); background:#fff; }
.bad-banner { background:#fdf2f1; }
.ok-banner { background:#f0f7f2; border-left-color:var(--green); }
.banner b { color:var(--red); }
.ok-banner b { color:var(--green); }
ul.findings { font-size:1.02rem; padding-left:20px; }
ul.findings li { margin-bottom:8px; }
table { border-collapse:collapse; width:100%; margin:14px 0;
        font-family:Helvetica,Arial,sans-serif; font-size:.8rem; background:#fff; }
th { background:var(--sand); text-align:left; padding:8px 9px; color:#4a4132;
     border-bottom:2px solid var(--green); font-size:.72rem; text-transform:uppercase;
     letter-spacing:.04em; position:sticky; top:0; }
td { padding:7px 9px; border-bottom:1px solid var(--line); vertical-align:top; }
tr:hover td { background:#fdf9f0; }
.badge { color:#fff; padding:2px 8px; border-radius:3px; font-size:.72rem;
         font-weight:600; white-space:nowrap; }
.miss { color:var(--red); background:#fdf2f1; border:1px solid #f0cdcb;
        padding:1px 6px; border-radius:3px; font-size:.72rem; font-weight:600; }
.na { color:#8a8378; font-style:italic; }
.ok { color:var(--green); font-weight:700; }
.bad { color:var(--red); font-weight:700; }
.sev-error { color:var(--red); font-weight:700; }
.sev-warn { color:#a8741a; font-weight:700; }
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
           border:1px solid var(--line); border-left:4px solid var(--green);
           padding:10px 14px; margin:10px 0; overflow-x:auto; }
.small { font-family:Helvetica,Arial,sans-serif; font-size:.78rem; color:var(--muted); }
.scroll { overflow-x:auto; }
.stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(168px,1fr));
         gap:10px; margin:18px 0; }
.stat { background:#fff; border:1px solid var(--line); border-top:3px solid var(--red);
        padding:10px 12px; }
.stat:nth-child(even) { border-top-color:var(--green); }
.statv { font-family:Helvetica,Arial,sans-serif; font-size:1.45rem; font-weight:700;
         line-height:1.1; }
.den { font-size:.85rem; color:var(--muted); font-weight:400; }
.statl { font-family:Helvetica,Arial,sans-serif; font-size:.7rem; text-transform:uppercase;
         letter-spacing:.05em; color:#444; margin-top:4px; }
.statn { font-family:Helvetica,Arial,sans-serif; font-size:.72rem; color:var(--muted);
         margin-top:5px; }
table.conv { font-size:.78rem; width:100%; }
table.conv th, table.conv td { text-align:center; padding:4px 5px; vertical-align:middle; }
table.conv th.th-left { text-align:left; white-space:nowrap; }
table.conv thead th { font-size:.66rem; }
table.conv tr.shared-row th, table.conv tr.shared-row td { background:#f7efd9; }
table.conv tr.shared-row th.th-left { box-shadow:inset 3px 0 0 var(--gold); }
.tgrp { display:inline-block; margin-left:7px; font-weight:400; color:var(--muted);
        font-size:.66rem; text-transform:uppercase; letter-spacing:.04em; }
.bub { display:inline-flex; align-items:center; justify-content:center;
       border-radius:50%; color:#fff; font-weight:700; font-size:.68rem;
       box-shadow:inset 0 0 0 1px rgba(0,0,0,.3); }
.pill { display:inline-block; margin-left:7px; background:var(--sand); color:#5b5140;
        border-radius:10px; padding:0 7px; font-size:.66rem; font-weight:700;
        white-space:nowrap; }
.pill.lone { background:#efeee9; color:#999999; }
.swatch { display:inline-block; width:10px; height:10px; border-radius:2px;
          margin-right:6px; }
.cid { font-family:ui-monospace,Menlo,Consolas,monospace; background:#f4efe3;
       border:1px solid var(--line); border-radius:3px; padding:0 4px; font-size:.7rem; }
.spotlight { background:#fffdf6; border:1px solid var(--line);
             border-left:5px solid var(--red); padding:12px 16px; margin:16px 0; }
.spotlight p { margin:0; }
.spotk { font-family:Helvetica,Arial,sans-serif; text-transform:uppercase;
         letter-spacing:.1em; font-size:.68rem; font-weight:700; color:var(--red);
         margin-bottom:6px; }
.smalltbl { font-size:.76rem; }
.keybox { background:#fffdf6; border:1px solid var(--line); border-left:5px solid var(--green);
          padding:14px 18px; margin:22px 0; }
.keyk { font-family:Helvetica,Arial,sans-serif; text-transform:uppercase;
        letter-spacing:.1em; font-size:.68rem; font-weight:700; color:var(--green);
        margin-bottom:6px; }
.keybox ul { margin:0; padding-left:20px; }
.keybox li { margin-bottom:7px; }
a.src { font-family:ui-monospace,Menlo,Consolas,monospace; font-size:.72rem;
        text-decoration:none; border-bottom:1px dotted var(--green); }
a.src:hover { color:var(--red); border-bottom-color:var(--red); }
.unresolved { color:#a8741a; font-weight:700; }
.why { color:#5b5346; font-size:.72rem; white-space:nowrap; }
table.claims { min-width:1440px; }
.lawquote { margin:26px 0; }
.lawquote blockquote { margin:0; background:#fff; border:1px solid var(--line);
  border-top:5px solid var(--gold); padding:20px 24px;
  font-family:'Amiri','Traditional Arabic','Noto Naskh Arabic',serif;
  font-size:1.35rem; line-height:2; color:var(--ink); direction:rtl; text-align:right; }
.lawquote .lawen { font-style:italic; color:#3a352c; background:#fffdf6;
  border-left:4px solid var(--green); padding:12px 16px; margin:10px 0 8px; }
.lawquote .lawtag { display:inline-block; background:var(--red); color:#fff;
  font-family:Helvetica,Arial,sans-serif; font-size:.66rem; font-weight:700;
  letter-spacing:.06em; text-transform:uppercase; border-radius:3px;
  padding:2px 7px; margin-right:8px; vertical-align:1px; }
.lawquote figcaption { font-family:Helvetica,Arial,sans-serif; font-size:.78rem;
  color:var(--muted); line-height:1.6; }
.glossary { margin:10px 0 0; padding:0; font-family:Helvetica,Arial,sans-serif;
  font-size:.82rem; }
.glossary dt { font-weight:700; color:var(--green); margin-top:9px; }
.glossary dd { margin:1px 0 0 0; color:#3f3a32; }
footer { margin-top:50px; padding-top:16px; border-top:4px solid var(--green);
         box-shadow:inset 0 2px 0 var(--gold);
         font-family:Helvetica,Arial,sans-serif; font-size:.78rem; color:var(--muted); }
@media (max-width:820px) {
  .masthead-inner { flex-direction:column; align-items:flex-start; gap:16px; }
  .flagmark { width:86px; height:86px; }
  .masthead h1 { font-size:1.85rem; }
  .tl { grid-template-columns:1fr; }
}
"""
CSS = CSS.replace("__STAR__", STAR_URI).replace("__ZELLIGE__", ZELLIGE_URI)

html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Morocco {CAMPAIGN_YEAR} - Electoral Claims, Promises &amp; Accountability</title>
<style>{CSS}</style>
</head>
<body>
<header class="masthead">
  <div class="masthead-bg" aria-hidden="true"></div>
  <div class="masthead-inner">
    <div class="flagmark">{STAR_BADGE_SVG}</div>
    <div class="masthead-copy">
      <div class="kicker">Royaume du Maroc &middot; <span class="ar" dir="rtl">&#1575;&#1604;&#1605;&#1605;&#1604;&#1603;&#1577; &#1575;&#1604;&#1605;&#1594;&#1585;&#1576;&#1610;&#1577;</span> &middot; {esc(countdown)}</div>
      <h1>Electoral Claims, Promises &amp; Accountability</h1>
      <p class="deck">What the parties are promising for {CAMPAIGN_YEAR}, how votes
      became seats in {BASELINE_YEAR}, and which numbers we can actually stand behind.</p>
      <div class="chips">
        <span class="chip">Campaign {CAMPAIGN_YEAR}</span>
        <span class="chip">Baseline {BASELINE_YEAR}</span>
        <span class="chip">{claim_rows} claims</span>
        <span class="chip">{len(shared_themes)} shared themes</span>
        <a class="chip chip-link" href="seat_simulator.html">Seat simulator &#8594;</a>
      </div>
    </div>
  </div>
</header>

<div class="wrap">

  <div class="colophon">Generated from the <code>data/</code> registry by
  <code>scripts/build.py</code> &middot; model CEAGI &middot;
  {len(sources)} sources, {claim_rows} campaign claims
  ({len(historical_claims)} historical), {len(promises)} promises,
  {len(pids)} parties &middot; convergence: {len(campaign_theme_order)} themes,
  {len(shared_themes)} shared by 2+ parties &middot;
  config: campaign {CAMPAIGN_YEAR} / baseline {BASELINE_YEAR}.</div>

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

{key_box}

<section>
  <h2>1. Publication gate</h2>
  <p class="small">Every gate must pass before this report is used editorially.
  Run <code>python3 scripts/validate.py</code> for the full finding list.
  The fill measure counts the <b>evidence</b> tables
  ({esc(', '.join(counted_tables))}); the method tables (theme vocabulary,
  claim-theme mapping, legal-basis quotes, constituencies) are complete by
  construction and are deliberately excluded, so adding them cannot make the
  gate pass while claims still lack baselines.</p>
  {table(["Gate", "Status", "Measured"], gate_rows)}
</section>

<section>
  <h2>2. What the data supports, and what it does not</h2>
  <h3>Supportable today</h3>
  <ul class="findings">{findings_html or '<li class="small">Nothing yet.</li>'}</ul>
  <h3>Not supportable yet</h3>
  <ul class="findings">{unfindings_html or '<li class="small">No blocked statements.</li>'}</ul>
</section>

{grades_html}

<section>
  <h2>4. To do before publishing</h2>
  <p class="small">Generated automatically, ordered by how many rows each gap blocks.</p>
  <ol class="findings">{actions_html or '<li class="small">Nothing outstanding.</li>'}</ol>
</section>

<section>
  <h2>5. CEAGI - party accountability score</h2>
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
  <h3>Where each number comes from</h3>
  <p class="small">Every sub-index with its sample size, the year it describes and
  the rule that produced it. <code>n</code> is how many claims, promises or
  election rows fed the value. Rows that say "Assigned" are the project's own
  documented judgements in <code>data/indicators.csv</code>, not measurements.</p>
  <div class="scroll">
  {table(["Party", "Dimension", "Value", "n", "Year", "How it was derived"],
         prov_rows, "smalltbl")}
  </div>
</section>

{legal_html}

<section>
  <h2>7. Charts</h2>
  {chart_blocks}
</section>

<section>
  <h2>8. {CAMPAIGN_YEAR} campaign claims</h2>
  <p class="small">{len(campaign_claims)} claims from {len({c.get('party_id') for c in campaign_claims})}
  parties. <span class="miss">FILL</span> marks a cell the registry does not have
  yet - it is shown rather than hidden so the hole is visible.</p>
  <div class="scroll">
  {table(["ID", "Party", "Class", "Verif.", "Claim", "Why this grade",
          "Sources", "Conf.", "Domain", "Baseline", "Target", "Deadline",
          "Unit"], claim_rows_html, "claims")}
  </div>
</section>

{convergence_html}

<section>
  <h2>10. Historical campaign claims ({', '.join(sorted({c.get('election_year') for c in historical_claims}))})</h2>
  <p class="small">{len(historical_claims)} claims from the earlier campaign(s), kept
  as a traceable record of what each party promised. They are not scored in the
  current CEAGI run.</p>
  <div class="scroll">
  {table(["Year", "ID", "Party", "Class", "Verif.", "Claim", "Why this grade",
          "Sources", "Conf.", "Domain", "Baseline", "Target", "Deadline",
          "Unit"], historical_rows_html, "claims")}
  </div>
</section>

<section>
  <h2>11. Promise ledger (past terms)</h2>
  {table(["ID", "Party", "Term", "Promise", "Domain", "Status", "Outcome",
          "Sources", "Conf."], promise_rows)}
</section>

<section>
  <h2>12. Leaders - achievements, ownership, ostensible motives</h2>
  <p class="small">"Skin in the game" = 0-1 judgement of how tightly the leader's
  personal fortune or career is tied to the promised outcomes (1 = fully exposed).
  Ownership and motive cells are claims requiring corroboration.</p>
  <div class="scroll">
  {table(["Leader", "Party", "Role", "Achievements", "Ownership",
          "Ostensible motive", "Conflicts", "Skin", "Sources"], leader_rows)}
  </div>
</section>

<section>
  <h2>13. Sources registry</h2>
  <div class="scroll">
  {table(["ID", "Title", "Author/Org", "Date", "Type", "Primary?", "Rel.",
          "Lean", "Status", "Link"], source_rows)}
  </div>
  <p class="small">The registry holds {n_primary} <b>primary</b> documents
  (laws, official texts) and {n_secondary} <b>secondary</b> reports. Every claim
  table above links its source IDs to the matching row here, and every row links
  out to the original. Where a claim rests on a single source it is marked as
  not corroborated - see section 3 for the rule.</p>
</section>

<section>
  <h2>14. Timeline</h2>
  {timeline_html}
</section>

<section>
  <h2>15. Validation findings</h2>
  <p class="small">Produced by <code>scripts/validate.py</code>. ERROR breaks an
  invariant, WARN is incomplete but sound, INFO is coverage.</p>
  <div class="scroll">
  {table(["Sev.", "Code", "Count", "Example"], findings_table)}
  </div>
</section>

<section>
  <h2>16. Methodology (short)</h2>
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


# ---------------------------------------------------------------------------
# Claim-convergence outputs. The report section is a view; these are the data.
# ---------------------------------------------------------------------------
conv_pairwise = []
for _i, _a in enumerate(conv_parties):
    for _b in conv_parties[_i + 1:]:
        _shared = sorted(party_themes[_a] & party_themes[_b],
                         key=lambda t: campaign_theme_order.index(t))
        _union = party_themes[_a] | party_themes[_b]
        conv_pairwise.append({
            "party_a": _a, "party_b": _b,
            "shared_themes": len(_shared),
            "jaccard": round(len(_shared) / len(_union), 3) if _union else 0.0,
            "themes": _shared,
        })
conv_pairwise.sort(key=lambda r: -r["shared_themes"])

write_json(OUT / "claim_overlap.json", {
    "generated": dt.datetime.now().isoformat(timespec="seconds"),
    "campaign_year": CAMPAIGN_YEAR,
    "method": ("claim -> theme mapping in data/claim_themes.csv (one row per "
               "claim/theme pair, each with a literal anchor from the claim); "
               "a theme is 'shared' when two or more parties claim it"),
    "totals": {
        "themes_in_play": len(campaign_theme_order),
        "themes_total": len(theme_meta),
        "shared_themes": len(shared_themes),
        "exclusive_themes": len(exclusive_themes),
        "unused_themes": len(unused_themes),
        "duplication_index": round(conv_duplication, 3),
        "theme_tagged_claims": conv_claims_total,
        "theme_tagged_claims_on_shared_themes": conv_shared_cells,
        "parties": len(conv_parties),
    },
    "themes": [
        {
            "theme_id": _tid, "label": theme_meta[_tid]["label"],
            "group": theme_meta[_tid]["group"],
            "shared": len(campaign_cells[_tid]) >= 2,
            "n_parties": len(campaign_cells[_tid]),
            "n_claims": sum(len(v) for v in campaign_cells[_tid].values()),
            "parties": {_p: campaign_cells[_tid][_p] for _p in conv_parties
                        if _p in campaign_cells[_tid]},
        }
        for _tid in campaign_theme_order
    ],
    "parties": [
        {
            "party_id": _p, "name": pname[_p],
            "themes": sorted(party_themes[_p],
                             key=lambda t: campaign_theme_order.index(t)),
            "shared_themes": sorted(party_shared[_p],
                                    key=lambda t: campaign_theme_order.index(t)),
            "exclusive_themes": sorted(party_exclusive[_p]),
            "carried_over_from_baseline": party_carried[_p],
            "convergence": round(len(party_shared[_p]) / len(party_themes[_p]), 3)
                           if party_themes[_p] else None,
        }
        for _p in sorted(conv_parties, key=lambda p: (-len(party_shared[p]), pname[p]))
    ],
    "pairwise": conv_pairwise,
})

with open(OUT / "claim_overlap.csv", "w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh)
    w.writerow(["theme_id", "theme", "group", "n_parties", "n_claims",
                "shared", "parties", "claim_ids"])
    for _tid in campaign_theme_order:
        _pm = campaign_cells[_tid]
        w.writerow([
            _tid, theme_meta[_tid]["label"], theme_meta[_tid]["group"],
            len(_pm), sum(len(v) for v in _pm.values()),
            "yes" if len(_pm) >= 2 else "no",
            "; ".join(f"{pname[p]}({len(_pm[p])})" for p in conv_parties if p in _pm),
            "; ".join(f"{pname[p]}:{'|'.join(_pm[p])}" for p in conv_parties if p in _pm),
        ])

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

lines += ["", "## 4c. Claim-theme mapping and convergence", "",
          f"Method: `data/claim_themes.csv` maps each claim to one or more themes "
          f"from the controlled vocabulary in `data/themes.csv`; every mapping "
          f"row quotes a literal anchor from the claim text. "
          f"`{len(shared_themes)}` of `{len(campaign_theme_order)}` themes in play "
          f"for {CAMPAIGN_YEAR} are claimed by two or more parties "
          f"(duplication index {conv_duplication:.0%}).", "",
          "| Theme | Group | Parties | Claim IDs |",
          "|---|---|---|---|"]
for _tid in campaign_theme_order:
    _pm = campaign_cells[_tid]
    _who = ", ".join(f"{pname[p]} ({len(_pm[p])})" for p in conv_parties if p in _pm)
    _ids = "; ".join(f"{pname[p]}: {', '.join(_pm[p])}"
                     for p in conv_parties if p in _pm)
    lines.append(f"| {theme_meta[_tid]['label']} | {theme_meta[_tid]['group']} | "
                 f"{_who} | {_ids} |")

lines += ["", "### Carried-over themes (same party, previous campaign)", "",
          "| Party | Themes run in both campaigns |", "|---|---|"]
for _p in conv_parties:
    _carried = ", ".join(theme_meta[t]["label"] for t in party_carried[_p])
    lines.append(f"| {pname[_p]} | {_carried or 'none'} |")

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
print(f"[ok] Overlap   -> claim_overlap.csv / claim_overlap.json")
print(f"[conv] themes={len(campaign_theme_order)} shared={len(shared_themes)} "
      f"({conv_duplication:.0%}) exclusive={len(exclusive_themes)} "
      f"theme_tagged_claims={conv_claims_total}")
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
