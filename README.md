# Morocco — Electoral Claims, Promises & Accountability

A reproducible, Reuters-style **data journalism project** for Moroccan voters.
It turns raw claims, promises, and election results into a quantified
accountability score per party, with charts and a self-contained HTML report.

## Primary deliverable

Open **`output/report.html`** in any browser — it is fully self-contained
(all charts are inline SVG, no internet needed).

## Quick start

```bash
python3 scripts/build.py          # regenerate everything
# or
make                              # same thing
```

Requires Python 3.10+ with `numpy` and `matplotlib`.

## What you get

| File | What it is |
|---|---|
| `output/report.html` | The full report (key findings, killer table, CEAGI scores, 8 charts, promise ledger, leaders, sources, timeline, methodology) |
| `output/ceagi_scores.csv` | Party scores + sub-indices, machine-readable |
| `output/ceagi_scores.json` | Same, structured for apps/dashboards |
| `output/audit_trail.md` | Every number traced to a source ID + confidence |
| `output/charts/*.svg` | Individual vector charts |
| `morocco-elections.org` | The full research skeleton (org-mode) with methodology & algebra |

## How it works (3 layers)

1. **Sources** — `data/sources.csv`, one row per source, each with an ID (`S###`).
2. **Facts / Claims** — `data/claims.csv`, one row per claim (never per source).
3. **Synthesis** — `scripts/build.py` computes the CEAGI model and renders charts.

A claim is *corroborated* only with 2+ independent reliable sources.
Reliability and political lean are stored in **separate** columns.

## The CEAGI model (short)

```
S_p = ( D^0.25 · C^0.20 · E^0.10 · G^0.20 · L^0.15 · M^0.10 )
```

- **D** Delivery — promise fulfilment rate: `(fulfilled + 0.5·partial) / (fulfilled + partial + failed + abandoned)`
- **C** Claim credibility — claim taxonomy × 1–5 verification rubric
- **E** Electoral efficiency — `1 − |seat-share ÷ vote-share − 1| / max(…)`
- **G** Governance — coalition/portfolio weight, legislative output
- **L** Leadership — integrity, conflicts, skin-in-the-game
- **M** Mandate coherence — manifesto vs. action alignment

Weighted **geometric** mean (a zero in any dimension cannot be fully
compensated). Uncertainty is a 8,000-draw Monte-Carlo pass (σ = 0.05 per
sub-index); the report shows 95% credible intervals.

## Data provenance & caveats (read before publishing)

- **Seat counts** (2011/2016/2021) are from official election records → **HIGH** confidence.
- **Vote shares** are **ESTIMATES** (marked `LOW`) — replace with official
  Interior-Ministry figures before publication.
- **Claims / promises / leaders** rows are **realistic placeholders** to
  demonstrate the pipeline (marked `ILLUSTRATIVE` or `FILL`). Verify against the
  cited sources and fill the `FILL` cells.
- Every number maps to a `source_id`; see `output/audit_trail.md`.

## File map

```
data/            auditable CSVs (edit these, not the charts)
scripts/build.py the whole pipeline: compute → charts → HTML → audit trail
output/          generated artefacts (safe to delete and regenerate)
things.txt       the original methodology note
morocco-elections.org   research skeleton (org-mode)
```

## Next steps for a publishable version

1. Fill every `FILL` cell in `data/` with sourced values.
2. Replace vote-share estimates with official figures.
3. Add an archived URL per source.
4. Justify the assigned `G/L/M` indicator scores in `data/indicators.csv`.
5. Re-run `make` and re-read `output/audit_trail.md`.
