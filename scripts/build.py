#!/usr/bin/env python3
"""
Morocco elections — Reuters-style report builder.

Reads auditable CSVs from data/, computes the CEAGI party score with a
Monte-Carlo uncertainty pass, renders SVG charts, and assembles a single
self-contained HTML report plus an audit trail.

Usage:  python3 scripts/build.py
"""
import csv
import json
import math
import re
import sys
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.sankey import Sankey

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = ROOT / "output"
CHARTS = OUT / "charts"
OUT.mkdir(parents=True, exist_ok=True)
CHARTS.mkdir(parents=True, exist_ok=True)

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
WEIGHTS = {"D": 0.25, "C": 0.20, "E": 0.10, "G": 0.20, "L": 0.15, "M": 0.10}
CLASS_W = {"Q": 1.0, "V": 0.4, "S": 0.1, "N": 0.2}
DONE = {"fulfilled", "partial", "failed", "abandoned"}
STATUS_NUM = {
    "fulfilled": 1.00, "partial": 0.66, "in_progress": 0.55,
    "pending": 0.33, "failed": 0.00, "abandoned": 0.00, "unverifiable": 0.25,
}
STATUS_COLOR = {
    "fulfilled": "#2e7d32", "partial": "#9ccc65", "in_progress": "#fbc02d",
    "pending": "#ffb74d", "failed": "#c62828", "abandoned": "#616161",
    "unverifiable": "#9e9e9e",
}
N_SAMPLES = 8000
SEED = 42
HOUSE_SEATS = 395
YEAR = 2021


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def load(name):
    with open(DATA / name, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


parties = load("parties.csv")
elections = load("elections.csv")
claims = load("claims.csv")
promises = load("promises.csv")
leaders = load("leaders.csv")
sources = load("sources.csv")
timeline = load("timeline.csv")
indicators = load("indicators.csv")

pids = [p["party_id"] for p in parties if p["party_id"] != "P009"]
pname = {p["party_id"]: p["name"] for p in parties}
pcolor = {p["party_id"]: p["color"] for p in parties}

# ----------------------------------------------------------------------------
# Sub-index computation
# ----------------------------------------------------------------------------
def compute_D():
    out = {}
    for pid in pids:
        rows = [r for r in promises if r["party_id"] == pid and r["status"] in DONE]
        F = sum(1 for r in rows if r["status"] == "fulfilled")
        P = sum(1 for r in rows if r["status"] == "partial")
        X = sum(1 for r in rows if r["status"] == "failed")
        A = sum(1 for r in rows if r["status"] == "abandoned")
        denom = F + P + X + A
        unv = sum(1 for r in promises if r["party_id"] == pid and r["status"] == "unverifiable")
        out[pid] = {
            "D": round((F + 0.5 * P) / denom, 3) if denom else 0.5,
            "F": F, "P": P, "X": X, "A": A, "unverifiable": unv, "tracked": len(rows),
        }
    return out


def compute_C():
    out = {}
    for pid in pids:
        rows = [r for r in claims if r["party_id"] == pid]
        if not rows:
            out[pid] = 0.5
            continue
        num = sum(CLASS_W.get(r["class"], 0.4) * int(r["verification_score"]) for r in rows)
        den = 5 * sum(CLASS_W.get(r["class"], 0.4) for r in rows)
        out[pid] = round(num / den, 3) if den else 0.5
    return out


def compute_E(year=YEAR):
    rows = [r for r in elections if int(r["election_year"]) == year and r["party_id"] != "P009"]
    S = sum(int(r["seats"]) for r in rows)
    V = sum(float(r["vote_share_est"]) for r in rows)
    A = {}
    for r in rows:
        pid = r["party_id"]
        s = int(r["seats"]) / S
        v = float(r["vote_share_est"]) / V
        A[pid] = s / v
    maxdev = max(abs(a - 1) for a in A.values())
    E = {
        pid: round(1 - abs(a - 1) / maxdev, 3) if maxdev > 1e-9 else 1.0
        for pid, a in A.items()
    }
    return E, A, S, V


Dmap = compute_D()
Cmap = compute_C()
Emap, Aratio, tot_seats, tot_votes = compute_E()

ind = {}
for r in indicators:
    if int(r["election_year"]) == YEAR:
        ind[r["party_id"]] = {
            "G": float(r["g_score"]), "L": float(r["l_score"]), "M": float(r["m_score"]),
        }

sub = {}
for pid in pids:
    sub[pid] = {
        "D": Dmap[pid]["D"],
        "C": Cmap[pid],
        "E": Emap.get(pid, 0.5),
        "G": ind.get(pid, {"G": 0.5})["G"],
        "L": ind.get(pid, {"L": 0.5})["L"],
        "M": ind.get(pid, {"M": 0.5})["M"],
    }


def geo(vals, w=WEIGHTS):
    logsum = 0.0
    wsum = 0.0
    for k, wv in w.items():
        x = max(vals[k], 1e-3)
        logsum += wv * math.log(x)
        wsum += wv
    return math.exp(logsum / wsum)


score = {pid: round(geo(sub[pid]), 3) for pid in pids}

# ----------------------------------------------------------------------------
# Monte-Carlo uncertainty (beta sampling around each sub-index)
# ----------------------------------------------------------------------------
rng = np.random.default_rng(SEED)
ci = {}
for pid in pids:
    s = sub[pid]
    samples = np.empty(N_SAMPLES)
    for i in range(N_SAMPLES):
        v = {}
        for k, wv in WEIGHTS.items():
            mu = s[k]
            v[k] = float(np.clip(rng.normal(mu, 0.05), 1e-3, 1.0))
        samples[i] = geo(v)
    ci[pid] = {
        "median": round(float(np.median(samples)), 3),
        "lo": round(float(np.percentile(samples, 2.5)), 3),
        "hi": round(float(np.percentile(samples, 97.5)), 3),
    }

ranking = sorted(pids, key=lambda p: score[p], reverse=True)

# ----------------------------------------------------------------------------
# Charts
# ----------------------------------------------------------------------------
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


def chart_vote_seat():
    rows = [r for r in elections if r["election_year"] == str(YEAR) and r["party_id"] != "P009"]
    rows.sort(key=lambda r: -int(r["seats"]))
    names = [pname[r["party_id"]] for r in rows]
    vs = [float(r["vote_share_est"]) for r in rows]
    ss = [int(r["seats"]) / HOUSE_SEATS * 100 for r in rows]
    colors = [pcolor[r["party_id"]] for r in rows]
    y = list(range(len(names)))[::-1]
    fig, ax = plt.subplots(figsize=(9, 6))
    for i in range(len(names)):
        ax.plot([vs[i], ss[i]], [y[i], y[i]], color="#bbbbbb", lw=2, zorder=1)
    ax.scatter(vs, y, s=110, c=colors, marker="o", edgecolors="white", linewidths=1.2,
               zorder=3, label="Vote share (%)")
    ax.scatter(ss, y, s=130, c=colors, marker="D", edgecolors="white", linewidths=1.2,
               zorder=3, label="Seat share (%)")
    ax.set_yticks(y)
    ax.set_yticklabels(names)
    ax.set_xlabel("Share of valid votes / seats (%)")
    ax.set_title(f"2021: how votes translated into seats (gap = distortion)")
    ax.legend(loc="lower right", frameon=False)
    ax.set_xlim(0, max(max(vs), max(ss)) * 1.12)
    return save(fig, "vote_seat_dumbbell")


def chart_advantage():
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
    ax.set_ylabel("Advantage ratio  (seat share ÷ vote share)")
    ax.set_title("2021: over- and under-representation (1.0 = perfectly proportional)")
    ax.set_ylim(0, max(vals) * 1.18)
    return save(fig, "advantage_ratio")


def chart_ceagi():
    names = [pname[p] for p in ranking]
    vals = [score[p] for p in ranking]
    lo = [ci[p]["lo"] for p in ranking]
    hi = [ci[p]["hi"] for p in ranking]
    err_lo = [max(0.0, vals[i] - lo[i]) for i in range(len(vals))]
    err_hi = [max(0.0, hi[i] - vals[i]) for i in range(len(vals))]
    colors = [pcolor[p] for p in ranking]
    fig, ax = plt.subplots(figsize=(9, 5.5))
    ax.bar(names, vals, yerr=[err_lo, err_hi], capsize=4, color=colors, alpha=0.9,
           error_kw=dict(ecolor="#333333", lw=1))
    for i, v in enumerate(vals):
        ax.text(i, v + 0.01, f"{v:.3f}", ha="center", va="bottom", fontsize=9)
    ax.set_ylabel("CEAGI score (0–1)")
    ax.set_title("Party accountability score (CEAGI) with 95% credible interval")
    ax.set_ylim(0, max(hi) * 1.15)
    return save(fig, "ceagi_ranking")


def chart_credibility_gap():
    names = [pname[p] for p in pids]
    c = [sub[p]["C"] for p in pids]
    d = [sub[p]["D"] for p in pids]
    x = np.arange(len(names))
    w = 0.38
    fig, ax = plt.subplots(figsize=(9, 5.5))
    ax.bar(x - w / 2, c, w, color="#1f77b4", label="Claim credibility (C)", alpha=0.9)
    ax.bar(x + w / 2, d, w, color="#d62728", label="Delivery rate (D)", alpha=0.9)
    ax.set_xticks(x)
    ax.set_xticklabels(names)
    ax.set_ylabel("Score (0–1)")
    ax.set_title("Credibility gap: how well parties say vs. how well they deliver")
    ax.legend(frameon=False)
    ax.set_ylim(0, 1.05)
    return save(fig, "credibility_gap")


def chart_composition():
    rows = [r for r in elections if r["election_year"] == str(YEAR) and r["party_id"] != "P009"]
    rows.sort(key=lambda r: -int(r["seats"]))
    names = [pname[r["party_id"]] for r in rows]
    votes = [float(r["vote_share_est"]) for r in rows]
    seats = [int(r["seats"]) for r in rows]
    colors = [pcolor[r["party_id"]] for r in rows]
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 4.6), sharex=True)
    left = 0.0
    for n, v, c in zip(names, votes, colors):
        ax1.barh(0, v, left=left, color=c, edgecolor="white", height=0.55)
        if v >= 4.5:
            ax1.text(left + v / 2, 0, f"{n}\n{v:.1f}%", ha="center", va="center",
                     fontsize=7.5, color="white")
        left += v
    ax1.set_xlim(0, 100); ax1.set_yticks([])
    ax1.set_title("Vote share (%, estimates — verify against official results)", fontsize=10)
    left = 0.0
    for n, s, c in zip(names, seats, colors):
        ax2.barh(0, s, left=left, color=c, edgecolor="white", height=0.55)
        if s >= 15:
            ax2.text(left + s / 2, 0, f"{n}\n{s}", ha="center", va="center",
                     fontsize=7.5, color="white")
        left += s
    ax2.set_xlim(0, 395); ax2.set_yticks([])
    ax2.set_title("Seats (395 total)", fontsize=10)
    ax2.set_xlabel("Seats")
    fig.suptitle("2021: from votes to seats — same colour, different slice", y=1.04)
    fig.tight_layout()
    return save(fig, "seat_composition")


def chart_bias_reliability():
    xs = [float(s["reliability"]) for s in sources]
    ys = [float(s["bias_lean"]) for s in sources]
    ids = [s["source_id"] for s in sources]
    labels = [f"{s['source_id']} — {s['author_org']}" for s in sources]
    fig, ax = plt.subplots(figsize=(9, 6))
    ax.scatter(xs, ys, s=90, c="#b32424", alpha=0.85, zorder=3)
    for i, sid in enumerate(ids):
        ax.annotate(sid, (xs[i], ys[i]), textcoords="offset points",
                    xytext=(6, 6), fontsize=9, color="#333333")
    ax.set_xlabel("Reliability (1 = weak … 5 = official)")
    ax.set_ylabel("Political lean  (−2 left/conservative … +2 right/liberal)")
    ax.set_title("Source map: reliability vs. lean (kept separate)")
    ax.set_xlim(2.5, 5.5)
    ax.set_ylim(-2.6, 2.6)
    ax.axhline(0, color="#bbbbbb", lw=1)
    ax.axvline(3, color="#bbbbbb", lw=1)
    return save(fig, "bias_reliability")


def chart_trajectory():
    years = [2011, 2016, 2021]
    main = ["P001", "P002", "P003", "P004", "P005"]
    fig, ax = plt.subplots(figsize=(9, 6))
    for pid in main:
        ys = []
        for yr in years:
            r = next((r for r in elections
                      if r["party_id"] == pid and int(r["election_year"]) == yr), None)
            ys.append(int(r["seats"]) / HOUSE_SEATS * 100 if r else None)
        ax.plot(years, ys, marker="o", lw=2.2, color=pcolor[pid], label=pname[pid])
    ax.set_xticks(years)
    ax.set_ylabel("Seat share (%)")
    ax.set_title("Seat-share trajectory, 2011–2021")
    ax.legend(frameon=False, ncol=2)
    ax.set_ylim(0, 35)
    return save(fig, "trajectory")


def chart_heatmap():
    domains = ["Economy", "Employment", "Social", "Housing", "Governance",
               "Agriculture", "Administration", "Rural"]
    active = [pid for pid in pids if any(r["party_id"] == pid for r in promises)]
    mat = np.zeros((len(active), len(domains)))
    text = [["" for _ in domains] for _ in active]
    for i, pid in enumerate(active):
        for j, dom in enumerate(domains):
            rs = [r for r in promises if r["party_id"] == pid and r["domain"] == dom]
            if rs:
                # most recent / most severe status dominates
                val = min(STATUS_NUM[r["status"]] for r in rs)
                mat[i, j] = val
                text[i][j] = rs[0]["status"][:8].replace("_", " ")
    fig, ax = plt.subplots(figsize=(10, 4.5))
    cmap = plt.cm.RdYlGn
    im = ax.imshow(mat, cmap=cmap, vmin=0, vmax=1, aspect="auto")
    ax.set_xticks(range(len(domains)))
    ax.set_xticklabels(domains, rotation=35, ha="right")
    ax.set_yticks(range(len(active)))
    ax.set_yticklabels([pname[p] for p in active])
    for i in range(len(active)):
        for j in range(len(domains)):
            if text[i][j]:
                ax.text(j, i, text[i][j], ha="center", va="center",
                        fontsize=7.5, color="#111111")
    ax.set_title("Promise ledger: status by party and domain (green = delivered, red = failed)")
    fig.colorbar(im, ax=ax, fraction=0.03, label="0 = failed … 1 = fulfilled")
    return save(fig, "promise_ledger_heatmap")


charts = [
    chart_vote_seat(),
    chart_advantage(),
    chart_ceagi(),
    chart_credibility_gap(),
    chart_composition(),
    chart_bias_reliability(),
    chart_trajectory(),
    chart_heatmap(),
]

# ----------------------------------------------------------------------------
# Outputs: CSVs / JSON
# ----------------------------------------------------------------------------
with open(OUT / "ceagi_scores.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["rank", "party_id", "party", "score", "ci_lo", "ci_hi",
                "D_delivery", "C_credibility", "E_efficiency", "G_governance",
                "L_leadership", "M_coherence"])
    for i, pid in enumerate(ranking, 1):
        w.writerow([i, pid, pname[pid], score[pid], ci[pid]["lo"], ci[pid]["hi"],
                    sub[pid]["D"], sub[pid]["C"], sub[pid]["E"], sub[pid]["G"],
                    sub[pid]["L"], sub[pid]["M"]])

with open(OUT / "ceagi_scores.json", "w", encoding="utf-8") as f:
    json.dump({
        "weights": WEIGHTS,
        "parties": [
            {"party_id": p, "name": pname[p], "score": score[p],
             "ci": ci[p], "subindices": sub[p],
             "advantage_ratio": round(Aratio.get(p, 1.0), 3)}
            for p in ranking
        ],
    }, f, indent=2, ensure_ascii=False)

# ----------------------------------------------------------------------------
# HTML report assembly
# ----------------------------------------------------------------------------
def inline_svg(path):
    txt = path.read_text(encoding="utf-8")
    txt = re.sub(r'<\?xml.*?\?>', '', txt)
    txt = re.sub(r'<!DOCTYPE.*?>', '', txt, flags=re.S)
    return txt


def table(headers, rows, cls=""):
    h = "".join(f"<th>{esc(x)}</th>" for x in headers)
    body = ""
    for r in rows:
        body += "<tr>" + "".join(f"<td>{esc(c)}</td>" for c in r) + "</tr>"
    return f'<table class="{cls}"><thead><tr>{h}</tr></thead><tbody>{body}</tbody></table>'


def status_badge(s):
    col = STATUS_COLOR.get(s, "#888888")
    return f'<span class="badge" style="background:{col}">{esc(s)}</span>'


chart_blocks = "\n".join(
    f'<figure class="chart">{inline_svg(p)}<figcaption>{Path(p).stem.replace("_"," ").title()}</figcaption></figure>'
    for p in charts
)

# Key findings
winner = ranking[0]
runner = ranking[1]
worst = ranking[-1]
over = max((p for p in pids if p in Aratio), key=lambda p: Aratio[p])
under = min((p for p in pids if p in Aratio), key=lambda p: Aratio[p])
gap = max(pids, key=lambda p: sub[p]["C"] - sub[p]["D"])
sub_label = {"D": "delivery", "C": "claim credibility", "E": "electoral efficiency",
             "G": "governance", "L": "leadership", "M": "mandate coherence"}
winner_top = sorted(WEIGHTS, key=lambda k: sub[winner][k], reverse=True)[:2]
winner_str = " and ".join(sub_label[k] for k in winner_top)

findings = f"""
<ul class="findings">
  <li><b>{pname[winner]}</b> tops the CEAGI accountability index at
      <b>{score[winner]:.3f}</b> (95% CI {ci[winner]['lo']:.3f}–{ci[winner]['hi']:.3f}),
      driven mainly by {winner_str}.</li>
  <li><b>{pname[under]}</b> is the most under-represented party in 2021: its seat
      share is only <b>{Aratio[under]:.2f}×</b> its vote share
      (advantage ratio), showing how the PR formula punished its vote geography.</li>
  <li><b>{pname[over]}</b> is the most over-represented (<b>{Aratio[over]:.2f}×</b>).</li>
  <li>The widest <b>credibility gap</b> (claims vs. delivery) belongs to
      <b>{pname[gap]}</b>: it talks at {sub[gap]['C']:.2f} but delivers at {sub[gap]['D']:.2f}.</li>
</ul>
"""

# Flagship claims table (quantified claims only)
flagship_rows = []
for r in claims:
    if r["class"] == "Q":
        flagship_rows.append([
            r["claim_id"], pname[r["party_id"]], r["claim"],
            r["target"] if r["target"] != "FILL" else "—",
            r["deadline"], r["unit"] if r["unit"] != "none" else "—",
            r["verification_score"] + "/5", r["source_ids"],
        ])

# CEAGI table
ceagi_rows = []
for i, pid in enumerate(ranking, 1):
    ceagi_rows.append([
        i, pname[pid], f"{score[pid]:.3f}",
        f"{ci[pid]['lo']:.3f}–{ci[pid]['hi']:.3f}",
        f"{sub[pid]['D']:.2f}", f"{sub[pid]['C']:.2f}", f"{sub[pid]['E']:.2f}",
        f"{sub[pid]['G']:.2f}", f"{sub[pid]['L']:.2f}", f"{sub[pid]['M']:.2f}",
    ])

# Promises table with badges
promise_rows = []
for r in promises:
    promise_rows.append([
        r["promise_id"], pname[r["party_id"]], f"{r['term_start']}–{r['term_end']}",
        r["promise"], r["domain"], status_badge(r["status"]),
        r["outcome_metric"] if r["outcome_metric"] != "FILL" else "—",
        r["source_ids"], r["confidence"],
    ])

# Leaders table
leader_rows = []
for r in leaders:
    leader_rows.append([
        r["leader"], pname[r["party_id"]], r["role"],
        r["achievements"] if r["achievements"] != "FILL" else "—",
        r["ownership"] if r["ownership"] != "FILL" else "—",
        r["ostensible_motive"] if r["ostensible_motive"] != "FILL" else "—",
        r["conflicts"] if r["conflicts"] != "FILL" else "—",
        r["skin_in_game"],
    ])

# Sources table
source_rows = []
for r in sources:
    source_rows.append([
        r["source_id"], r["title"], r["author_org"], r["date"], r["type"],
        r["reliability"], r["bias_label"], r["status"],
    ])

# Timeline
timeline_rows = sorted(timeline, key=lambda r: r["date"])
timeline_html = ""
for r in timeline_rows:
    timeline_html += (
        f'<div class="tl"><span class="tldate">{esc(r["date"])}</span>'
        f'<span class="tlev">{esc(r["event"])}</span>'
        f'<span class="tlactor">{esc(r["actor"])}</span></div>'
    )

html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Morocco — Electoral Claims, Promises &amp; Accountability</title>
<style>
:root {{ --red:#b32424; --ink:#1a1a1a; --muted:#666; --line:#e3e0dc; --bg:#fbfaf8; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; font-family:Georgia,'Times New Roman',serif; color:var(--ink); background:var(--bg); line-height:1.55; }}
.wrap {{ max-width:1080px; margin:0 auto; padding:0 22px 60px; }}
header {{ border-bottom:4px solid var(--red); padding:34px 0 20px; margin-bottom:26px; }}
.kicker {{ font-family:Helvetica,Arial,sans-serif; text-transform:uppercase; letter-spacing:.14em; color:var(--red); font-size:.8rem; font-weight:700; }}
h1 {{ font-size:2.5rem; margin:.25rem 0 .35rem; line-height:1.1; }}
h2 {{ font-family:Helvetica,Arial,sans-serif; font-size:1.25rem; border-bottom:2px solid var(--line); padding-bottom:6px; margin:44px 0 16px; }}
h3 {{ font-family:Helvetica,Arial,sans-serif; font-size:1rem; margin:26px 0 10px; }}
.deck {{ font-size:1.12rem; color:#333; max-width:760px; }}
.meta {{ font-family:Helvetica,Arial,sans-serif; font-size:.8rem; color:var(--muted); margin-top:10px; }}
.banner {{ background:#fdf3f3; border-left:6px solid var(--red); padding:14px 18px; margin:22px 0; font-family:Helvetica,Arial,sans-serif; font-size:.85rem; }}
.banner b {{ color:var(--red); }}
ul.findings {{ font-size:1.02rem; padding-left:20px; }}
ul.findings li {{ margin-bottom:8px; }}
table {{ border-collapse:collapse; width:100%; margin:14px 0; font-family:Helvetica,Arial,sans-serif; font-size:.82rem; background:#fff; }}
th {{ background:#f1eee8; text-align:left; padding:8px 9px; border-bottom:2px solid var(--ink); font-size:.74rem; text-transform:uppercase; letter-spacing:.04em; }}
td {{ padding:7px 9px; border-bottom:1px solid var(--line); vertical-align:top; }}
tr:hover td {{ background:#faf6f0; }}
.badge {{ color:#fff; padding:2px 8px; border-radius:3px; font-size:.72rem; font-weight:600; white-space:nowrap; }}
figure.chart {{ margin:28px 0; text-align:center; }}
figure.chart svg {{ max-width:100%; height:auto; }}
figcaption {{ font-family:Helvetica,Arial,sans-serif; font-size:.78rem; color:var(--muted); margin-top:6px; }}
.tl {{ display:grid; grid-template-columns:110px 1fr 160px; gap:10px; padding:7px 0; border-bottom:1px solid var(--line); font-family:Helvetica,Arial,sans-serif; font-size:.85rem; }}
.tldate {{ font-weight:700; color:var(--red); }}
.tlactor {{ color:var(--muted); }}
.formula {{ font-family:Georgia,serif; font-style:italic; background:#fff; border:1px solid var(--line); padding:10px 14px; margin:10px 0; overflow-x:auto; }}
.small {{ font-family:Helvetica,Arial,sans-serif; font-size:.78rem; color:var(--muted); }}
footer {{ margin-top:50px; padding-top:16px; border-top:3px solid var(--ink); font-family:Helvetica,Arial,sans-serif; font-size:.78rem; color:var(--muted); }}
@media (max-width:760px) {{ h1 {{ font-size:1.8rem; }} .tl {{ grid-template-columns:1fr; }} }}
</style>
</head>
<body>
<div class="wrap">

<header>
  <div class="kicker">Data project · Morocco · Voter guide</div>
  <h1>Electoral Claims, Promises &amp; Accountability</h1>
  <p class="deck">How Moroccan parties campaign, how votes become seats, and which
  parties keep their word — quantified with a reproducible scoring model.</p>
  <div class="meta">Generated from the <code>data/</code> registry · Script:
  <code>scripts/build.py</code> · Model: CEAGI · {len(sources)} sources, {len(claims)} claims,
  {len(promises)} promises tracked.</div>
</header>

<div class="banner">
  <b>DATA PROVENANCE &amp; CAVEATS.</b> Seat counts are from official election
  records and are marked <b>HIGH</b> confidence. <b>Vote shares are estimates</b> (marked
  LOW) and must be replaced with official Interior-Ministry figures before
  publication. Promise/claim/leader rows marked <b>ILLUSTRATIVE</b> are realistic
  placeholders to demonstrate the pipeline — verify against the cited sources and
  fill the <code>FILL</code> cells. Every number maps to a <code>source_id</code> in the audit trail.
</div>

<section>
  <h2>Key findings</h2>
  {findings}
</section>

<section>
  <h2>The killer table — flagship claims vs. reality</h2>
  <p class="small">Quantified (SMART) claims only. “Target” is the party’s own
  number; verification 1–5 follows the credibility rubric (5 = independently audited).</p>
  {table(["Claim", "Party", "Claim", "Target", "Deadline", "Unit", "Verif.", "Sources"], flagship_rows)}
</section>

<section>
  <h2>CEAGI — party accountability score</h2>
  <p>CEAGI = Composite Electoral Accountability &amp; Governance Index. Six
  sub-indices (Delivery D, Claim credibility C, Electoral efficiency E, Governance
  G, Leadership L, Mandate coherence M) are combined with a weighted geometric mean,
  so a party cannot compensate a total failure in one dimension with a strong
  showing in another. 95% credible intervals come from a Monte-Carlo pass.</p>
  <div class="formula">S<sub>p</sub> = ( D<sup>0.25</sup> · C<sup>0.20</sup> · E<sup>0.10</sup> · G<sup>0.20</sup> · L<sup>0.15</sup> · M<sup>0.10</sup> )</div>
  {table(["Rank", "Party", "Score", "95% CI", "D", "C", "E", "G", "L", "M"], ceagi_rows)}
  <p class="small">D from promise fulfilment; C from claim taxonomy × rubric; E from
  2021 seat/vote distortion; G, L, M from the indicators registry (assigned — justify
  with sources before publishing).</p>
</section>

<section>
  <h2>Charts</h2>
  {chart_blocks}
</section>

<section>
  <h2>Promise ledger</h2>
  {table(["ID", "Party", "Term", "Promise", "Domain", "Status", "Outcome", "Sources", "Conf."], promise_rows)}
</section>

<section>
  <h2>Leaders — achievements, ownership, ostensible motives</h2>
  <p class="small">“Skin in the game” = 0–1 judgement of how tightly the leader’s
  personal fortune/career is tied to the promised outcomes (1 = fully exposed).
  Treat ownership &amp; motive cells as claims requiring corroboration.</p>
  {table(["Leader", "Party", "Role", "Achievements", "Ownership", "Ostensible motive", "Conflicts", "Skin"], leader_rows)}
</section>

<section>
  <h2>Sources registry</h2>
  {table(["ID", "Title", "Author/Org", "Date", "Type", "Rel.", "Lean", "Status"], source_rows)}
</section>

<section>
  <h2>Timeline</h2>
  {timeline_html}
</section>

<section>
  <h2>Methodology (short)</h2>
  <p class="small">Sources → Facts/Claims → Synthesis. A claim is “corroborated”
  only with 2+ independent reliable sources. Delivery D = (fulfilled + 0.5·partial)
  / (fulfilled + partial + failed + abandoned). Credibility C = Σ(weight·rubric)/5Σweight,
  with class weights Q=1.0, V=0.4, S=0.1, N=0.2. Electoral efficiency E = 1 − |A−1|/max|A−1|
  where A = seat-share ÷ vote-share. Aggregation uses the weighted geometric mean;
  uncertainty via 8,000 beta samples (σ=0.05 per sub-index). Full derivation and the
  audit trail are in <code>output/audit_trail.md</code> and <code>morocco-elections.org</code>.</p>
</section>

<footer>
  <p>Reproducible data project. Regenerate: <code>python3 scripts/build.py</code>.
  Sources are cited by ID in the audit trail; replace all LOW-confidence and
  ILLUSTRATIVE values with verified figures before editorial use.</p>
</footer>

</div>
</body>
</html>
"""

(OUT / "report.html").write_text(html_doc, encoding="utf-8")

# ----------------------------------------------------------------------------
# Audit trail (Markdown)
# ----------------------------------------------------------------------------
lines = []
lines.append("# Audit trail — Morocco elections data project\n")
lines.append("Every number below is traced to a source ID and confidence level.\n")
lines.append("> **Legend** — HIGH: official/verified · MEDIUM: one solid source · "
             "LOW: estimate to be verified · ILLUSTRATIVE: placeholder for the pipeline.\n")
lines.append("\n## Election results (seats)\n")
lines.append("| Year | Party | Seats | Confidence |")
lines.append("|---|---|---|---|")
for r in elections:
    lines.append(f"| {r['election_year']} | {pname[r['party_id']]} | {r['seats']} | {r['seats_confidence']} |")
lines.append("\n## Vote shares (ESTIMATES — replace with official figures)\n")
lines.append("| Year | Party | Vote share est. | Confidence |")
lines.append("|---|---|---|---|")
for r in elections:
    lines.append(f"| {r['election_year']} | {pname[r['party_id']]} | {r['vote_share_est']}% | {r['vote_confidence']} |")
lines.append("\n## CEAGI scores\n")
lines.append("| Party | Score | CI lo | CI hi | D | C | E | G | L | M |")
lines.append("|---|---|---|---|---|---|---|---|---|---|")
for pid in ranking:
    s = sub[pid]
    lines.append(f"| {pname[pid]} | {score[pid]} | {ci[pid]['lo']} | {ci[pid]['hi']} | "
                 f"{s['D']} | {s['C']} | {s['E']} | {s['G']} | {s['L']} | {s['M']} |")
lines.append("\n## Claims\n")
for r in claims:
    lines.append(f"- {r['claim_id']} [{pname[r['party_id']]}, {r['election_year']}] "
                 f"{r['claim']} — class {r['class']}, verif {r['verification_score']}/5, "
                 f"source {r['source_ids']}, confidence {r['confidence']}.")
lines.append("\n## Promises\n")
for r in promises:
    lines.append(f"- {r['promise_id']} [{pname[r['party_id']]}, {r['term_start']}–{r['term_end']}] "
                 f"{r['promise']} — {r['status']} — {r['outcome_metric']} "
                 f"(source {r['source_ids']}, {r['confidence']}).")
lines.append("\n## Leaders\n")
for r in leaders:
    lines.append(f"- {r['leader']} [{pname[r['party_id']]}] role {r['role']} — "
                 f"skin-in-game {r['skin_in_game']} — source {r['source_ids']}, {r['confidence']}.")
(OUT / "audit_trail.md").write_text("\n".join(lines), encoding="utf-8")

print(f"[ok] Wrote report -> {OUT/'report.html'}")
print(f"[ok] Charts    -> {len(charts)} SVGs in {CHARTS}")
print(f"[ok] Scores    -> ceagi_scores.csv / ceagi_scores.json")
print(f"[ok] Audit     -> audit_trail.md")
print(f"[ok] Top party: {pname[winner]} ({score[winner]:.3f})")
