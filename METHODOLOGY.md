# Methodology — Metrics, Policies & Standards

This is the single authoritative reference for **how this project measures and
judges electoral claims**. Every rule below is either enforced by code
(`scripts/validate.py`, `scripts/build.py`), configured in `config.json`, or
stated in the research skeleton (`morocco-elections.org`, `things.txt`). The
section "Where each rule lives" maps every rule to its source of truth.

---

## 1. Guiding principles

1. **Never fabricate.** A number is recorded only if a source says so. A value
   that cannot be computed is reported as `n/a` with a reason — it is never
   imputed, never defaulted to `0.5`, and never invented.
2. **Every number is traceable.** Each claim, promise and indicator points to a
   `source_id` (`S###`). No anonymous numbers.
3. **Reliability and bias are separate.** Whether a source is *trustworthy* and
   which *side* it leans toward are two different questions and are stored in
   two different columns.
4. **Corroborate before believing.** A claim is treated as *corroborated* only
   when 2+ independent reliable sources support it. One source = single-source,
   never "established".
5. **Fail loud, not silent.** The validator exits non-zero on a broken
   invariant; the builder refuses to run on errors; the report prints a
   `NOT PUBLISHABLE` banner when the publication gate is not met.

---

## 2. The three-layer model

From `things.txt`, all data is organised in three layers:

| Layer | File | Rule |
|---|---|---|
| **Sources** | `data/sources.csv` | one row per source, each with an ID (`S###`) |
| **Facts / Claims** | `data/claims.csv` | one row per claim, never per source |
| **Synthesis** | `scripts/build.py` | CEAGI model + charts + report |

A claim is *not* the same as a source: one source can back many claims, and a
claim that is worth its salt is backed by several independent sources.

---

## 3. Source registry & reliability

`data/sources.csv` — one row per source.

| Field | Meaning | Values |
|---|---|---|
| `source_id` | stable key, regex `^S\d{3}$` | `S001`, `S002`, … |
| `primary_secondary` | is this the original document? | `primary` \| `secondary` |
| `reliability` | trustworthiness of the source, 1–5 | see scale below |
| `bias_lean` | left–right lean, kept separate from reliability | number −5…+5 |
| `bias_label` | free-text description of the lean | e.g. `Center`, `Official (state agency)`, `Party manifesto (primary)` |
| `status` | review state | `to read` \| `read` \| `verified` |

### Reliability scale (1–5)

| Score | Meaning | Typical examples |
|---|---|---|
| **5** | Official primary dataset / legal text | HCP statistics, Interior-Ministry results, `Bulletin Officiel` decrees |
| **4** | Official state agency, or a primary party document | MAP / MAP Express, a party's own manifesto |
| **3** | Established independent outlet | Hespress, LesEco, Le Matin, Le360, H24info, L'Opinion, 2M |
| **2** | Aggregator / weak editorial control / anonymous | unnamed "sources", reposts |
| **1** | Unverified, rumour, social media | — |

### Bias, kept separate

`bias_lean` is a coarse numeric axis (−5 = far left … +5 = far right; `0` =
neutral/centre). Moroccan politics does not map cleanly onto a single
left–right axis (Islamist/conservative, left, liberal, and rural/Amazigh
cleavages overlap), which is why `bias_label` is free text. **A high
`reliability` does not imply low bias** — an official agency is reliable but
may still be institutionally aligned; a party manifesto is authoritative about
*what the party promised* while being maximally partisan.

---

## 4. Corroboration policy

**The rule (from `things.txt`):** a claim is *corroborated* only when it is
supported by **2+ independent reliable sources**.

- **Independent** = different organisations (different outlets, or an outlet
  plus the primary document). Two articles from the same outlet are *one*
  source, not two.
- **Reliable** = `reliability >= 3` unless a stronger primary document exists.

### Statuses

| Status | Definition |
|---|---|
| **Corroborated** | 2+ independent reliable sources agree |
| **Single-source** | exactly one source |
| **Disputed** | sources disagree (requires a counter-evidence field) |
| **False** | refuted by a more authoritative source |

### How the pipeline computes it

In `scripts/build.py`, a campaign claim is *corroborated* when its `source_ids`
cell contains **2 or more distinct `S###` tokens**:

```python
len({token for token in split_ids(source_ids) if SOURCE_ID_RE.match(token)}) >= 2
```

`scripts/validate.py` emits a `SINGLE_SOURCE` warning for every claim resting on
one source, and the publication gate (section 11) requires at least half of all
campaign claims to be corroborated before anything is publishable.

---

## 5. Claim taxonomy (Q / V / S / N)

Every claim is classified before it is scored. The class is stored in
`data/claims.csv` `class` and determines how much the claim is worth in the
credibility score.

| Class | Definition | Example | Weight |
|---|---|---|---|
| **Q** | Quantified target (number + unit + deadline) | "porter le SMIG à 5 000 DH" | **1.0** |
| **V** | Vague but directional | "améliorer la santé" | **0.4** |
| **S** | Slogan / identity claim | "Morocco first" | **0.1** |
| **N** | Negative / anti-claim (attack) | "ils ont échoué sur X" | **0.2** |

`default_claim_weight = 0.4` is applied if a claim somehow has no class.
These weights are in `config.json` (`claim_weights`).

### One claim per row

The methodology forbids bundling several targets into one row. The validator
flags any claim containing **3 or more numeric targets** as `MERGED_CLAIM`
(threshold `MERGED_CLAIM_THRESHOLD = 3`) with a warning to split it into
`C###a`, `C###b`, …

---

## 6. Claim verification rubric (1–5)

The `verification_score` in `data/claims.csv` grades *how well a claim can be
audited*, following a six-point protocol (from `morocco-elections.org`
section 5): baseline verification → attribution/ownership → feasibility →
counter-evidence search → triangulation (2+ sources) → output scoring.

| Score | Label | Meaning |
|---|---|---|
| **1** | Unverifiable | no baseline, no unit, no deadline |
| **2** | Weak | baseline or deadline missing; single source |
| **3** | Moderate | complete but single-source or contested |
| **4** | Strong | complete, corroborated, feasible |
| **5** | Audited | independently verified by a neutral body |

The credibility sub-index **C** (section 9) is the weighted average of this
score, normalised to [0,1]:

```
C_p = Σ (w_k · s_k) / (5 · Σ w_k)
```

where `s_k ∈ {1..5}` is the rubric score and `w_k` is the class weight of claim
`k`.

### How the rubric is applied mechanically today

As long as `baseline` is `FILL`, no claim can score above 2, because the rubric
ties "weak" to a missing baseline/deadline and "moderate/strong" to
*completeness* (baseline + unit + deadline). The current deterministic rule is:

| Class | Score | Why |
|---|---|---|
| **Q** (quantified) | **2** — weak | has a target/unit, but single source and/or missing baseline |
| **V / S / N** | **1** — unverifiable | no numeric target, unit or deadline |

This is deliberately conservative. Filling `baseline` from official statistics
(HCP, ministries) and adding corroborating sources is what moves a claim to
3 (complete + single-source) or 4 (complete + corroborated + feasible).

---

## 7. Confidence levels

Stored in `data/claims.csv` `confidence` (and elsewhere). Allowed values:
`HIGH`, `MEDIUM`, `LOW`, `ILLUSTRATIVE`.

| Level | Meaning |
|---|---|
| **HIGH** | official/verified, multiple corroborating sources |
| **MEDIUM** | one solid source |
| **LOW** | estimate or headline-only capture, to be verified |
| **ILLUSTRATIVE** | placeholder demonstrating the pipeline — never cite |

---

## 8. The CEAGI model

**CEAGI** = *Composite Electoral Accountability & Governance Index*. Six
sub-indices, each normalised to [0,1], combined by a **weighted geometric
mean** so that a total failure in one dimension cannot be compensated by a
strong show in another.

### Sub-indices

| Code | Name | Formula / source |
|---|---|---|
| **D** | Delivery | `(F + 0.5·P) / (F + P + X + A)` over the **2021** promises (F=fulfilled, P=partial, X=failed, A=abandoned). Requires `min_promises_for_D = 3` concluded promises. Statuses are set only where a source backs them; `unverifiable`/`in_progress` are excluded, never guessed. |
| **C** | Promise credibility (2021) | `Σ(w·s) / (5·Σw)` over the 2021 promises (see section 6). |
| **E** | Electoral efficiency | `1 − |A_p − 1| / max_j |A_j − 1|`, where `A_p = (s_p/S) ÷ (v_p/V)` (seat-share ÷ vote-share). |
| **G** | Governance | 0–1 indicator from `data/indicators.csv` (coalition/portfolio weight, legislative output). |
| **L** | Leadership | 0–1 indicator (integrity, conflicts, skin-in-the-game). |
| **M** | Mandate coherence | 0–1 indicator (manifesto vs. action alignment). |

### Aggregation

```
S_p = exp( Σ w_k · ln(max(x_k, ε)) / Σ w_k ),   ε = 1e-6
```

Weights (`composite_weights` in `config.json`):

| D | C | E | G | L | M |
|---|---|---|---|---|---|
| 0.25 | 0.20 | 0.10 | 0.20 | 0.15 | 0.10 |

### Uncertainty

An 8 000-draw Monte-Carlo pass (`n_samples = 8000`, `seed = 42`): each
sub-index is sampled from `Normal(μ, σ=0.05)`, clipped to `[1e-3, 1]`, the
geometric mean is recomputed, and the **2.5th / 50th / 97.5th percentiles** are
reported as the 95% credible interval.

### The no-imputation rule

A full `S_p` is published **only when all six sub-indices exist**. If a
sub-index has no evidence it is reported as `n/a` with its reason (sample size,
year, detail), and the party is *not ranked*. A "provisional" average over the
available dimensions is shown for diagnosis only and is explicitly **never
ranked**, because averaging over different dimension sets makes parties
incomparable.

### Two years, not one

`campaign_year` (2026) feeds **C**; `baseline_year` (2021) feeds **E**, **G**,
**L**, **M**; **D** is computed over *completed* terms only. This is deliberate:
delivery is a property of a finished term, credibility of the current campaign,
efficiency of the last election that actually happened.

---

## 9. Promise-status → numeric map

Used by the promise-ledger heatmap (`config.json` `promise_status_numeric`):

| Status | Value |
|---|---|
| `fulfilled` | 1.00 |
| `partial` | 0.66 |
| `in_progress` | 0.55 |
| `pending` | 0.33 |
| `unverifiable` | 0.25 |
| `failed` | 0.00 |
| `abandoned` | 0.00 |

Allowed status vocabulary (enforced by the validator): `fulfilled`, `partial`,
`failed`, `abandoned`, `in_progress`, `pending`, `unverifiable`.

---

## 10. Electoral distortion metrics

From `morocco-elections.org` section 6 (votes → seats algebra). `house_seats =
395`.

| Metric | Formula | Meaning |
|---|---|---|
| **Advantage ratio** | `A_p = (s_p/S) ÷ (v_p/V)` | `>1` over-represented, `<1` under-represented |
| **Gallagher (least squares)** | `LSq = √(½·Σ (v_i/V − s_i/S)²)` | disproportionality |
| **Loosemore–Hanby** | `LH = ½·Σ |v_i/V − s_i/S|` | max-deviation index |
| **Effective number of parties** | `N_votes = 1/Σ(v_i/V)²`, `N_seats = 1/Σ(s_i/S)²` | fragmentation |
| **Wasted votes** | votes below threshold + surplus above quota | — |

Seat allocation reference (2021): Hare quota + largest remainder, 3% threshold,
92 local constituencies + 90 national-list seats (60 women, 30 under-40).

---

## 11. Publication gate

The report is `PUBLISHABLE` only when **all** of these hold
(`config.json` `publication_gate`):

| Gate | Threshold | What it measures |
|---|---|---|
| No structural errors | 0 `ERROR`s | validator invariant failures |
| Fill fraction | `<= 0.10` | `FILL` cells ÷ total cells |
| Claims scored | `>= 0.50` | campaign claims with a numeric `verification_score` |
| Claims corroborated | `>= 0.50` | campaign claims with 2+ distinct `S###` sources |
| All six sub-indices | ≥ 1 party fully scored | the model is actually computable |

Currently the project is **NOT PUBLISHABLE** (claims are recorded but unscored
and mostly single-source) — by design, not by accident.

---

## 12. Claim convergence & duplication (themes layer)

Parties do not have to use the same words to make the same promise. The
convergence layer answers a different question from CEAGI: **which parties are
promising the same things?** It is a descriptive lens, not a score, and it is
deliberately kept out of the composite.

### Controlled vocabulary and mapping

| File | Role |
|---|---|
| `data/themes.csv` | the controlled vocabulary: `theme_id`, `label`, `group`, `definition` |
| `data/claim_themes.csv` | the mapping, one row per `(claim_id, theme_id)` pair, with the literal `evidence` anchor and a `confidence` |

The mapping is an **editorial judgement recorded as data**, not a keyword
heuristic hidden in the builder. Every row must quote a phrase that actually
appears in the claim text (or its `domain` label); `scripts/build.py` asserts
this when the file is generated, and the validator treats a claim with no theme
as a `CLAIM_WITHOUT_THEME` warning so an omission cannot silently understate
duplication. Change the mapping and the convergence picture changes; nothing
about it is inferred at build time.

### Definitions

- **Shared theme** — a theme claimed by two or more parties in the campaign
  year. **Exclusive / alone** — claimed by exactly one.
- **Duplication index** — `shared themes ÷ themes in play`. A property of the
  campaign, not of a party.
- **Convergence (per party)** — `shared themes ÷ own themes`. High means a
  crowded, undifferentiated platform; low means a distinctive one. Because it
  is a ratio it does not reward a party merely for having more claims.
- **Jaccard overlap (per pair)** — `|A ∩ B| ÷ |A ∪ B|` over each party's theme
  sets, reported beside the raw shared count so a large programme does not look
  more convergent than it is.
- **Carried-over theme** — a theme the same party also ran on in the previous
  campaign. A promise repeated is not a promise added.

### Presentation

The report's section 9 shows the theme × party matrix as a bubble grid (area ∝
number of claims on that theme; gold banding marks shared themes), a
duplication ledger with the claim IDs on each side, a per-party convergence
table, and a party × party echo matrix. The lede spotlight theme is an explicit
setting, `convergence.spotlight_theme` in `config.json` (it falls back to the
most crowded theme when unset). The same matrices are exported as
`output/charts/claim_convergence_matrix.svg` and
`output/charts/party_echo_matrix.svg`; the underlying data is written to
`output/claim_overlap.csv` and `output/claim_overlap.json`.

### Limits

Themes are coarse: two claims can share a theme and still differ sharply in
ambition, target or mechanism. Convergence therefore measures **agenda overlap,
not agreement**, and says nothing about whether either party would deliver. A
claim that bundles several targets touches several themes, so theme-tagged
claims exceed claim rows.

---

## 13. Data-hygiene conventions

Enforced by `scripts/validate.py`.

| Convention | Rule |
|---|---|
| **Missing value** | `FILL`, `TBD`, `TODO`, `XXX`, `N/A`, `NA`, `-`, `?`, or an empty cell |
| **Knowingly unsourced** | the sentinel `UNSOURCED` (greppable; counted; must not be published) |
| **Source IDs** | `^S\d{3}$`; any other token in a `source_ids` cell is an `ERROR` |
| **Party / claim / promise IDs** | `^P\d{3}$`, `^C\d{3}$`, `^PR\d{3}$` |
| **Ragged row** | any row whose field count ≠ header count → `ERROR` (the signature of an unquoted comma silently truncating a cell) |
| **Duplicates** | duplicate primary keys → `ERROR` |
| **Enums** | `class ∈ {Q,V,S,N}`, `confidence ∈ {HIGH,MEDIUM,LOW,ILLUSTRATIVE}`, `primary_secondary ∈ {primary,secondary}`, etc. |
| **Numeric ranges** | `reliability ∈ [1,5]`, `bias_lean ∈ [-5,5]`, `verification_score ∈ [1,5]`, `g/l/m_score ∈ [0,1]`, `skin_in_game ∈ [0,1]`, `seats ∈ [0,1000]`, `vote_share ∈ [0,100]`, `turnout ∈ [0,100]` |
| **Dates** | timeline dates must be ISO `YYYY-MM-DD` (loose `dd/mm/yy` is a warning) |
| **Column names** | snake_case; a space or uppercase is a `NON_SNAKE_CASE` warning |
| **Optional columns** | `official_slogan`, `notes`, `additional notes`, `timeline.election_year` are not counted as missing |
| **LibreOffice locks** | a `.~lock.<file>#` in `data/` warns that a human has the CSV open and a script edit will be lost on save |

The `P009` "Others" bucket is excluded from the party census.

---

## 14. External standards this project is aligned with

The policies above are this project's own, but they follow widely accepted
fact-checking and sourcing practice:

- **IFCN Code of Principles** (International Fact-Checking Network, Poynter) —
  the five commitments to *non-partisanship, transparency of sources,
  transparency of funding, transparency of methodology, and corrections* map
  directly onto principles 2–5 above. [IFCN commitments](https://ifcncodeofprinciples.poynter.org/the-commitments) · [code, via Accountable Journalism](https://accountablejournalism.org/ethics-codes/international-fact-checking-network-fact-checkers-code-of-principles)
- **AFP — 20 Principles of Sourcing** — the rule that a fact should be
  confirmed by independent sources before publication underlies the
  corroboration policy. [AFP principles of sourcing (PDF)](https://www.afp.com/sites/default/files/2025-11/EN-20-principles-of-sourcing.pdf)
- **Source-reliability rating scales** (e.g. Media Bias/Fact Check) informed the
  reliability/bias split — with the caveat that such scales are themselves
  contested in research (see [Stop using Media Bias/Fact Check in research](https://browse-export.arxiv.org/pdf/2607.12108)). This project therefore stores
  reliability and bias **separately** and treats them as internal judgements,
  not imported ratings.

---

## 15. Where each rule lives

| Rule | Source of truth |
|---|---|
| Years, weights, gate thresholds, Monte-Carlo, promise-status map | `config.json` |
| Schema, IDs, enums, numeric ranges, FILL/UNSOURCED, merged-claim detection | `scripts/validate.py` |
| CEAGI formulas, corroboration computation, no-imputation logic, convergence metrics | `scripts/build.py` |
| Theme vocabulary and claim-to-theme mapping | `data/themes.csv`, `data/claim_themes.csv` |
| Presentation: flag palette, pentagram star, zellige watermark | `scripts/moroccan_theme.py` |
| Verbatim legal provisions quoted in the report | `data/legal_basis.csv` |
| Constituency names, seat counts and map units | `data/constituencies.csv` |
| Simplified province / region geometry for the map | `data/morocco_map.json` |
| Official 2021 votes per province and party | `data/results_2021.csv` |
| Claim taxonomy, rubric, six-point protocol, electoral algebra, distortion metrics | `morocco-elections.org` |
| Three-layer model, corroboration rule, source/claims schema templates | `things.txt` |
| Human-readable summary of all of the above | this file + `README.md` |

---

## 16. The electoral law this project relies on

The seat arithmetic is not a convention: it is fixed by Morocco's organic law.
The report quotes the operative articles verbatim in `data/legal_basis.csv` and
renders them in report section 6.

| Instrument | Reference |
|---|---|
| **Loi organique n° 27.11** relative à la Chambre des représentants | Original: dahir n° 1.11.165 of 14 October 2011; consolidated text dated 29 January 2026 |
| Amended by **loi organique n° 53.25** | Dahir n° 1.25.70 of 16 January 2026, BO n° 7478 of 29 January 2026, p. 785 |
| Amended by **loi organique n° 04.21** | Dahir n° 1.21.39 of 21 April 2021, BO n° 6987 of 17 May 2021, p. 3405 |
| Amended by **loi organique n° 20.16** | Dahir n° 1.16.118 of 10 August 2016, BO n° 6490 |

What the law says (quoted in `data/legal_basis.csv`):

- **Article 1** - the House has **395 members**: **305** elected in local
  constituencies and **90** in regional constituencies (a fixed table across the
  twelve regions). The election is by **proportional representation on the
  largest-remainder rule** (*plus fort reste*), without panachage or preferential
  voting.
- **Article 84** - seats are allocated by an **electoral quotient** equal to the
  **registered voters** in the constituency divided by the number of seats
  allocated to it; the remaining seats go to the **largest remainders**.
- **Article 85** applies the same method to the regional constituencies.
- There is **no electoral threshold**: the 3%/6% threshold in force from 2002 to
  2016 was removed by the 2021 reform.

Consequence for reading this report: because the quotient is computed on
*registered* voters rather than votes cast, few lists reach it and most seats are
decided at the largest-remainder step. The seat simulator illustrates the method
on national vote totals and says so on the page; the real count runs constituency
by constituency.

The interactive simulator (`scripts/build_simulator.py`) now applies the
method constituency by constituency rather than at national level: seats are
allocated inside each of the 75 map units (305 local seats) and inside each of
the twelve regional constituencies (90 seats), then summed into the 395-seat
hemicycle. The map geometry is simplified from **geoBoundaries** MAR ADM2/ADM1
  (OpenStreetMap / Wambacher, ODbL 1.0; `geoBoundaries-MAR-ADM2.geojson` and
`-ADM1.geojson` from the geoBoundaries gbOpen release); the seat counts are the
official 2021 table published by the House of Representatives. Where the law splits a prefecture into several local constituencies
(Casablanca, Fès, Rabat, Salé, Kénitra, Khémisset, Azilal, Taounate), the map
shows the prefecture once with the seats added together — the page says so.

### The register and the quotient

The simulator now takes the **registered voters** of a constituency as an
input, because Article 84 divides the register by the seats to obtain the
quotient. Each map unit carries a register field; the default is derived from
the votes cast in the scenario divided by a national **turnout** assumption
(50.2% for 2021), and it can be overridden per constituency. The page shows the
resulting `Q` and how many lists reach it — usually few, which is exactly why
so many seats are decided at the largest-remainder step.

`data/results_2021.csv` holds the real 2021 votes per province/prefecture and
party, parsed from the official per-constituency results
(`data/resultats_legislatives_2021_details_et_nombre_de_voix.pdf`): 1,372
candidate rows, 92 local constituencies, 7.41M votes. Four of the 305 local
seats could not be traced in the source and are flagged; the small parties
(MDS, FFD, CNI, PSU) are carried in the registry's `Others` bucket.
