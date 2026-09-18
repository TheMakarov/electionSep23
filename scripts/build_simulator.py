#!/usr/bin/env python3
"""
Build the interactive seat-allocation simulator (output/seat_simulator.html).

Self-contained HTML+SVG+JS (no internet). Reads data/parties.csv (colours),
data/elections.csv (2021 vote-share prefill) and output/ceagi_scores.json
(CEAGI sub-indices) so the simulator stays in sync with the registry.

Electoral formula (Morocco, Chambre des Représentants, 395 seats):
  - Hare quota (quotient électoral): Q = V / S
  - automatic seats: floor(votes / Q)
  - leftover seats: largest remainders
  - 3% threshold on valid votes
This applies the formula at national level for illustration; the real ballot
allocates 305 local seats per constituency and 90 national-list seats
(60 women / 30 under-40) with the same largest-remainder logic.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = ROOT / "output"

import sys  # noqa: E402
sys.path.insert(0, str(ROOT / "scripts"))
from moroccan_theme import STAR_URI, ZELLIGE_URI, STAR_BADGE_SVG  # noqa: E402

parties = [p for p in csv.DictReader(open(DATA / "parties.csv", newline="", encoding="utf-8"))]
elections = list(csv.DictReader(open(DATA / "elections.csv", newline="", encoding="utf-8")))
try:
    ceagi = json.load(open(OUT / "ceagi_scores.json", encoding="utf-8"))
except FileNotFoundError:
    ceagi = {"parties": []}

# 2021 vote-share prefill (estimates, labelled LOW in the registry)
prefill = {
    r["party_id"]: float(r["vote_share_est"])
    for r in elections if r["election_year"] == "2021" and r["party_id"] != "P009"
}

party_js = [
    {"id": p["party_id"], "name": p["name"], "ar": p.get("name_ar", ""), "color": p["color"]}
    for p in parties
]

ceagi_js = []
for p in ceagi.get("parties", []):
    sub = p.get("subindices", {})
    ceagi_js.append({
        "id": p["party_id"], "name": p["name"], "score": p.get("score"),
        "coverage": p.get("coverage", 0),
        "sub": {k: sub.get(k, {}).get("value") for k in ("D", "C", "E", "G", "L", "M")},
    })

TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Morocco 2026 — Seat Allocation Simulator & CEAGI</title>
<style>
:root { --red:#c1272d; --red-deep:#8e1b20; --green:#006233; --gold:#c8a24a;
        --sand:#f6efe0; --ink:#1c1a17; --muted:#6d6459; --line:#e4dbc9; --bg:#fbf8f1; }
* { box-sizing:border-box; }
body { margin:0; font-family:Georgia,serif; color:var(--ink); background:var(--bg); line-height:1.5; }
body::before { content:""; display:block; height:5px;
  background:linear-gradient(90deg,var(--green) 0 34%,var(--gold) 34% 66%,var(--red) 66% 100%); }
a { color:var(--green); }
a:hover { color:var(--red); }
.masthead { position:relative; overflow:hidden; color:#fff;
  background:radial-gradient(125% 150% at 86% 4%,#d8434a 0%,var(--red) 46%,var(--red-deep) 100%);
  border-bottom:5px solid var(--green); box-shadow:0 3px 0 var(--gold); }
.masthead-bg { position:absolute; inset:0; pointer-events:none;
  background-image:url("__ZELLIGE__"); background-size:64px 64px; opacity:.95; }
.masthead-inner { position:relative; max-width:1120px; margin:0 auto; padding:26px 20px 24px;
  display:flex; gap:22px; align-items:center; }
.flagmark { flex:0 0 auto; width:84px; height:84px;
  filter:drop-shadow(0 6px 13px rgba(0,0,0,.36)); }
.flagmark svg { width:100%; height:100%; display:block; }
.masthead-copy { min-width:0; }
.kicker { font-family:Helvetica,Arial,sans-serif; text-transform:uppercase; letter-spacing:.18em;
  color:var(--gold); font-size:.72rem; font-weight:700; }
.masthead h1 { font-size:2.05rem; margin:.25rem 0 .35rem; line-height:1.1; color:#fffdf7;
  text-shadow:0 2px 0 rgba(0,0,0,.2); }
.masthead .deck { color:#fbe6e6; max-width:820px; margin:.1rem 0 0; }
.wrap { max-width:1120px; margin:0 auto; padding:0 20px 70px; }
h2 { font-family:Helvetica,Arial,sans-serif; font-size:1.15rem; color:var(--green);
  border-bottom:2px solid var(--green); padding-bottom:6px; margin:38px 0 14px;
  display:flex; align-items:center; gap:.55em; }
h2::before { content:""; flex:0 0 auto; width:1.02em; height:1.02em;
  background:url("__STAR__") center/contain no-repeat; }
.note { font-family:Helvetica,Arial,sans-serif; font-size:.78rem; color:var(--muted); margin:6px 0 16px; }
.formula { font-family:Georgia,serif; background:#fff; border:1px solid var(--line);
  border-left:4px solid var(--green); padding:10px 14px; margin:12px 0; }
.formula code { font-family:ui-monospace,Menlo,monospace; }
.grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(230px,1fr)); gap:10px 18px; margin:12px 0; }
.party-input { display:grid; grid-template-columns:20px 1fr 110px; gap:8px; align-items:center; font-family:Helvetica,Arial,sans-serif; font-size:.82rem; }
.swatch { width:16px; height:16px; border-radius:50%; border:1px solid rgba(0,0,0,.2); }
.party-input input[type=number] { width:110px; padding:4px 6px; border:1px solid var(--line); border-radius:4px; }
.party-input input[type=range] { width:100%; }
.toolbar { margin:14px 0; }
button { font-family:Helvetica,Arial,sans-serif; font-size:.8rem; padding:6px 12px; margin-right:8px;
  border:1px solid var(--line); background:#fff; border-radius:4px; cursor:pointer; }
button:hover { background:var(--sand); }
#hemicycle { text-align:center; margin:18px 0; }
#hemicycle svg { max-width:100%; height:auto; }
table { border-collapse:collapse; width:100%; margin:14px 0; font-family:Helvetica,Arial,sans-serif; font-size:.8rem; background:#fff; }
th { background:var(--sand); text-align:left; padding:7px 8px; border-bottom:2px solid var(--green);
  font-size:.7rem; text-transform:uppercase; letter-spacing:.04em; color:#4a4132; }
td { padding:6px 8px; border-bottom:1px solid var(--line); vertical-align:top; }
tr:hover td { background:#fdf9f0; }
.over { color:var(--red); font-weight:700; }
.under { color:#1a6a8a; }
.chip { display:inline-block; width:11px; height:11px; border-radius:50%; margin-right:5px; vertical-align:middle; }
.interpret p { margin:8px 0; font-size:.92rem; }
.interpret .big { font-size:1.05rem; }
#ceagi-bars svg { max-width:100%; height:auto; }
footer { margin-top:46px; padding-top:14px; border-top:4px solid var(--green);
  box-shadow:inset 0 2px 0 var(--gold);
  font-family:Helvetica,Arial,sans-serif; font-size:.74rem; color:var(--muted); }
@media (max-width:700px){
  .masthead-inner { flex-direction:column; align-items:flex-start; gap:14px; }
  .flagmark { width:66px; height:66px; }
  .masthead h1 { font-size:1.6rem; }
  .party-input { grid-template-columns:16px 1fr 90px; }
  .party-input input[type=number]{ width:90px; }
}
</style>
</head>
<body>
<header class="masthead">
  <div class="masthead-bg" aria-hidden="true"></div>
  <div class="masthead-inner">
    <div class="flagmark">__STARBADGE__</div>
    <div class="masthead-copy">
      <div class="kicker">Interactive &middot; Chambre des Repr&eacute;sentants</div>
      <h1>Seat Allocation Simulator &amp; CEAGI reference</h1>
      <p class="deck">How votes become seats under Morocco's proportional formula, and how that
      compares with each party's accountability score (CEAGI).</p>
    </div>
  </div>
</header>

<div class="wrap">

<div class="formula">
  <b>Quotient électoral (Hare) :</b> <code>Q = V / S</code> &nbsp;&nbsp;where
  <code>V</code> = valid votes of lists above the <b>3% threshold</b>, <code>S</code> = seats (395).
  Each list gets <code>floor(votes / Q)</code> automatic seats; the
  remaining seats go to the <b>largest remainders</b>. Majority = <b>198</b>.
  <span class="note" style="margin:0">This applies the formula at national level for
  illustration — the real ballot allocates 305 local seats per constituency plus 90
  national-list seats (60 women / 30 under-40) with the same largest-remainder logic.</span>
</div>

<section>
  <h2>1. Votes in</h2>
  <div class="toolbar">
    <button onclick="prefill2021()">Prefill 2021 (est.)</button>
    <button onclick="clearVotes()">Clear</button>
    <span class="note">Votes are in thousands; edit a number or drag its slider.</span>
  </div>
  <div class="grid" id="inputs"></div>
</section>

<section>
  <h2>2. Seats out — hemicycle</h2>
  <div id="hemicycle"></div>
</section>

<section>
  <h2>3. Interpretation</h2>
  <div id="results-table"></div>
  <div class="interpret" id="interpret"></div>
</section>

<section>
  <h2>4. CEAGI reference — accountability vs. seats</h2>
  <p class="note">CEAGI combines six sub-indices (D delivery of fulfilled promises, C claim
  credibility, E electoral efficiency, G governance, L leadership, M mandate coherence).
  <b>Only parties with all six are ranked</b>; the rest show a provisional, unranked
  average. Values marked <span class="under">—</span> are <code>n/a</code> (no evidence yet:
  D is missing for most parties because their term's promises are still open).</p>
  <div id="ceagi-table"></div>
  <div id="ceagi-bars"></div>
</section>

<footer>
  <p>Generated from <code>data/</code> and <code>output/ceagi_scores.json</code> by
  <code>scripts/build_simulator.py</code>. Electoral formula: Hare quota + largest remainder,
  3% threshold (395 seats). Vote prefill = 2021 estimates (marked LOW in the registry).</p>
</footer>

</div>

<script>
const PARTIES = __PARTIES__;
const CEAGI = __CEAGI__;
const PREFILL = __PREFILL__;
const TOTAL_SEATS = 395;
const MAJORITY = Math.floor(TOTAL_SEATS / 2) + 1; // 198
const THRESHOLD_PCT = 3.0;
const DIMS = ["D","C","E","G","L","M"];
const DIM_LABEL = {D:"Delivery", C:"Claims", E:"Electoral", G:"Governance", L:"Leadership", M:"Mandate"};

const state = { votes: {} };

function partyById(id){ return PARTIES.find(p => p.id === id); }

/* ---------------- seat allocation (Hare + largest remainder) ---------------- */
function allocate(votes) {
  const total = Object.values(votes).reduce((a,b)=>a+b,0) || 0;
  const eligible = Object.entries(votes)
    .filter(([id,v]) => total > 0 && (v / total * 100) >= THRESHOLD_PCT);
  // quota over above-threshold votes only (below-threshold votes are wasted)
  const vEff = eligible.reduce((acc,[id,v]) => acc + v, 0);
  const quota = vEff / TOTAL_SEATS;
  const seats = {}; PARTIES.forEach(p => seats[p.id] = 0);
  const remainders = [];
  let autoTotal = 0;
  for (const [id, v] of eligible) {
    const a = Math.floor(v / quota);
    seats[id] = a; autoTotal += a;
    remainders.push([id, v - a * quota]);
  }
  let leftover = TOTAL_SEATS - autoTotal;
  remainders.sort((x,y) => y[1] - x[1]);
  for (let i = 0; i < leftover && i < remainders.length; i++) seats[remainders[i][0]] += 1;
  return { seats, total, quota, eligible: new Set(eligible.map(e => e[0])) };
}

/* ---------------- hemicycle layout (concentric arcs) ---------------- */
function hemicycleLayout(n) {
  const rows = 8, Rmax = 300, Rmin = 112;
  const radii = Array.from({length: rows}, (_, i) => Rmax - (Rmax - Rmin) * i / (rows - 1));
  const wsum = radii.reduce((a,b)=>a+b,0);
  const counts = radii.map(r => Math.round(n * r / wsum));
  let diff = n - counts.reduce((a,b)=>a+b,0);
  for (let i = 0; diff !== 0; i++) {
    const j = i % rows, d = diff > 0 ? 1 : -1;
    if (counts[j] + d >= 1) { counts[j] += d; diff -= d; }
  }
  const pts = [];
  for (let i = 0; i < rows; i++) {
    const r = radii[i], k = counts[i];
    for (let j = 0; j < k; j++) {
      const th = (k === 1) ? Math.PI / 2 : Math.PI - j * Math.PI / (k - 1);
      pts.push({ x: r * Math.cos(th), y: -r * Math.sin(th), row: i });
    }
  }
  return pts;
}

/* ---------------- rendering ---------------- */
function seatColorList(seats) {
  const order = PARTIES.slice().sort((a,b) => (seats[b.id]||0) - (seats[a.id]||0));
  const colors = [];
  for (const p of order) { for (let i = 0; i < (seats[p.id]||0); i++) colors.push(p.color); }
  while (colors.length < TOTAL_SEATS) colors.push("#e0ddd6");
  return colors;
}

function renderHemicycle(seats) {
  const pts = hemicycleLayout(TOTAL_SEATS);
  const colors = seatColorList(seats);
  const cx = 320, cy = 312, r = 4.0;
  let dots = "";
  pts.forEach((p, i) => {
    dots += `<circle cx="${(cx + p.x).toFixed(1)}" cy="${(cy + p.y).toFixed(1)}" r="${r}" fill="${colors[i]}" stroke="white" stroke-width="0.6"/>`;
  });
  // majority line
  let line = "";
  for (let k = 1; k < TOTAL_SEATS; k++) {
    // mark the majority boundary visually at the seat index MAJORITY
    if (k === MAJORITY) { const p = pts[k-1]; line = `<line x1="${(cx+p.x).toFixed(1)}" y1="0" x2="${(cx+p.x).toFixed(1)}" y2="${cy+p.y}" stroke="#111" stroke-width="1.4" stroke-dasharray="3 3"/>`; }
  }
  document.getElementById("hemicycle").innerHTML =
    `<svg viewBox="0 0 640 340" width="700" role="img" aria-label="Parliament hemicycle">` +
    `<rect x="0" y="0" width="640" height="340" fill="#fbfaf8"/>` +
    line + dots + `</svg>`;
}

function renderTable(alloc) {
  const rows = PARTIES.map(p => {
    const v = state.votes[p.id] || 0;
    const vs = alloc.total ? v / alloc.total : 0;
    const ss = (alloc.seats[p.id] || 0) / TOTAL_SEATS;
    const adv = (vs > 0 && ss > 0) ? ss / vs : (ss > 0 ? Infinity : (vs > 0 ? 0 : 1));
    const advTxt = adv === Infinity ? "—" : adv.toFixed(2);
    const cls = (!isFinite(adv)) ? "" : (adv > 1.02 ? "over" : adv < 0.98 ? "under" : "");
    return `<tr><td><span class="chip" style="background:${p.color}"></span>${p.name}</td>` +
      `<td>${(v/1000).toFixed(1)} M</td><td>${(vs*100).toFixed(1)}%</td>` +
      `<td>${alloc.seats[p.id]}</td><td>${(ss*100).toFixed(1)}%</td>` +
      `<td class="${cls}">${advTxt}</td></tr>`;
  }).join("");
  document.getElementById("results-table").innerHTML =
    `<table><thead><tr><th>Party</th><th>Votes</th><th>Vote %</th><th>Seats</th><th>Seat %</th><th>Advantage ratio</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function renderInterpretation(alloc) {
  const entries = PARTIES.map(p => ({ p, v: state.votes[p.id]||0, s: alloc.seats[p.id]||0 }))
    .filter(e => e.v > 0 || e.s > 0);
  const total = alloc.total;
  const voteShare = id => total ? (state.votes[id]||0) / total : 0;
  const seatShare = id => (alloc.seats[id]||0) / TOTAL_SEATS;

  let gallagher = 0;
  PARTIES.forEach(p => { const d = voteShare(p.id) - seatShare(p.id); gallagher += d*d; });
  gallagher = Math.sqrt(0.5 * gallagher) * 100;

  let nv = 0, ns = 0;
  PARTIES.forEach(p => { nv += voteShare(p.id)**2; ns += seatShare(p.id)**2; });
  const enpVotes = nv ? 1/nv : 0, enpSeats = ns ? 1/ns : 0;

  const bySeats = entries.slice().sort((a,b) => b.s - a.s);
  const largest = bySeats[0];
  const over = [...entries].sort((a,b) => (b.s/TOTAL_SEATS)/(b.v>0?b.v/total:1) - (a.s/TOTAL_SEATS)/(a.v>0?a.v/total:1))[0];
  const under = [...entries].sort((a,b) => (a.s/TOTAL_SEATS)/(a.v>0?a.v/total:1) - (b.s/TOTAL_SEATS)/(b.v>0?b.v/total:1))[0];

  // greedy coalition to reach 198
  let coal = [], sum = 0, coalNames = [];
  for (const e of bySeats) { if (sum >= MAJORITY) break; sum += e.s; coalNames.push(e.p.name); }
  coal = coalNames;

  const html = `
    <p class="big">Largest party: <b>${largest.p.name}</b> with <b>${largest.s}</b> seats
    (${(largest.s/TOTAL_SEATS*100).toFixed(1)}%) — ${largest.s >= MAJORITY ? "reaches the 198-seat majority alone." : "short of the 198-seat majority."}</p>
    <p>A minimal winning bloc needs ${MAJORITY} seats. Counting from the largest, a coalition of
    <b>${coal.length}</b> largest groups (${coal.join(", ")}) would just cross it.</p>
    <p><b>${over.p.name}</b> is the most over-represented (advantage ratio
    ${((over.s/TOTAL_SEATS)/(over.v>0?over.v/total:1)).toFixed(2)}) and <b>${under.p.name}</b>
    the most under-represented — the largest-remainder method does not convert votes to seats perfectly.</p>
    <p class="note">Distortion: Gallagher index <b>${gallagher.toFixed(2)}</b> · Effective number of parties
    <b>${enpVotes.toFixed(2)}</b> (votes) / <b>${enpSeats.toFixed(2)}</b> (seats).</p>`;
  document.getElementById("interpret").innerHTML = html;
}

function renderCEAGI() {
  const rows = CEAGI.map(c => {
    const cells = DIMS.map(d => {
      const v = c.sub[d];
      return `<td>${(v === null || v === undefined) ? '<span class="under">—</span>' : v.toFixed(2)}</td>`;
    }).join("");
    const score = (c.score === null || c.score === undefined) ? `<span class="under">provisional ${c.coverage}/6</span>` : `<b>${c.score.toFixed(3)}</b>`;
    return `<tr><td><span class="chip" style="background:${(partyById(c.id)||{}).color||'#999'}"></span>${c.name}</td>` +
      `<td>${score}</td>${cells}</tr>`;
  }).join("");
  document.getElementById("ceagi-table").innerHTML =
    `<table><thead><tr><th>Party</th><th>CEAGI</th><th>D</th><th>C</th><th>E</th><th>G</th><th>L</th><th>M</th></tr></thead><tbody>${rows}</tbody></table>`;

  // bar chart
  const bw = 620, bh = CEAGI.length * 40 + 30, leftPad = 90;
  let bars = "";
  CEAGI.forEach((c, i) => {
    const y = 24 + i * 40;
    bars += `<text x="${leftPad-6}" y="${y+15}" text-anchor="end" font-family="Helvetica" font-size="12">${c.name}</text>`;
    DIMS.forEach((d, j) => {
      const v = c.sub[d];
      if (v === null || v === undefined) return;
      const x = leftPad + j * ((bw-leftPad)/DIMS.length);
      const w = (bw-leftPad)/DIMS.length * 0.8;
      const h = v * 28;
      bars += `<rect x="${x.toFixed(0)}" y="${y + (28-h)}" width="${w.toFixed(0)}" height="${h.toFixed(0)}" fill="#${d==='C'?'d62728':d==='D'?'2e7d32':'8a8a8a'}" opacity="0.85"/>`;
    });
  });
  const legend = DIMS.map(d => `<tspan fill="#${d==='C'?'d62728':d==='D'?'2e7d32':'555'}">${DIM_LABEL[d]}(${d})  </tspan>`).join("");
  document.getElementById("ceagi-bars").innerHTML =
    `<svg viewBox="0 0 ${bw} ${bh}" width="700" role="img" aria-label="CEAGI sub-indices">` +
    `<rect width="${bw}" height="${bh}" fill="#fff"/>` +
    `<text x="${leftPad}" y="${bh-4}" font-family="Helvetica" font-size="11" fill="#555">${legend} · green=Delivery, red=Claims; blank = n/a</text>` +
    bars + `</svg>`;
}

function render() {
  const alloc = allocate(state.votes);
  renderHemicycle(alloc.seats);
  renderTable(alloc);
  renderInterpretation(alloc);
}

/* ---------------- inputs ---------------- */
function buildInputs() {
  const grid = document.getElementById("inputs");
  grid.innerHTML = PARTIES.map(p => {
    const def = (PREFILL[p.id] !== undefined ? PREFILL[p.id] : 1) * 1000; // thousands
    return `<div class="party-input">
      <span class="swatch" style="background:${p.color}"></span>
      <span>${p.name} <span style="color:#999">${p.ar ? "· "+p.ar : ""}</span></span>
      <input type="number" id="n-${p.id}" value="${def}" min="0" step="100" oninput="syncFromNum('${p.id}')">
      <span></span>
      <input type="range" id="s-${p.id}" min="0" max="5000" step="10" value="${Math.min(def,5000)}" oninput="syncFromSlider('${p.id}')" style="grid-column:2/4">
    </div>`;
  }).join("");
  PARTIES.forEach(p => state.votes[p.id] = (PREFILL[p.id] !== undefined ? PREFILL[p.id] : 1) * 1000);
}

function syncFromNum(id) {
  const v = Number(document.getElementById("n-" + id).value) || 0;
  state.votes[id] = v;
  const s = document.getElementById("s-" + id);
  s.max = Math.max(s.max, v); s.value = Math.min(v, s.max);
  render();
}
function syncFromSlider(id) {
  const v = Number(document.getElementById("s-" + id).value) || 0;
  state.votes[id] = v;
  document.getElementById("n-" + id).value = v;
  render();
}
function prefill2021() {
  PARTIES.forEach(p => {
    const v = (PREFILL[p.id] !== undefined ? PREFILL[p.id] : 1) * 1000;
    state.votes[p.id] = v;
    document.getElementById("n-" + p.id).value = v;
    document.getElementById("s-" + p.id).value = Math.min(v, document.getElementById("s-" + p.id).max);
  });
  render();
}
function clearVotes() {
  PARTIES.forEach(p => {
    state.votes[p.id] = 0;
    document.getElementById("n-" + p.id).value = 0;
    document.getElementById("s-" + p.id).value = 0;
  });
  render();
}

buildInputs();
renderCEAGI();
render();
</script>
</body>
</html>
"""

html = (TEMPLATE
        .replace("__PARTIES__", json.dumps(party_js, ensure_ascii=False))
        .replace("__CEAGI__", json.dumps(ceagi_js, ensure_ascii=False))
        .replace("__PREFILL__", json.dumps(prefill, ensure_ascii=False))
        .replace("__STARBADGE__", STAR_BADGE_SVG)
        .replace("__STAR__", STAR_URI)
        .replace("__ZELLIGE__", ZELLIGE_URI))

OUT.mkdir(parents=True, exist_ok=True)
(OUT / "seat_simulator.html").write_text(html, encoding="utf-8")
print(f"[ok] Simulator -> {OUT / 'seat_simulator.html'}")
print(f"[ok] {len(party_js)} parties, {len(ceagi_js)} CEAGI rows embedded")
