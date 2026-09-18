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
`NOT PUBLISHABLE` on one gate only: the registry is 10.4% `FILL` cells, just
above the 10% ceiling (`class`/`verification_score` are now scored for all 61
campaign claims and 69% are corroborated; the remaining holes are mostly
`baseline` / `deadline` / `unit`).

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
| `output/report.html` | The full report: publication gate, supportable vs. blocked findings, an auto-generated to-do list, readiness and sub-index tables, charts, the campaign claims table, **claim convergence** (bubble matrix, duplication ledger, party echo matrix), promise ledger, leaders, sources, timeline, validation findings, methodology |
| `output/seat_simulator.html` | **Interactive** seat-allocation simulator: enter votes, see the Hare-quota → hemicycle (coloured seats) + interpretation, plus the CEAGI reference |
| `output/ceagi_scores.csv` | Sub-indices + coverage per party, machine-readable |
| `output/ceagi_scores.json` | Same, plus per-sub-index provenance (`n`, `year`, `detail`) and the gate results |
| `output/claim_overlap.csv` / `.json` | Which parties promise the same things: themes in play, shared themes, per-party convergence, carried-over themes, pairwise Jaccard overlap |
| `output/audit_trail.md` | Every number traced to a source ID, a sample size, a year, and a reason when absent |
| `output/charts/*.svg` | Individual vector charts (stale ones are deleted on each build) |
| `config.json` | Years, weights, gate thresholds, convergence spotlight — the only place to change the model |
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
  source, claims that bundle several numeric targets into one row, a claim with
  no theme mapping (`CLAIM_WITHOUT_THEME`, which would silently understate
  duplication), non-ISO dates, a CSV left open in LibreOffice.
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

### The convergence layer (who is copying whom)

Parties rarely use the same words, but they often make the same promise. Every
campaign claim is mapped to one or more policy themes in
**`data/claim_themes.csv`**, drawn from the controlled vocabulary in
**`data/themes.csv`**. Each mapping row quotes the phrase in the claim that
justifies the tag, so the duplication finding is auditable rather than a
keyword guess. Section 7 of the report renders the result as a bubble matrix
(area ∝ claims, gold banding = shared theme), a duplication ledger with claim
IDs, and a party × party echo matrix; the machine-readable form is
`output/claim_overlap.csv` / `.json`.

Two numbers come out of it: the campaign's **duplication index** (shared themes
÷ themes in play) and each party's **convergence** (shared themes ÷ own themes),
plus **carried-over themes** — the same party running again on a theme it
already campaigned on in 2021.

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

The full reference — every metric, scale, formula and threshold, with the
external standards they follow — is in **[`METHODOLOGY.md`](METHODOLOGY.md)**.

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
- **The 2026 campaign claims** (61 rows) are extracted commitments. `class` and
  `verification_score` are now filled for all of them, but `baseline` (96/96),
  `deadline` (87/96), `target` (39/96) and `unit` (40/96) are still largely
  `FILL`, so most claims cannot yet be checked against an outcome.
- **The 60 sources are almost all secondary press reports** (59 secondary, 1
  primary); 5 have no `reliability` score and 6 no `bias_lean`. 19 of the 61
  campaign claims still rest on a single source, so they are **not
  corroborated** under the project's own rule.
- **15 claims bundle three or more numeric targets into one row** (e.g. C003 mixes
  AMO coverage, doctors, and nurses). The methodology requires one claim per row.
- **Convergence mapping is editorial.** `data/claim_themes.csv` is a human
  classification with an evidence anchor per row, not a measured fact; read
  METHODOLOGY.md section 12 before quoting a "duplication" figure.

Run `make check` for the current, authoritative version of this list.

## File map

```
data/                  auditable CSVs (edit these, never the charts)
data/themes.csv        controlled vocabulary for the convergence layer
data/claim_themes.csv  claim -> theme mapping, each row with its evidence anchor
scripts/validate.py    the integrity gate: schema, references, enums, census
scripts/build.py       compute -> charts -> HTML -> audit trail -> overlap export
scripts/build_simulator.py  the interactive seat simulator
scripts/moroccan_theme.py   shared flag palette, pentagram star, zellige tile
config.json            years, weights, gate thresholds
METHODOLOGY.md         the full reference: every metric, formula & policy
output/                generated artefacts (safe to delete; `make clean`)
things.txt             the original methodology note
morocco-elections.org  research skeleton (org-mode)
```

Both HTML deliverables carry one Moroccan identity, defined once in
`scripts/moroccan_theme.py`: the flag palette (`#c1272d` red, `#006233` green,
brass `#c8a24a`), the flag's interlaced pentagram as the masthead badge and the
section markers, and an eight-point zellige tile as the masthead watermark.
Change it there and the report and the simulator both follow.

## Next steps for a publishable version

1. Fill the remaining `FILL` cells (mostly `baseline`, `deadline`, `target`,
   `unit`) to push the registry below the 10% fill ceiling — that is the last
   gate keeping the report `NOT PUBLISHABLE`.
2. Split the 15 merged claims into atomic rows (`C003a`, `C003b`, …) and fill
   `baseline` / `target` / `deadline` / `unit` so the killer table renders.
3. Add a second independent source to the 19 single-source campaign claims (an
   official manifesto PDF is both primary and corroborating) and give the 5
   sources without a `reliability` score one.
4. Extend the convergence layer as new programmes land: add a row to
   `data/themes.csv` rather than inventing a one-off theme, and re-run `make`.
5. Re-source the `UNSOURCED` promise and timeline rows from Interior-Ministry
   and HCP documents.
6. Add 2026 rows to `data/indicators.csv` (justify each `G`/`L`/`M` in `basis`)
   and run the sensitivity analysis promised in `morocco-elections.org`.
7. Re-run `make strict` and re-read `output/audit_trail.md`.
