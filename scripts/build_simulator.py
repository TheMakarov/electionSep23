#!/usr/bin/env python3
"""
Build the interactive seat-allocation simulator (output/seat_simulator.html).

Self-contained HTML + inline SVG map + JS (no internet). Reads:
  - data/parties.csv        party names and colours
  - data/elections.csv      2021 vote-share prefill
  - data/constituencies.csv the 92 local + 12 regional constituencies and their
                            official seat counts
  - data/morocco_map.json   simplified province / region geometry (SVG paths)
  - data/legal_basis.csv    the law articles quoted on the page

Electoral formula (Morocco, Chambre des Représentants, 395 seats):
  - proportional representation, largest remainder (plus fort reste)
  - legal quotient (art. 84): registered voters in the constituency / its seats
  - no electoral threshold (the 3%/6% threshold was removed by the 2021 reform)
  - 305 seats in local constituencies, 90 in regional constituencies (art. 1)
  - loi organique n° 27.11, consolidated 29 Jan 2026 (amended by 53.25, 04.21)

The page lets you edit the party list and enter votes per constituency; seats are
then allocated constituency by constituency, which is how the real count works.
The quotient here divides the votes you enter (not registered voters), because
the page has no register — the difference is stated on the page.
"""
from __future__ import annotations

import base64
import csv
import json
import sys
from html import escape as _esc
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = ROOT / "output"

sys.path.insert(0, str(ROOT / "scripts"))
from moroccan_theme import STAR_URI, ZELLIGE_URI, STAR_BADGE_SVG, FAVICON_URI, apply_tokens  # noqa: E402
from og_image import render_card  # noqa: E402

# Site base URL for social-share meta tags (og:url / og:image). Falls back to
# the local `make serve` address; set config.json's site_base_url for deployment.
_SITE_BASE = "http://localhost:8000"
try:
    _cfg = json.load(open(ROOT / "config.json", encoding="utf-8"))
    _SITE_BASE = (_cfg.get("site_base_url") or _SITE_BASE).rstrip("/")
except (FileNotFoundError, ValueError):
    pass


def load(name):
    return list(csv.DictReader(open(DATA / name, newline="", encoding="utf-8")))


parties = load("parties.csv")
elections = load("elections.csv")
legal_basis = load("legal_basis.csv")
constituencies = load("constituencies.csv")
sources = {r["source_id"]: r for r in load("sources.csv")}
map_data = json.load(open(DATA / "morocco_map.json", encoding="utf-8"))

# Party logos (data/logos/<party_id>.{svg,png,gif,jpg}) inlined as data URIs so
# the page stays self-contained; a party without a logo falls back to its colour.
_MIME = {".svg": "image/svg+xml", ".png": "image/png", ".gif": "image/gif",
         ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
logos_js = {}
for _p in parties:
    for _ext, _mime in _MIME.items():
        _fp = DATA / "logos" / (_p["party_id"] + _ext)
        if _fp.exists():
            logos_js[_p["party_id"]] = (
                "data:" + _mime + ";base64,"
                + base64.b64encode(_fp.read_bytes()).decode("ascii"))
            break

# 2021 vote-share prefill (estimates, labelled LOW in the registry), in percent.
prefill = {r["party_id"]: float(r["vote_share_est"])
           for r in elections
           if r["election_year"] == "2021" and r["party_id"] != "P009"
           and r["vote_share_est"] not in ("", "FILL")}

# Normalise the vote-share prefill so the displayed shares always sum to exactly
# 100.00% (rounding each row to two decimals on its own drifts the total, e.g.
# to 99.99%). Largest-remainder keeps every value within 0.01pp of the source.
_shares = [prefill.get(p["party_id"], 0.0) for p in parties]
if sum(_shares) > 0:
    _scaled = [s / sum(_shares) * 100 for s in _shares]
    _hundredths = [int(round(s * 100)) for s in _scaled]
    _diff = 10000 - sum(_hundredths)
    _order = sorted(range(len(_scaled)),
                    key=lambda i: _scaled[i] * 100 - _hundredths[i], reverse=True)
    for _k in range(abs(_diff)):
        _i = _order[_k % len(_order)]
        _hundredths[_i] += 1 if _diff > 0 else -1
    _norm_shares = [h / 100 for h in _hundredths]
else:
    _norm_shares = [0.0] * len(parties)

party_js = [{"id": p["party_id"], "name": p["name"], "ar": p.get("name_ar", ""),
             "color": p["color"], "share": _norm_shares[i]}
            for i, p in enumerate(parties)]

const_js = [{"id": c["constituency_id"], "level": c["level"], "name": c["name"],
             "prefecture": c["prefecture"], "unit": c["map_unit"],
             "region": c["region"], "seats": int(c["seats"])}
            for c in constituencies]

# 2021 real results, parsed from the official per-constituency PDF
# (data/results_2021.csv). Party abbreviations used in the source -> party_id.
ABBR = {"RNI": "P001", "PAM": "P002", "PI": "P003", "PJD": "P004",
        "USFP": "P005", "MP": "P006", "PPS": "P007", "UC": "P008",
        "MDS": "P010", "FFD": "P011", "CNI": "P012", "PSU": "P013"}
# Only the 12 parties that won a seat in 2021 are modelled; the micro-lists
# (AFG, NEO, PEDD, ...) won none and are NOT lumped together, otherwise their
# summed votes would fake a seat bloc in the largest-remainder step.
results_js = {}
splinter_votes = 0
try:
    for r in csv.DictReader(open(DATA / "results_2021.csv", newline="", encoding="utf-8")):
        pid = ABBR.get(r["party"])
        if pid is None:
            splinter_votes += int(r["votes"])
            continue
        u = r["map_unit"]
        results_js.setdefault(u, {})
        results_js[u][pid] = results_js[u].get(pid, 0) + int(r["votes"])
except FileNotFoundError:
    results_js = {}

# official 2021 seat totals (local + regional) from the registry
seats2021_js = {r["party_id"]: int(r["seats"]) for r in elections
                if r["election_year"] == "2021" and r["seats"].isdigit()}
TURNOUT_2021 = 50.2  # national participation announced for 8 Sept 2021 (%)


def law_block(lb):
    src = sources.get(lb["source_id"], {})
    url = (src.get("url") or "").strip()
    link = (f' &middot; <a href="{_esc(url)}" target="_blank" rel="noopener">'
            f'official text &#8599;</a>') if url else ""
    return (
        '<figure class="lawquote">'
        f'<blockquote lang="ar" dir="rtl">{_esc(lb["quote_ar"])}</blockquote>'
        f'<p class="lawen">{_esc(lb["quote_en"])}</p>'
        f'<figcaption><span class="lawtag">{_esc(lb["article"])}</span>'
        f'<b>{_esc(lb["instrument"])}</b>{link}'
        f'<br><span class="note" style="margin:0">{_esc(lb["note"])}</span>'
        '</figcaption></figure>')


LAW_HTML = "".join(law_block(lb) for lb in legal_basis)
LOCAL_SEATS = sum(c["seats"] for c in const_js if c["level"] == "local")
REGIONAL_SEATS = sum(c["seats"] for c in const_js if c["level"] == "regional")
N_UNITS = len({c["unit"] for c in const_js if c["level"] == "local"})


def js(payload):
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" type="image/svg+xml" href="__FAVICON__">
__OG_META__
<title>Morocco 2026 — Constituency Seat Simulator</title>
<style>
:root { --red:__RED__; --red-deep:__RED_DEEP__; --green:__GREEN__; --gold:__GOLD__;
        --sand:__SAND__; --ink:__INK__; --muted:__MUTED__; --line:__LINE__; --bg:__BG__; }
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
.masthead-inner { position:relative; max-width:1240px; margin:0 auto; padding:26px 20px 24px;
  display:flex; gap:22px; align-items:center; }
.flagmark { flex:0 0 auto; width:84px; height:84px;
  filter:drop-shadow(0 6px 13px rgba(0,0,0,.36)); }
.flagmark svg { width:100%; height:100%; display:block; }
.masthead-copy { min-width:0; }
.kicker { font-family:Helvetica,Arial,sans-serif; text-transform:uppercase; letter-spacing:.18em;
  color:var(--gold); font-size:.72rem; font-weight:700; }
.masthead h1 { font-size:2.05rem; margin:.25rem 0 .35rem; line-height:1.1; color:#fffdf7;
  text-shadow:0 2px 0 rgba(0,0,0,.2); }
.masthead .deck { color:#fbe6e6; max-width:860px; margin:.1rem 0 0; }
.wrap { max-width:1240px; margin:0 auto; padding:0 20px 70px; }
h2 { font-family:Helvetica,Arial,sans-serif; font-size:1.15rem; color:var(--green);
  border-bottom:2px solid var(--green); padding-bottom:6px; margin:38px 0 14px;
  display:flex; align-items:center; gap:.55em; }
h2::before { content:""; flex:0 0 auto; width:1.02em; height:1.02em;
  background:url("__STAR__") center/contain no-repeat; }
h3 { font-family:Helvetica,Arial,sans-serif; font-size:.98rem; color:var(--red); margin:20px 0 8px; }
.note { font-family:Helvetica,Arial,sans-serif; font-size:.78rem; color:var(--muted); margin:6px 0 16px; }
.formula { font-family:Georgia,serif; background:#fff; border:1px solid var(--line);
  border-left:4px solid var(--green); padding:10px 14px; margin:12px 0; }
.formula code { font-family:ui-monospace,Menlo,monospace; }
.grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(250px,1fr)); gap:10px 18px; margin:12px 0; }
.toolbar { margin:14px 0; }
button { font-family:Helvetica,Arial,sans-serif; font-size:.8rem; padding:6px 12px; margin-right:8px;
  border:1px solid var(--line); background:#fff; border-radius:4px; cursor:pointer; }
button:hover { background:var(--sand); }
button.primary { background:var(--green); color:#fff; border-color:var(--gold); }
button.primary:hover { background:var(--gold); color:#3a2c07; }
.share-bar { display:flex; align-items:center; gap:10px; margin:8px 0 10px;
  font-family:Helvetica,Arial,sans-serif; font-size:.8rem; }
.share-sum { font-weight:700; padding:2px 10px; border-radius:12px; }
.share-sum.ok { background:#eaf2e5; color:var(--green); }
.share-sum.bad { background:#fbe4e3; color:var(--red); }
table { border-collapse:collapse; width:100%; margin:14px 0; font-family:Helvetica,Arial,sans-serif;
  font-size:.8rem; background:#fff; }
th { background:var(--sand); text-align:left; padding:7px 8px; border-bottom:2px solid var(--green);
  font-size:.7rem; text-transform:uppercase; letter-spacing:.04em; color:#4a4132; }
td { padding:6px 8px; border-bottom:1px solid var(--line); vertical-align:top; }
tr:hover td { background:#fdf9f0; }
.over { color:var(--red); font-weight:700; }
.under { color:__GREEN__; }
.chip { display:inline-block; width:11px; height:11px; border-radius:50%; margin-right:5px; vertical-align:middle; }
.plogo { width:20px; height:20px; object-fit:contain; vertical-align:-5px; margin-right:6px;
  border-radius:3px; background:#fff; }
.interpret p { margin:8px 0; font-size:.92rem; }
.interpret .big { font-size:1.05rem; }
/* ---- Gallagher disproportionality ---- */
.gallagher { display:flex; gap:18px; align-items:center; margin:16px 0 4px;
  background:#fff; border:1px solid var(--line); border-left:5px solid var(--gold);
  border-radius:6px; padding:14px 18px; }
.g-value { font-family:Helvetica,Arial,sans-serif; font-size:2.3rem; font-weight:800;
  line-height:1; flex:0 0 auto; min-width:96px; text-align:center; }
.g-body { min-width:0; }
.g-label { font-family:Helvetica,Arial,sans-serif; font-size:.72rem; text-transform:uppercase;
  letter-spacing:.07em; font-weight:700; color:#4a4132; }
.g-verdict { display:inline-block; font-family:Helvetica,Arial,sans-serif; font-size:.74rem;
  font-weight:700; border-radius:3px; padding:1px 8px; margin:5px 0 5px; }
.g-note { font-size:.8rem; color:var(--muted); margin:0; line-height:1.45; }
.gv-low  { color:var(--green); }
.gv-mid  { color:#a8741a; }
.gv-high { color:var(--red); }
.g-verdict.gv-low  { background:#eaf2e5; }
.g-verdict.gv-mid  { background:#f6e9d0; }
.g-verdict.gv-high { background:#fbe4e3; }
/* ---- editable parties ---- */
.party-row { display:grid; grid-template-columns:34px 1fr 96px 120px 34px; gap:8px;
  align-items:center; margin:6px 0; font-family:Helvetica,Arial,sans-serif; font-size:.84rem; }
.party-row input[type=text] { padding:4px 6px; border:1px solid var(--line); border-radius:4px; width:100%; }
.party-row input[type=number] { width:100%; padding:4px 6px; border:1px solid var(--line); border-radius:4px; }
.party-row input[type=color] { width:34px; height:26px; border:1px solid var(--line);
  border-radius:4px; background:#fff; padding:0; }
.party-row .rm { border:1px solid var(--line); background:#fff; color:var(--red);
  border-radius:4px; cursor:pointer; font-weight:700; padding:3px 0; }
.party-row .rm:hover { background:#fdf2f1; }
.party-head { display:grid; grid-template-columns:34px 1fr 96px 120px 34px; gap:8px;
  font-family:Helvetica,Arial,sans-serif; font-size:.66rem; text-transform:uppercase;
  letter-spacing:.05em; color:var(--muted); margin:14px 0 4px; }
/* ---- map ---- */
.mapwrap { display:grid; grid-template-columns:minmax(320px,1.05fr) minmax(320px,1fr);
  gap:20px; align-items:start; }
@media (max-width:940px) { .mapwrap { grid-template-columns:1fr; } }
.mapcard { background:transparent; border:none; padding:0; }
#mapsvg { width:100%; height:auto; display:block; background:transparent; }
#mapsvg path.unit { stroke:#fffdf8; stroke-width:.5; cursor:pointer; transition:fill .15s; }
#mapsvg path.unit:hover { stroke:__RED__; stroke-width:1.4; }
#mapsvg path.unit.sel { stroke:var(--red); stroke-width:2.1; }
#mapsvg path.unit.manual { stroke:var(--gold); stroke-width:1.5; stroke-dasharray:2.4 1.6; }
#mapsvg path.regionline { fill:none; stroke:__GREEN__; stroke-opacity:.45; stroke-width:1;
  pointer-events:none; }
.maptools { display:flex; gap:8px; margin-top:8px; }
.tooltip { position:fixed; pointer-events:none; background:rgba(28,26,23,.95); color:#fff;
  padding:6px 9px; border-radius:4px; font:12px/1.35 Helvetica,Arial,sans-serif; z-index:50;
  opacity:0; transition:opacity .1s; }
.legend { display:flex; flex-wrap:wrap; gap:6px 14px; margin-top:10px;
  font-family:Helvetica,Arial,sans-serif; font-size:.76rem; }
.legend .sw { display:inline-block; width:12px; height:12px; border-radius:3px;
  margin-right:5px; vertical-align:-1px; border:1px solid rgba(0,0,0,.15); }
.seatpill { display:inline-block; background:var(--sand); border-radius:10px; padding:0 8px;
  font-weight:700; font-size:.72rem; }
.unit-votes td { padding:4px 6px; }
.unit-votes input { width:96px; padding:3px 5px; border:1px solid var(--line); border-radius:4px; }
.card { background:#fff; border:1px solid var(--line); border-radius:6px; padding:12px 14px; }
.statline { display:flex; flex-wrap:wrap; gap:10px; margin:12px 0; }
.stat { background:#fff; border:1px solid var(--line); border-top:3px solid var(--red);
  padding:8px 12px; border-radius:0 0 4px 4px; min-width:120px; }
.stat:nth-child(even) { border-top-color:var(--green); }
.statv { font-family:Helvetica,Arial,sans-serif; font-size:1.25rem; font-weight:700; }
.statl { font-family:Helvetica,Arial,sans-serif; font-size:.66rem; text-transform:uppercase;
  letter-spacing:.05em; color:#555; }
.lawquote { margin:22px 0; }
.lawquote blockquote { margin:0; background:#fff; border:1px solid var(--line);
  border-top:5px solid var(--gold); padding:18px 22px;
  font-family:'Amiri','Traditional Arabic','Noto Naskh Arabic',serif;
  font-size:1.3rem; line-height:2; color:var(--ink); direction:rtl; text-align:right; }
.lawquote .lawen { font-style:italic; color:#3a352c; background:#fffdf6;
  border-left:4px solid var(--green); padding:11px 15px; margin:10px 0 8px; }
.lawquote .lawtag { display:inline-block; background:var(--red); color:#fff;
  font-family:Helvetica,Arial,sans-serif; font-size:.64rem; font-weight:700;
  letter-spacing:.06em; text-transform:uppercase; border-radius:3px;
  padding:2px 7px; margin-right:8px; }
.lawquote figcaption { font-family:Helvetica,Arial,sans-serif; font-size:.78rem;
  color:var(--muted); line-height:1.6; }
.lawquote figcaption a { color:var(--green); }
footer { margin-top:46px; padding-top:14px; border-top:4px solid var(--green);
  box-shadow:inset 0 2px 0 var(--gold);
  font-family:Helvetica,Arial,sans-serif; font-size:.74rem; color:var(--muted); }
</style>
</head>
<body>
<header class="masthead">
  <div class="masthead-bg" aria-hidden="true"></div>
  <div class="masthead-inner">
    <div class="flagmark">__STARBADGE__</div>
    <div class="masthead-copy">
      <div class="kicker">Interactive &middot; Chambre des Repr&eacute;sentants &middot; 395 si&egrave;ges</div>
      <h1>Constituency Seat Simulator</h1>
      <p class="deck">Draw your own electoral map: edit the parties, enter votes
      constituency by constituency, and watch the 395 seats fall. The method and
      the law behind it are stated below.</p>
    </div>
  </div>
</header>

<div class="wrap">

<div class="formula">
  <b>Quotient &eacute;lectoral :</b> <code>Q = I / S</code> &nbsp;where
  <code>I</code> = <b>registered voters</b> in the constituency and <code>S</code> = its
  seats. Each list takes <code>floor(votes / Q)</code> seats; the seats left over go to
  the <b>largest remainders</b> (<i>plus fort reste</i>). Majority = <b>198</b>.
  <span class="note" style="margin:0">This page divides the <b>votes you enter</b> by the
  constituency's seats — a stand-in for the register. The real count runs
  constituency by constituency: <b>__NLOCAL__ local seats</b> in __NUNITS__ map units and
  <b>__NREGIONAL__ regional seats</b> in the twelve regions, with
  <b>no electoral threshold</b>.</span>
</div>

<section>
  <h2>1. Parties — add, rename, recolour, remove</h2>
  <p class="note">The registry ships __NPARTIES__ parties (the <b>Others</b> row is the
  catch-all: in 2021 it gathers the micro-lists (AFG, NEO, PEDD, PML, PVM, PUD and
  others) that are not separate rows in <code>data/parties.csv</code>; MDS, FFD, CNI
  and PSU now have their own rows). Add any other party, remove the
  ones you do not want to field (a removed party simply does not take seats), and set each
  party's national vote share. Everything below recomputes as you type.</p>
  <div class="party-head"><span>Colour</span><span>Party</span><span>Share %</span>
    <span>Arabic name</span><span></span></div>
  <div class="share-bar"><span id="share-sum" class="share-sum"></span>
    <button onclick="normaliseShares()" title="Rescale shares so they add up to exactly 100%">Normalise to 100%</button></div>
  <div id="party-editor"></div>
  <div class="toolbar">
    <button class="primary" onclick="addParty()">+ Add a party</button>
    <button onclick="resetParties()">Reset to registry</button>
    <button onclick="prefill2021()">Prefill 2021 (est.)</button>
    <button onclick="equalShares()">Equal shares</button>
    <button onclick="loadResults2021()">Load the real 2021 result</button>
    <button onclick="randomiseConstituencies()">Scatter the map (&plusmn;55%)</button>
    <button onclick="clearOverrides()">Clear constituency overrides</button>
  </div>
</section>

<section>
  <h2>2. Constituency map — __NLOCAL__ local seats</h2>
  <p class="note">Each shape is a province or prefecture: the local constituency (where a
  prefecture is split into several seats in law, their seats are shown together).
  Click a shape to edit its votes and zoom in; drag to pan, scroll to zoom.
  By default the national shares above are applied uniformly everywhere — use
  <b>Scatter the map</b> or edit a constituency to build a geographic scenario.</p>
  <div class="mapwrap">
    <div class="mapcard">
      <svg id="mapsvg" viewBox="__VIEWBOX__" role="img"
           aria-label="Map of Morocco by province and prefecture"></svg>
      <div class="maptools">
        <label class="note" style="margin:0">Colour
          <select id="colourby" onchange="setColourBy(this.value)"
                  style="font:inherit;padding:3px 5px;border:1px solid var(--line);border-radius:4px">
            <option value="leader">Leading party</option>
            <option value="margin">Competitiveness</option>
            <option value="turnout">Turnout</option>
          </select></label>
        <span class="note" id="map-hint" style="margin:0"></span>
      </div>
    </div>
    <div>
      <div class="statline" id="map-stats"></div>
      <div class="card" id="unit-panel"></div>
      <div class="legend" id="map-legend"></div>
    </div>
  </div>
</section>

<section>
  <h2>3. Seats out — the 395-seat hemicycle</h2>
  <div id="hemicycle"></div>
  <p class="note" id="split"></p>
  <div id="seatbar"></div>
  <div class="gallagher" id="gallagher"></div>
</section>

<section>
  <h2>4. Results</h2>
  <div id="results-table"></div>
  <div class="interpret" id="interpret"></div>
</section>

<section>
  <h2>5. Legal basis — what the law says</h2>
  __LAW__
</section>

<footer>
  <p>Generated from <code>data/</code> (parties, constituencies, elections) and
  <code>data/morocco_map.json</code> by <code>scripts/build_simulator.py</code>.
  The map is simplified from
  <b>geoBoundaries</b> MAR ADM2/ADM1 (OpenStreetMap / Wambacher, ODbL 1.0); seat
  counts are the official 2021 table (<a href="https://www.chambredesrepresentants.ma/fr/actualites/donnees-chiffrees-autour-du-scrutin-legislatif-du-mercredi-8-septembre-2021-conformement">Chambre des repr&eacute;sentants</a>).
  Method: proportional representation, largest remainder, quotient on registered
  voters (loi organique n&deg; 27.11, arts. 1 &amp; 84), <b>no electoral threshold</b>.</p>
  <p class="small">Party logos are the trademarks of the respective parties, used
  editorially for identification only; image files sourced from the official
  national portal (maroc.ma).</p>
</footer>

</div>
<div class="tooltip" id="tip"></div>

<script>
const REGISTRY = __PARTIES__;
const LOGOS = __LOGOS__;            // party_id -> data-URI logo (absent = colour chip)
const CONSTS = __CONSTS__;
const MAP = __MAP__;
const RESULTS = __RESULTS__;        // map_unit -> {party_id: 2021 votes}
const SEATS2021 = __SEATS2021__;    // party_id -> official 2021 seats
const TURNOUT_2021 = __TURNOUT__;   // % participation, 8 Sept 2021
const SPLINTER_2021 = __SPLINTER__;  // thousands of votes for micro-lists, won no seat

const LOCAL = CONSTS.filter(c => c.level === "local");
const REGIONAL = CONSTS.filter(c => c.level === "regional");
const UNIT_SEATS = {}, UNIT_REGION = {}, UNIT_PREFECTURE = {}, REGION_SEATS = {};
LOCAL.forEach(c => {
  UNIT_SEATS[c.unit] = (UNIT_SEATS[c.unit] || 0) + c.seats;
  UNIT_REGION[c.unit] = c.region;
  UNIT_PREFECTURE[c.unit] = c.prefecture;
});
REGIONAL.forEach(c => { REGION_SEATS[c.region] = c.seats; });
const UNITS = Object.keys(UNIT_SEATS).sort((a,b) => MAP.units[a].label.localeCompare(MAP.units[b].label));
const TOTAL_LOCAL = LOCAL.reduce((a,c) => a + c.seats, 0);
const TOTAL_REGIONAL = REGIONAL.reduce((a,c) => a + c.seats, 0);
const TOTAL_SEATS = TOTAL_LOCAL + TOTAL_REGIONAL;
const MAJORITY = Math.floor(TOTAL_SEATS / 2) + 1;
const PALETTE = ["#c1272d","#006233","#c8a24a","#1f77b4","#9467bd","#e377c2",
                 "#8c564b","#17becf","#f4a100","#2ca02c","#d62728","#7f7f7f"];

let parties = [];
let overrides = {};        // map_unit -> {party_id: votes}
let regOverrides = {};     // map_unit -> registered voters (absolute)
let turnoutPct = TURNOUT_2021;
let selected = null;
let colourBy = "leader";
let nextId = 1;

function freshParties() {
  nextId = 1;
  return REGISTRY.map(p => ({ id:p.id, name:p.name, ar:p.ar, color:p.color, share:p.share }));
}
parties = freshParties();

// A party's logo when we have one, otherwise its colour chip.
function partyBadge(p) {
  if (LOGOS[p.id]) return `<img class="plogo" src="${LOGOS[p.id]}" alt="" aria-hidden="true">`;
  return `<span class="chip" style="background:${p.color}"></span>`;
}

/* ------------------------- seat allocation ------------------------- */
function allocate(votes, seats, registered) {
  let res = {};
  const entries = Object.entries(votes).filter(([k, v]) => v > 0);
  if (!entries.length || seats <= 0) return res;
  const total = entries.reduce((a, [, v]) => a + v, 0);
  // Article 84: the quotient divides REGISTERED VOTERS by the seats. Where no
  // register is given we fall back to votes cast (the old illustration).
  let quota = (registered && registered > 0) ? registered / seats : total / seats;
  let rems = [], used = 0;
  const run = (q) => {
    res = {}; rems = []; used = 0;
    for (const [k, v] of entries) {
      const a = Math.floor(v / q);
      if (a > 0) res[k] = a;
      used += a;
      rems.push([k, v - a * q]);
    }
  };
  run(quota);
  if (used > seats) { quota = total / seats; run(quota); }   // defensive
  rems.sort((x, y) => (y[1] - x[1]) || (votes[y[0]] - votes[x[0]]));
  let left = seats - used, i = 0;
  while (left > 0 && rems.length) {
    const k = rems[i % rems.length][0];
    res[k] = (res[k] || 0) + 1; left--; i++;
  }
  return res;
}

function derivedVotes(unit) {
  // Illustrative turnout base: ~55k voters per seat, roughly the national
  // registered-voters / seats ratio. Only ratios matter for the allocation.
  const base = (UNIT_SEATS[unit] || 1) * 55000;
  const sum = parties.reduce((a, p) => a + (Number(p.share) || 0), 0);
  const out = {};
  if (sum <= 0) return out;
  parties.forEach(p => {
    const v = (Number(p.share) || 0) / sum * base;
    if (v > 0) out[p.id] = v;
  });
  return out;
}
function mulberry32(a) {
  return function () {
    a |= 0; a = a + 0x6D2B79F5 | 0;
    let t = Math.imul(a ^ a >>> 15, 1 | a);
    t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
    return ((t ^ t >>> 14) >>> 0) / 4294967296;
  };
}
function randomiseConstituencies() {
  // Scenario tool: perturb each party's share in each constituency (not data).
  const rnd = mulberry32(20260923);
  overrides = {};
  UNITS.forEach(u => {
    const base = unitVotes(u);
    const v = {};
    parties.forEach(p => {
      const f = 1 + (rnd() - 0.5) * 1.1;      // ±55%
      v[p.id] = Math.max(0, (base[p.id] || 0) * f);
    });
    overrides[u] = v;
  });
  renderAll(); renderUnitPanel();
}
function unitVotes(unit) { return overrides[unit] || derivedVotes(unit); }
function unitVotesCast(unit) { return Object.values(unitVotes(unit)).reduce((a, b) => a + b, 0); }
function registeredVoters(unit) {
  // The register is a property of the constituency, not of the scenario: we
  // derive it from the votes cast here divided by the turnout assumption, so
  // the quotient keeps a consistent scale. The field can be overridden per unit.
  if (regOverrides[unit] != null) return regOverrides[unit];
  const t = Math.max(5, Math.min(100, Number(turnoutPct) || TURNOUT_2021)) / 100;
  return unitVotesCast(unit) / t;
}
function setRegistered(unit, value) {
  const v = Number(value);
  if (!isFinite(v) || v <= 0) delete regOverrides[unit]; else regOverrides[unit] = v;
  renderAll(); renderUnitPanel();
}
function loadResults2021() {
  // Real 8 Sept 2021 votes per province/prefecture, mapped onto the party list.
  overrides = {}; regOverrides = {};
  UNITS.forEach(u => { if (RESULTS[u]) overrides[u] = Object.assign({}, RESULTS[u]); });
  const nat = {};
  Object.values(RESULTS).forEach(rs => { for (const k in rs) nat[k] = (nat[k] || 0) + rs[k]; });
  const tot = Object.values(nat).reduce((a, b) => a + b, 0) || 1;
  parties.forEach(p => { p.share = Math.round((nat[p.id] || 0) / tot * 1000) / 10; });
  renderPartyEditor(); renderAll(); renderUnitPanel();
}

function computeAll() {
  const local = {}, regional = {}, total = {}, unitRes = {};
  parties.forEach(p => { local[p.id] = 0; regional[p.id] = 0; total[p.id] = 0; });
  UNITS.forEach(u => {
    const votes = unitVotes(u);
    const seats = allocate(votes, UNIT_SEATS[u], registeredVoters(u));
    unitRes[u] = { votes, seats };
    for (const k in seats) { local[k] = (local[k] || 0) + seats[k]; total[k] = (total[k] || 0) + seats[k]; }
  });
  const byRegion = {};
  UNITS.forEach(u => {
    const r = UNIT_REGION[u];
    byRegion[r] = byRegion[r] || {};
    const v = unitVotes(u);
    for (const k in v) byRegion[r][k] = (byRegion[r][k] || 0) + v[k];
  });
  REGIONAL.forEach(c => {
    const reg = UNITS.filter(u => UNIT_REGION[u] === c.region)
      .reduce((a, u) => a + registeredVoters(u), 0);
    const seats = allocate(byRegion[c.region] || {}, c.seats, reg);
    for (const k in seats) { regional[k] = (regional[k] || 0) + seats[k]; total[k] = (total[k] || 0) + seats[k]; }
  });
  const allVotes = {};
  UNITS.forEach(u => { const v = unitVotes(u); for (const k in v) allVotes[k] = (allVotes[k] || 0) + v[k]; });
  return { local, regional, total, unitRes, allVotes };
}

/* ------------------------- hemicycle ------------------------- */
function hemicycleLayout(n) {
  const rows = 9, Rmax = 300, Rmin = 104;
  const radii = Array.from({length:rows}, (_, i) => Rmax - (Rmax - Rmin) * i / (rows - 1));
  const wsum = radii.reduce((a,b) => a+b, 0);
  const counts = radii.map(r => Math.round(n * r / wsum));
  let diff = n - counts.reduce((a,b) => a+b, 0);
  for (let i = 0; diff !== 0; i++) {
    const j = i % rows, d = diff > 0 ? 1 : -1;
    if (counts[j] + d >= 1) { counts[j] += d; diff -= d; }
  }
  const pts = [];
  for (let i = 0; i < rows; i++) {
    const r = radii[i], k = counts[i];
    for (let j = 0; j < k; j++) {
      const th = (k === 1) ? Math.PI/2 : Math.PI - j * Math.PI / (k - 1);
      pts.push({ x:r*Math.cos(th), y:-r*Math.sin(th) });
    }
  }
  return pts;
}
function seatColours(seats) {
  const order = parties.slice().sort((a,b) => (seats[b.id]||0) - (seats[a.id]||0));
  const cols = [];
  order.forEach(p => { for (let i = 0; i < (seats[p.id]||0); i++) cols.push(p.color); });
  while (cols.length < TOTAL_SEATS) cols.push("__SAND__");
  return cols;
}
function renderHemicycle(seats) {
  const pts = hemicycleLayout(TOTAL_SEATS);
  const cols = seatColours(seats);
  const cx = 320, cy = 312;
  let dots = "", line = "";
  pts.forEach((p, i) => {
    dots += `<circle cx="${(cx+p.x).toFixed(1)}" cy="${(cy+p.y).toFixed(1)}" r="4" fill="${cols[i]}" stroke="white" stroke-width="0.6"/>`;
  });
  const p = pts[MAJORITY-1];
  if (p) line = `<line x1="${(cx+p.x).toFixed(1)}" y1="0" x2="${(cx+p.x).toFixed(1)}" y2="${(cy+p.y).toFixed(1)}" stroke="__RED__" stroke-width="1.5" stroke-dasharray="3 3"/>`;
  document.getElementById("hemicycle").innerHTML =
    `<svg viewBox="0 0 640 340" width="100%" role="img" aria-label="Parliament hemicycle">` +
    `<rect width="640" height="340" fill="__CREAM__"/>${line}${dots}</svg>`;
}

/* ------------------------- map ------------------------- */
const unitIndex = {};
function buildMap() {
  const svg = document.getElementById("mapsvg");
  let html = "";
  UNITS.forEach((u, i) => {
    unitIndex[u] = i;
    html += `<path id="u${i}" class="unit" data-unit="${u.replace(/"/g,'&quot;')}" d="${MAP.units[u].d}"></path>`;
  });
  Object.values(MAP.regions).forEach(d => { html += `<path class="regionline" d="${d}"></path>`; });
  svg.innerHTML = html;
  svg.addEventListener("click", ev => {
    const path = ev.target.closest("path.unit");
    if (path) { selectUnit(path.dataset.unit); }
  });
  svg.addEventListener("mousemove", ev => {
    const path = ev.target.closest("path.unit");
    if (path) showTip(ev, path.dataset.unit); else hideTip();
  });
  svg.addEventListener("mouseleave", hideTip);
}
function showTip(ev, unit) {
  const tip = document.getElementById("tip");
  const r = unitResGlobal[unit];
  const lead = leadingParty(r);
  tip.innerHTML = `<b>${MAP.units[unit].label}</b><br>${UNIT_SEATS[unit]} local seats`
    + (UNIT_PREFECTURE[unit] && UNIT_PREFECTURE[unit] !== MAP.units[unit].label ? ` · ${UNIT_PREFECTURE[unit]}` : "")
    + (lead ? `<br>Leading: ${lead.name} (${lead.seats})` : "<br>no votes entered");
  tip.style.left = (ev.clientX + 14) + "px";
  tip.style.top = (ev.clientY + 14) + "px";
  tip.style.opacity = "1";
}
function hideTip() { document.getElementById("tip").style.opacity = "0"; }
function leadingParty(r) {
  if (!r) return null;
  let best = null;
  parties.forEach(p => {
    const s = r.seats[p.id] || 0;
    if (!best || s > best.seats || (s === best.seats && s > 0 && (r.votes[p.id]||0) > (r.votes[best.id]||0)))
      if (s > 0 || (r.votes[p.id]||0) > 0) best = { id:p.id, name:p.name, color:p.color, seats:s, votes:r.votes[p.id]||0 };
  });
  return best;
}
let unitResGlobal = {};
function setColourBy(v) { colourBy = v; if (lastRes) { renderMap(lastRes); renderLegend(lastRes); } }
function mix(a, b, t) {
  return "rgb(" + a.map((v, i) => Math.round(v + (b[i] - v) * t)).join(",") + ")";
}
function marginColour(m) {
  // 0 = knife-edge (sand), 0.5 = solid (gold), 1 = safe (green)
  return m < 0.5 ? mix([246, 239, 224], [200, 162, 74], m / 0.5)
                 : mix([200, 162, 74], [0, 98, 51], (m - 0.5) / 0.5);
}
function unitMargin(r) {
  const vs = Object.values(r.votes).sort((a, b) => b - a);
  if (!vs.length) return null;
  if (vs.length === 1) return 1;
  return (vs[0] - vs[1]) / (vs[0] + vs[1]);
}
function renderMap(res) {
  unitResGlobal = res.unitRes;
  UNITS.forEach(u => {
    const path = document.getElementById("u" + unitIndex[u]);
    if (!path) return;
    const r = res.unitRes[u];
    const lead = leadingParty(r);
    const manual = overrides[u] ? " manual" : "";
    const sel = (selected === u) ? " sel" : "";
    path.setAttribute("class", "unit" + manual + sel);
    if (!lead) { path.setAttribute("fill", "__SAND__"); path.setAttribute("fill-opacity", "1"); return; }
    if (colourBy === "turnout") {
      const reg = registeredVoters(u), cast = unitVotesCast(u);
      const t = reg > 0 ? Math.min(1, (cast / reg) / 0.8) : 0;
      path.setAttribute("fill", mix([246, 239, 224], [0, 98, 51], t));
      path.setAttribute("fill-opacity", "0.95");
    } else if (colourBy === "margin") {
      const m = unitMargin(r);
      path.setAttribute("fill", marginColour(m === null ? 0 : m));
      path.setAttribute("fill-opacity", "0.95");
    } else {
      path.setAttribute("fill", lead.color);
      path.setAttribute("fill-opacity", (0.5 + 0.5 * r.seats[lead.id] / UNIT_SEATS[u]).toFixed(2));
    }
  });
}
function renderMapStats(res) {
  const filled = UNITS.filter(u => Object.keys(res.unitRes[u].seats).length).length;
  let best = null;
  UNITS.forEach(u => { const l = leadingParty(res.unitRes[u]); if (l && (!best || l.seats > best.seats)) best = l; });
  const totalCast = Object.values(res.allVotes).reduce((a,b)=>a+b,0);
  let close = 0;
  UNITS.forEach(u => { const m = unitMargin(res.unitRes[u]); if (m !== null && m < 0.05 && Object.keys(res.unitRes[u].seats).length) close++; });
  document.getElementById("map-stats").innerHTML =
    `<div class="stat"><div class="statv">${filled}/${UNITS.length}</div><div class="statl">map units with votes</div></div>` +
    `<div class="stat"><div class="statv">${TOTAL_LOCAL}+${TOTAL_REGIONAL}</div><div class="statl">local + regional seats</div></div>` +
    `<div class="stat"><div class="statv">${totalCast ? (totalCast/1e6).toFixed(1)+"M" : "0"}</div><div class="statl">votes entered</div></div>` +
    `<div class="stat"><div class="statv">${close}</div><div class="statl">races within 5 points</div></div>`;
}
function renderLegend(res) {
  const seatTotals = res.total;
  const items = parties.slice().sort((a,b) => (seatTotals[b.id]||0)-(seatTotals[a.id]||0))
    .map(p => `<span><span class="sw" style="background:${p.color}"></span>${p.name} `
      + `<span class="seatpill">${seatTotals[p.id]||0}</span></span>`).join("");
  const scale = (colourBy === "turnout")
    ? `<span class="note" style="margin:0">turnout: `
      + `<span class="sw" style="background:${mix([246,239,224],[0,98,51],0)}"></span>low `
      + `<span class="sw" style="background:${mix([246,239,224],[0,98,51],0.5)}"></span>mid `
      + `<span class="sw" style="background:${mix([246,239,224],[0,98,51],1)}"></span>80%+</span>`
    : (colourBy === "margin")
    ? `<span class="note" style="margin:0">competitiveness: `
      + `<span class="sw" style="background:${marginColour(0)}"></span>knife-edge `
      + `<span class="sw" style="background:${marginColour(0.5)}"></span>solid `
      + `<span class="sw" style="background:${marginColour(1)}"></span>safe</span>`
    : "";
  document.getElementById("map-legend").innerHTML = items + scale
    + `<span><span class="sw" style="background:__SAND__"></span>no votes / no seats</span>`;
}

/* ------------------------- unit panel ------------------------- */
function selectUnit(unit) {
  selected = unit;
  renderUnitPanel();
  renderMap(lastRes);
}
function renderUnitPanel() {
  const panel = document.getElementById("unit-panel");
  if (!selected) {
    panel.innerHTML = `<h3 style="margin-top:0">Pick a constituency</h3>
      <p class="note" style="margin:0">Click a shape on the map to enter its votes.
      The national shares above are applied to every constituency until you override one.</p>`;
    return;
  }
  const votes = unitVotes(selected);
  const reg = registeredVoters(selected);
  const seats = allocate(votes, UNIT_SEATS[selected], reg);
  const quota = UNIT_SEATS[selected] ? reg / UNIT_SEATS[selected] : 0;
  const reached = parties.filter(p => (votes[p.id] || 0) >= quota).length;
  const rows = parties.map(p => `<tr>
      <td>${partyBadge(p)}${p.name}</td>
      <td><input type="number" min="0" step="100" value="${Math.round(votes[p.id]||0)}"
           oninput="setSelectedVote('${p.id}',this.value)"></td>
      <td id="us-${p.id}"><b>${seats[p.id]||0}</b></td></tr>`).join("");
  panel.innerHTML = `<h3 style="margin-top:0">${MAP.units[selected].label}
      <span class="seatpill">${UNIT_SEATS[selected]} seats</span>
      ${overrides[selected] ? '<span class="seatpill" style="background:#f6e0aa">edited</span>' : ""}</h3>
    <p class="note" style="margin:2px 0 8px">Region: ${UNIT_REGION[selected]}${
      UNIT_PREFECTURE[selected] !== MAP.units[selected].label ? " · " + UNIT_PREFECTURE[selected] : ""}
      · quotient <b>Q = ${Math.round(quota).toLocaleString()}</b> registered voters per seat
      · lists reaching Q: <b>${reached}</b></p>
    <table class="unit-votes"><thead><tr><th>Party</th><th>Votes</th><th>Seats</th></tr></thead>
    <tbody>${rows}</tbody></table>
    <p class="note" style="margin:4px 0 2px">Votes cast here: <b>${Math.round(unitVotesCast(selected)).toLocaleString()}</b>
    &middot; registered voters: <b>${Math.round(reg).toLocaleString()}</b></p>
    <div class="party-row" style="grid-template-columns:1fr 150px;margin:10px 0 4px">
      <span style="font-family:Helvetica,Arial,sans-serif;font-size:.82rem">Registered voters
      ${regOverrides[selected] != null ? '<span class="seatpill" style="background:#f6e0aa">edited</span>' : ''}</span>
      <input type="number" min="0" step="1000" value="${Math.round(reg)}"
             oninput="setSelectedRegistered(this.value)">
    </div>
    <button onclick="resetUnit()">Use national shares here</button>`;
}
function setUnitVote(unit, partyId, value) {
  if (!overrides[unit]) overrides[unit] = Object.assign({}, unitVotes(unit));
  overrides[unit][partyId] = Math.max(0, Number(value) || 0);
  renderAll();
  const cell = document.getElementById("us-" + partyId);
  if (cell) { const a = allocate(unitVotes(unit), UNIT_SEATS[unit]); cell.innerHTML = `<b>${a[partyId]||0}</b>`; }
}
function setSelectedVote(partyId, value) {
  if (selected) setUnitVote(selected, partyId, value);
}
function resetUnit() {
  if (!selected) return;
  delete overrides[selected]; delete regOverrides[selected];
  renderAll(); renderUnitPanel();
}
function setSelectedRegistered(v) { if (selected) setRegistered(selected, v); }
function clearOverrides() { overrides = {}; regOverrides = {}; renderAll(); renderUnitPanel(); }

/* ------------------------- parties editor ------------------------- */
function renderPartyEditor() {
  const el = document.getElementById("party-editor");
  el.innerHTML = parties.map((p, i) => {
    const logo = LOGOS[p.id] ? `<img class="plogo" src="${LOGOS[p.id]}" alt="" aria-hidden="true">` : "";
    return `<div class="party-row">
      <input type="color" value="${p.color}" oninput="setParty(${i},'color',this.value)">
      <div style="display:flex;align-items:center;gap:6px;min-width:0">${logo}
        <input type="text" value="${(p.name||"").replace(/"/g,'&quot;')}" oninput="setParty(${i},'name',this.value)">
      </div>
      <input type="number" min="0" step="0.5" value="${p.share}" oninput="setParty(${i},'share',this.value)">
      <span class="note" style="margin:0">${p.ar||""}</span>
      <button class="rm" title="Remove party" onclick="removeParty(${i})">&times;</button>
    </div>`;
  }).join("");
}
function setParty(i, key, value) {
  if (!parties[i]) return;
  parties[i][key] = (key === "share") ? Math.max(0, Number(value) || 0) : value;
  renderAll();
}
function addParty() {
  parties.push({ id:"x" + (nextId++), name:"New party", ar:"", share:0,
                 color:PALETTE[parties.length % PALETTE.length] });
  renderPartyEditor(); renderAll();
}
function removeParty(i) {
  const gone = parties[i]; if (!gone) return;
  parties.splice(i, 1);
  Object.keys(overrides).forEach(u => { delete overrides[u][gone.id]; if (!Object.keys(overrides[u]).length) delete overrides[u]; });
  if (selected === gone.id) selected = null;
  renderPartyEditor(); renderAll(); renderUnitPanel();
}
function resetParties() { parties = freshParties(); overrides = {}; selected = null; renderPartyEditor(); renderAll(); renderUnitPanel(); }
function prefill2021() {
  parties.forEach(p => { p.share = REGISTRY.find(r => r.id === p.id) ? (REGISTRY.find(r => r.id === p.id).share || 0) : p.share; });
  renderPartyEditor(); renderAll();
}
function sumShares() { return parties.reduce((a, p) => a + (Number(p.share) || 0), 0); }
function renderShareSum() {
  const el = document.getElementById("share-sum");
  if (!el) return;
  const s = sumShares();
  const ok = Math.abs(s - 100) < 0.05;
  el.className = "share-sum " + (ok ? "ok" : "bad");
  el.textContent = ok ? `Σ = ${s.toFixed(1)}%` : `Σ = ${s.toFixed(1)}% — not 100%`;
}
function normaliseShares() {
  const s = sumShares();
  if (s <= 0) { equalShares(); return; }
  // Work in tenths-of-a-percent to avoid float drift, then fix the residual so
  // the rounded shares add up to exactly 100.0%.
  const raw = parties.map(p => (Number(p.share) || 0) / s * 1000);
  const tenths = raw.map(x => Math.floor(x + 1e-9));
  let diff = 1000 - tenths.reduce((a, b) => a + b, 0);
  const order = [...Array(parties.length).keys()]
    .sort((a, b) => (raw[b] - tenths[b]) - (raw[a] - tenths[a]));
  for (let k = 0; diff !== 0; k++) {
    const i = order[k % order.length];
    const d = diff > 0 ? 1 : -1;
    tenths[i] += d; diff -= d;
  }
  parties.forEach((p, i) => p.share = tenths[i] / 10);
  renderPartyEditor(); renderAll();
}
function equalShares() {
  const n = parties.length || 1;
  const base = Math.floor(1000 / n);
  const rem = 1000 - base * n;
  parties.forEach((p, i) => p.share = (base + (i < rem ? 1 : 0)) / 10);
  renderPartyEditor(); renderAll();
}

/* ------------------------- results ------------------------- */
function renderTable(res) {
  const totalVotes = Object.values(res.allVotes).reduce((a,b) => a+b, 0) || 1;
  const rows = parties.map(p => {
    const v = res.allVotes[p.id] || 0, vs = v / totalVotes;
    const s = res.total[p.id] || 0, ss = s / TOTAL_SEATS;
    const adv = (vs > 0 && ss > 0) ? ss / vs : null;
    const cls = adv === null ? "" : (adv > 1.02 ? "over" : adv < 0.98 ? "under" : "");
    const s21 = SEATS2021[p.id];
    const d = (s21 == null) ? null : s - s21;
    const dTxt = (d === null) ? "—" : (d > 0 ? "+" + d : String(d));
    const dCls = (d === null || d === 0) ? "" : (d > 0 ? "over" : "under");
    return `<tr><td>${partyBadge(p)}${p.name}</td>` +
      `<td>${(vs*100).toFixed(1)}%</td><td>${res.local[p.id]||0}</td>` +
      `<td>${res.regional[p.id]||0}</td><td><b>${s}</b></td>` +
      `<td>${(ss*100).toFixed(1)}%</td><td class="${cls}">${adv === null ? "—" : adv.toFixed(2)}</td>` +
      `<td class="${dCls}">${dTxt}</td></tr>`;
  }).join("");
  document.getElementById("results-table").innerHTML =
    `<table><thead><tr><th>Party</th><th>Vote %</th><th>Local</th><th>Regional</th>` +
    `<th>Total seats</th><th>Seat %</th><th>Advantage ratio</th><th>&Delta; vs 2021</th>` +
    `</tr></thead><tbody>${rows}</tbody></table>` +
    `<p class="note" style="margin:4px 0 0"><b>&Delta; vs 2021</b> compares your scenario with the
     <b>official</b> 2021 outcome (395 seats, local + regional) in <code>data/elections.csv</code>.
     Seeding the map with the real 2021 votes (<button onclick="loadResults2021()" style="padding:2px 7px">Load 2021</button>)
     reconstructs a result from those votes — close to, but not identical with, the official count,
     because the regional seats are allocated here from the local votes and four of the 305 local
     seats could not be traced in the source PDF. The micro-lists (about
     <b>${SPLINTER_2021}k votes, 3.4%</b>) won no seat in 2021 and are excluded from the model.</p>`;
}
function renderSeatBar(res) {
  const total = TOTAL_SEATS;
  let left = 0, bars = "";
  parties.slice().sort((a,b) => (res.total[b.id]||0)-(res.total[a.id]||0)).forEach(p => {
    const s = res.total[p.id] || 0; if (!s) return;
    const w = s / total * 100;
    bars += `<div style="width:${w}%;background:${p.color}" title="${p.name}: ${s}"></div>`;
    left += w;
  });
  if (left < 100) bars += `<div style="width:${100-left}%;background:__SAND__" title="unfilled"></div>`;
  document.getElementById("seatbar").innerHTML =
    `<div style="display:flex;height:20px;border:1px solid var(--line);border-radius:3px;overflow:hidden;margin-top:6px">${bars}</div>`;
}
function renderInterpretation(res) {
  const entries = parties.map(p => ({ p, v:res.allVotes[p.id]||0, s:res.total[p.id]||0 }))
    .filter(e => e.v > 0 || e.s > 0).sort((a,b) => b.s - a.s);
  const totalVotes = Object.values(res.allVotes).reduce((a,b) => a+b, 0) || 1;
  if (!entries.length) {
    document.getElementById("interpret").innerHTML =
      `<p class="note">No votes entered yet. Set national shares above, or click a constituency and type its numbers.</p>`;
    return;
  }
  const largest = entries[0];
  let sum = 0; const coal = [];
  for (const e of entries) { if (sum >= MAJORITY) break; sum += e.s; coal.push(e.p.name); }
  document.getElementById("interpret").innerHTML = `
    <p class="big">Largest party: <b>${largest.p.name}</b> with <b>${largest.s}</b> seats
    (${(largest.s/TOTAL_SEATS*100).toFixed(1)}%) — ${largest.s >= MAJORITY
      ? "reaches the 198-seat majority alone." : "short of the 198-seat majority."}</p>
    <p>A minimal winning bloc needs ${MAJORITY} seats. Counting from the largest, a coalition of
    <b>${coal.length}</b> groups (${coal.join(", ")}) crosses it.</p>
    <p class="note">Advantage ratio = seat share ÷ vote share (1.00 = proportional;
    &gt;1.00 the seat system over-rewards the party, &lt;1.00 it under-rewards).
    The vote-seat gap is measured by the <b>Gallagher index</b> above the hemicycle.</p>`;
}

function renderGallagher(res) {
  const totalVotes = Object.values(res.allVotes).reduce((a,b) => a+b, 0) || 1;
  const entries = parties.map(p => ({ v:res.allVotes[p.id]||0, s:res.total[p.id]||0 }))
    .filter(e => e.v > 0 || e.s > 0);
  const el = document.getElementById("gallagher");
  if (!entries.length) { el.innerHTML = ""; return; }
  let g = 0, nv = 0, ns = 0;
  entries.forEach(e => { const d = e.v/totalVotes - e.s/TOTAL_SEATS; g += d*d; });
  g = Math.sqrt(0.5 * g) * 100;
  entries.forEach(e => { nv += (e.v/totalVotes)**2; ns += (e.s/TOTAL_SEATS)**2; });
  const enpv = nv ? 1/nv : 0, enps = ns ? 1/ns : 0;
  const band = g < 5 ? "gv-low" : (g < 10 ? "gv-mid" : "gv-high");
  const verdict = g < 5 ? "highly proportional" : (g < 10 ? "moderate distortion" : "strong distortion");
  el.innerHTML = `
    <div class="g-value ${band}">${g.toFixed(2)}</div>
    <div class="g-body">
      <div class="g-label">Gallagher disproportionality index</div>
      <div><span class="g-verdict ${band}">${verdict}</span></div>
      <p class="g-note">The square root of half the sum of squared
      (vote-share &minus; seat-share) gaps, expressed as a percentage: <b>0</b> = perfectly
      proportional, <b>5+</b> moderate, <b>10+</b> high. Effective number of parties
      <b>${enpv.toFixed(2)}</b> by votes &middot; <b>${enps.toFixed(2)}</b> by seats.</p>
    </div>`;
}
/* ------------------------- orchestration ------------------------- */
let lastRes = null;
function renderAll() {
  const res = computeAll();
  lastRes = res;
  renderShareSum();
  renderMap(res);
  renderMapStats(res);
  renderLegend(res);
  renderHemicycle(res.total);
  renderSeatBar(res);
  renderGallagher(res);
  renderTable(res);
  renderInterpretation(res);
  const lg = res.local, rg = res.regional;
  const lSum = Object.values(lg).reduce((a,b)=>a+b,0), rSum = Object.values(rg).reduce((a,b)=>a+b,0);
  document.getElementById("split").innerHTML =
    `Local constituencies fill <b>${lSum}</b>/${TOTAL_LOCAL} seats · regional constituencies fill ` +
    `<b>${rSum}</b>/${TOTAL_REGIONAL} · <b>${lSum+rSum}</b>/${TOTAL_SEATS} total. ` +
    `Grey seats are unfilled because those constituencies have no votes yet.`;
}
function init() {
  buildMap();
  renderPartyEditor();
  renderUnitPanel();
  renderAll();
  document.getElementById("map-hint").textContent =
    `${UNITS.length} map units · ${TOTAL_LOCAL} local seats`;
}
init();
</script>
</body>
</html>
"""

render_card(str(OUT / "og-simulator.png"),
            "MOROCCO \u00b7 CHAMBRE DES REPR\u00c9SENTANTS \u00b7 395 SEATS",
            "Seat Simulator",
            "Edit parties, enter votes, watch the seats fall")
SIM_TITLE = "Morocco 2026 \u2014 Constituency Seat Simulator"
SIM_DESC = ("Interactive seat simulator for Morocco's 395-seat Chamber of "
            "Representatives: edit parties, enter votes constituency by "
            "constituency, and watch the seats fall.")
SIM_OG_META = f"""
<meta name="description" content="{SIM_DESC}">
<meta name="theme-color" content="#c1272d">
<meta property="og:type" content="website">
<meta property="og:site_name" content="Morocco Elections 2026">
<meta property="og:title" content="{SIM_TITLE}">
<meta property="og:description" content="{SIM_DESC}">
<meta property="og:url" content="{_SITE_BASE}/seat_simulator.html">
<meta property="og:image" content="{_SITE_BASE}/og-simulator.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{SIM_TITLE}">
<meta name="twitter:description" content="{SIM_DESC}">
<meta name="twitter:image" content="{_SITE_BASE}/og-simulator.png">"""

html = (TEMPLATE
        .replace("__PARTIES__", js(party_js))
        .replace("__LOGOS__", js(logos_js))
        .replace("__OG_META__", SIM_OG_META)
        .replace("__CONSTS__", js(const_js))
        .replace("__MAP__", js(map_data))
        .replace("__RESULTS__", js(results_js))
        .replace("__SEATS2021__", js(seats2021_js))
        .replace("__TURNOUT__", str(TURNOUT_2021))
        .replace("__SPLINTER__", f"{splinter_votes/1000:.0f}")
        .replace("__VIEWBOX__", map_data["viewBox"])
        .replace("__NLOCAL__", str(LOCAL_SEATS))
        .replace("__NREGIONAL__", str(REGIONAL_SEATS))
        .replace("__NUNITS__", str(N_UNITS))
        .replace("__NPARTIES__", str(len(party_js)))
        .replace("__STARBADGE__", STAR_BADGE_SVG)
        .replace("__STAR__", STAR_URI)
        .replace("__ZELLIGE__", ZELLIGE_URI)
        .replace("__FAVICON__", FAVICON_URI)
        .replace("__LAW__", LAW_HTML))

html = apply_tokens(html)

OUT.mkdir(parents=True, exist_ok=True)
(OUT / "seat_simulator.html").write_text(html, encoding="utf-8")
print(f"[ok] Simulator -> {OUT / 'seat_simulator.html'}")
print(f"[ok] {len(party_js)} parties, {len(const_js)} constituencies, "
      f"{N_UNITS} map units, {LOCAL_SEATS}+{REGIONAL_SEATS} seats")
