# Morocco — Electoral Claims, Promises & Accountability

A reproducible, Reuters-style **data journalism project** for Moroccan voters,
built around the legislative election of **Wednesday 23 September 2026**.

It turns raw claims, promises, and election results into a quantified
accountability score per party, with charts and a self-contained HTML report.

The governing rule of this repository: **nothing is invented**. A number that
cannot be computed from the registry is printed as `n/a` together with the
reason it is missing. A `FILL` cell is shown, not hidden, and never silently
replaced with a default.

## Primary deliverable

Open **`output/report.html`** in a browser — it is fully self-contained (all
charts are inline SVG, no internet needed).

Section 1 of that report is a **publication gate**. While it says
`NOT PUBLISHABLE`, the report is a workbench, not a source. It is currently
`NOT PUBLISHABLE`: the campaign claims exist, but they are unscored and
single-sourced.

## Quick start

```bash
make venv            # one-time: local .venv with numpy + matplotlib
make check           # validate data/ — exits non-zero on a broken invariant
make build           # render output/report.html + charts + audit trail
make                 # same as: make check && make build
make strict          # validate, treating warnings as failures (pre-publish)
make serve           # preview at http://localhost:8000/report.html
```

Requires Python 3.10+. Dependencies are pinned in `requirements.txt`.
`make build` **refuses to run** while `make check` reports errors; pass
`--force` to `scripts/build.py` only when you deliberately want to see the
broken output.

## What you get

| File | What it is |
|---|---|
| `output/report.html` | The full report: publication gate, supportable vs. blocked findings, an auto-generated to-do list, readiness and sub-index tables, charts, the campaign claims table, promise ledger, leaders, sources, timeline, validation findings, methodology |
| `output/ceagi_scores.csv` | Sub-indices + coverage per party, machine-readable |
| `output/ceagi_scores.json` | Same, plus per-sub-index provenance (`n`, `year`, `detail`) and the gate results |
| `output/audit_trail.md` | Every number traced to a source ID, a sample size, a year, and a reason when absent |
| `output/charts/*.svg` | Individual vector charts (stale ones are deleted on each build) |
| `config.json` | Years, weights, and gate thresholds — the only place to change the model |
| `morocco-elections.org` | The research skeleton (org-mode) with methodology & algebra |

## Data integrity gate — `scripts/validate.py`

This is the highest-value part of the pipeline. Run it before trusting
anything. It reads `data/` and reports:

- **ERROR** — a broken invariant. Wrong or renamed header, duplicate key,
  ragged row (the signature of an unquoted comma silently truncating a cell),
  a reference to a source ID that does not exist, an out-of-range score, a bad
  enum. Any ERROR fails `make check` and blocks `make build`.
- **WARN** — incomplete but structurally sound. `FILL` placeholders (with a
  per-column census), rows marked `UNSOURCED`, claims resting on a single
  source, claims that bundle several numeric targets into one row, non-ISO
  dates, a CSV left open in LibreOffice.
- **INFO** — coverage. e.g. "claims exist for 2026 but there is no 2026
  election row, so E cannot be computed".

```bash
python3 scripts/validate.py             # human report
python3 scripts/validate.py --strict    # warnings fail too
python3 scripts/validate.py --json output/validation.json
```

### The `UNSOURCED` sentinel

Some legacy rows (promises and timeline entries from 2011–2021) cited source
IDs `S006`–`S010`, which no longer exist in `sources.csv`. Rather than leave a
phantom citation that made the audit trail *look* complete, those cells now say
`UNSOURCED`. It is greppable, it is honest, and the validator counts it. Replace
them with real source IDs from official Interior-Ministry and HCH/HCP documents.

An empty cell is treated exactly like `FILL`. Columns where absence is
legitimate (`official_slogan`, `notes`, `additional notes`, `timeline.election_year`)
are declared `optional` in the validator schema and excluded from the census,
and the `P009` "Others" bucket is excluded from the party census.

## How it works (3 layers)

1. **Sources** — `data/sources.csv`, one row per source, each with an ID (`S###`).
2. **Facts / Claims** — `data/claims.csv`, one row per claim (never per source).
3. **Synthesis** — `scripts/build.py` computes the CEAGI model and renders charts.

A claim is *corroborated* only with 2+ independent reliable sources.
Reliability and political lean are stored in **separate** columns.

## Two years, not one

The model deliberately separates the campaign from the record:

| Setting | Value | Meaning |
|---|---|---|
| `campaign_year` | 2026 | The election being contested. Claim credibility **C** comes from here. |
| `baseline_year` | 2021 | The last completed election. **E**, **G**, **L**, **M** come from here. |

**D** (delivery) is computed over completed terms only and requires at least
`min_promises_for_D` concluded promises before it is scored at all. The outgoing
2021–2026 coalition has no concluded promises on record, so its **D is `n/a`** —
which is the finding, not a bug.

## The CEAGI model (short)

```
S_p = ( D^0.25 · C^0.20 · E^0.10 · G^0.20 · L^0.15 · M^0.10 )
```

- **D** Delivery — `(fulfilled + 0.5·partial) / (fulfilled + partial + failed + abandoned)`, completed terms only, minimum 3 concluded promises
- **C** Claim credibility — claim taxonomy × 1–5 verification rubric
- **E** Electoral efficiency — `1 − |seat-share ÷ vote-share − 1| / max(…)`
- **G** Governance — coalition/portfolio weight, legislative output
- **L** Leadership — integrity, conflicts, skin-in-the-game
- **M** Mandate coherence — manifesto vs. action alignment

Weighted **geometric** mean, so a zero in any dimension cannot be fully
compensated. Uncertainty is an 8,000-draw Monte-Carlo pass (σ = 0.05 per
sub-index).

A full score is published **only when all six sub-indices exist**. The report
also shows a *provisional* average over whatever dimensions do exist, labelled
"not ranked" — because averaging over different dimension sets makes parties
incomparable, and ranking them would compare unlike things.

## Data provenance & caveats (read before publishing)

- **Seat counts** (2011/2016/2021) are from official election records → **HIGH** confidence.
- **Vote shares** are **ESTIMATES** (marked `LOW`) — replace with official Interior-Ministry figures.
- **The 2026 campaign claims** (`C001`–`C040`) are real extracted commitments, but
  every scorable field (`class`, `verification_score`, `baseline`, `target`,
  `deadline`, `unit`, `confidence`) is still `FILL`.
- **All five sources are secondary press reports**, with `reliability` unfilled and
  no archived URL. Every claim rests on exactly one of them, so **no claim is
  corroborated** under the project's own rule.
- **14 claims bundle three or more numeric targets into one row** (e.g. C003 mixes
  AMO coverage, doctors, and nurses). The methodology requires one claim per row.
- **Four parties have no 2026 claims at all**: RNI (the governing party), PAM, MP,
  and UC — so no incumbent-vs-challenger comparison is possible yet.

Run `make check` for the current, authoritative version of this list.

## File map

```
data/                  auditable CSVs (edit these, never the charts)
scripts/validate.py    the integrity gate: schema, references, enums, census
scripts/build.py       compute -> charts -> HTML -> audit trail
config.json            years, weights, gate thresholds
output/                generated artefacts (safe to delete; `make clean`)
things.txt             the original methodology note
morocco-elections.org  research skeleton (org-mode)
```

## Next steps for a publishable version

1. Close `data/claims.csv` in LibreOffice (the `.~lock.claims.csv#` file is why
   the validator warns), then fill `class` and `verification_score` — that alone
   unblocks **C** for every party.
2. Split the 14 merged claims into atomic rows (`C003a`, `C003b`, …) and fill
   `baseline` / `target` / `deadline` / `unit` so the killer table renders.
3. Add a second independent source per claim (an official manifesto PDF is
   both primary and corroborating) and give every source a reliability score
   and an archived URL.
4. Add the missing parties' 2026 programmes — RNI, PAM, MP, UC.
5. Re-source the `UNSOURCED` promise and timeline rows from Interior-Ministry
   and HCP documents.
6. Add 2026 rows to `data/indicators.csv` (justify each `G`/`L`/`M` in `basis`)
   and run the sensitivity analysis promised in `morocco-elections.org`.
7. Re-run `make strict` and re-read `output/audit_trail.md`.
