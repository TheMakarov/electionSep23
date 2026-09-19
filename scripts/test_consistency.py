#!/usr/bin/env python3
"""
Cross-consistency tests for the seat simulator and its data.

These are the invariants the simulator silently assumes. Run before trusting
the map or the hemicycle:

    python3 scripts/test_consistency.py      # or: make test

Every check prints PASS/FAIL; the process exits non-zero if anything fails.
"""
from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

FAILED: list[str] = []


def load(name: str):
    return list(csv.DictReader(open(DATA / name, newline="", encoding="utf-8")))


def check(ok: bool, msg: str):
    print(("PASS  " if ok else "FAIL  ") + msg)
    if not ok:
        FAILED.append(msg)


def split_ids(raw) -> list[str]:
    if not raw:
        return []
    return [t for t in re.split(r"[,;|/\s]+", str(raw)) if t]


parties = load("parties.csv")
constituencies = load("constituencies.csv")
elections = load("elections.csv")
results = load("results_2021.csv")
promises = load("promises.csv")
claims = load("claims.csv")
sources = load("sources.csv")
map_data = json.load(open(DATA / "morocco_map.json", encoding="utf-8"))

pid = {p["party_id"] for p in parties}
sid = {s["source_id"] for s in sources}
units = set(map_data["units"])
regions = set(map_data["regions"])
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")

# parties that the simulator models with their own row (the 12 that won a seat
# in 2021); every other abbreviation is a micro-list that won no seat.
SPECIFIC = {"RNI": "P001", "PAM": "P002", "PI": "P003", "PJD": "P004",
            "USFP": "P005", "MP": "P006", "PPS": "P007", "UC": "P008",
            "MDS": "P010", "FFD": "P011", "CNI": "P012", "PSU": "P013"}

# ---- parties ---------------------------------------------------------------
check(len(pid) == len(parties), f"party ids are unique ({len(pid)} parties)")
for p in parties:
    check(HEX.match(p.get("color", "")), f"party {p['party_id']} has a valid colour")
check(all(p.get("name") for p in parties), "every party has a name")
check(set(SPECIFIC.values()) <= pid, "every modelled party has a row in parties.csv")

# ---- constituencies --------------------------------------------------------
local = [c for c in constituencies if c["level"] == "local"]
regional = [c for c in constituencies if c["level"] == "regional"]
local_seats = sum(int(c["seats"]) for c in local)
regional_seats = sum(int(c["seats"]) for c in regional)
check(local_seats == 305, f"local constituencies sum to 305 seats (got {local_seats})")
check(regional_seats == 90, f"regional constituencies sum to 90 seats (got {regional_seats})")
check(local_seats + regional_seats == 395,
      f"total constituencies sum to 395 seats (got {local_seats + regional_seats})")

local_units = {c["map_unit"] for c in local}
check(not (local_units - units),
      f"every local constituency maps to a map unit (orphans: {sorted(local_units - units) or 'none'})")
check(not (units - local_units),
      f"every map unit carries at least one local constituency (missing: {sorted(units - local_units) or 'none'})")

regional_units = {c["map_unit"] for c in regional}
check(regional_units <= regions,
      f"every regional constituency maps to a map region (bad: {sorted(regional_units - regions) or 'none'})")
check(regions <= regional_units,
      f"every map region has its regional seats (missing: {sorted(regions - regional_units) or 'none'})")

local_regions = {c["region"] for c in local}
check(local_regions <= regions,
      f"every local constituency's region is known (bad: {sorted(local_regions - regions) or 'none'})")
check(regional_units == local_regions,
      f"local and regional rows share the same region keys "
      f"(local-only: {sorted(local_regions - regional_units) or 'none'}, "
      f"regional-only: {sorted(regional_units - local_regions) or 'none'})")

# ---- map geometry ----------------------------------------------------------
check(len(units) == 75, f"map has 75 units (got {len(units)})")
check(len(regions) == 12, f"map has 12 regions (got {len(regions)})")
check(all(u.get("d") for u in map_data["units"].values()), "every map unit has a path")

# ---- elections: 100% shares, 395 seats, parties exist ----------------------
for year in ("2011", "2016", "2021"):
    rows = [r for r in elections if r["election_year"] == year]
    share = sum(float(r["vote_share_est"] or 0) for r in rows)
    seats = sum(int(r["seats"] or 0) for r in rows)
    check(abs(share - 100.0) < 0.11, f"{year} vote shares sum to 100% (got {share:.2f}%)")
    check(seats == 395, f"{year} seats sum to 395 (got {seats})")
for r in elections:
    check(r["party_id"] in pid, f"election row {r['election_year']} {r['party_id']} references a party")

# ---- results <-> map / parties (the linkage the user asked about) ----------
res_units = {r["map_unit"] for r in results}
check(res_units <= units, f"every result row maps to a known unit (bad: {sorted(res_units - units) or 'none'})")

res_parties = {r["party"] for r in results}
micro = res_parties - set(SPECIFIC)
missing_specific = set(SPECIFIC) - res_parties
check(missing_specific <= {"CNI"},
      f"every modelled party has 2021 local results, except CNI (regional-list seat "
      f"only, no local votes in the parsed source) (missing: {sorted(missing_specific) or 'none'})")
check(bool(micro) and all(m not in SPECIFIC for m in micro),
      f"micro-lists are a separate, non-empty set (got {sorted(micro) or 'none'})")
check(all(int(r["votes"]) > 0 for r in results), "every result row has positive votes")

# vote shares from the map must match the registry shares (map <-> parties link)
from collections import defaultdict  # noqa: E402
tot = defaultdict(int)
for r in results:
    if r["party"] in SPECIFIC:
        tot[r["party"]] += int(r["votes"])
denom = sum(tot.values())
e21 = {r["party_id"]: float(r["vote_share_est"]) for r in elections if r["election_year"] == "2021"}
mismatch = []
for abbr, p in SPECIFIC.items():
    map_share = tot[abbr] / denom * 100
    reg_share = e21.get(p, 0.0)
    if abs(map_share - reg_share) > 0.06:
        mismatch.append(f"{abbr}: map {map_share:.2f}% vs registry {reg_share:.2f}%")
check(not mismatch,
      f"map-derived vote shares equal the registry shares (mismatch: {mismatch or 'none'})")

# micro-lists must not swamp the model
micro_votes = sum(int(r["votes"]) for r in results if r["party"] not in SPECIFIC)
check(micro_votes / (denom + micro_votes) < 0.10,
      f"micro-list votes are a small splinter ({micro_votes/ (denom+micro_votes):.1%})")

# ---- promises / claims -----------------------------------------------------
for r in promises:
    check(r["party_id"] in pid, f"promise {r['promise_id']} references a party")
    check(int(r["term_start"]) == 2021 and int(r["term_end"]) == 2026,
          f"promise {r['promise_id']} is on the 2021-2026 term")
    for s in split_ids(r["source_ids"]):
        check(s in sid or s in ("FILL", "UNSOURCED"),
              f"promise {r['promise_id']} source {s} resolves")
claims21 = {c["claim_id"] for c in claims if c["election_year"] == "2021"}
check(len(claims21) == len(promises),
      f"the 2021 promise ledger has one row per 2021 claim "
      f"(claims={len(claims21)}, promises={len(promises)})")

print()
if FAILED:
    print(f"{len(FAILED)} check(s) FAILED")
    sys.exit(1)
print("All consistency checks passed.")
