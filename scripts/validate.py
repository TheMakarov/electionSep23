#!/usr/bin/env python3
"""
Data validation gate for the Morocco electoral accountability project.

This is the referee for data/. It answers one question: *can every number in
the report be trusted and traced back to a source?* Nothing is written; the
script only reads data/ and reports.

It catches, in order of severity:

  ERROR   a broken invariant that makes the audit trail false or the build
          crash -- wrong header, duplicate key, reference to a source that
          does not exist, ragged row, out-of-range number, bad enum.
  WARN    incomplete but structurally sound -- FILL placeholders, explicit
          UNSOURCED rows, non-ISO dates, non-corroborated claims, claims
          that bundle several targets into one row.
  INFO    coverage notes -- e.g. "2026 claims exist but there is no 2026
          election or indicator row, so E/G/L/M cannot be computed".

Usage
-----
    python3 scripts/validate.py              # human report, exit 1 on ERROR
    python3 scripts/validate.py --strict     # WARN also fails the run
    python3 scripts/validate.py --json out.json
    python3 scripts/validate.py --quiet      # only the summary line

Exit codes: 0 = no errors, 1 = at least one ERROR (or any WARN with
--strict), 2 = the validator could not read the data at all.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

ERROR, WARN, INFO = "ERROR", "WARN", "INFO"
SEVERITY_ORDER = {ERROR: 0, WARN: 1, INFO: 2}

# ---------------------------------------------------------------------------
# Canonical schema. Every file listed here must exist with exactly this header.
# `ids` columns hold one or more source references.
# ---------------------------------------------------------------------------
SOURCE_ID_RE = re.compile(r"^S\d{3}$")
# An empty cell is a missing value, exactly like an explicit FILL placeholder.
FILL_TOKENS = {"", "FILL", "TBD", "TODO", "XXX", "N/A", "NA", "-", "?"}
UNSOURCED = "UNSOURCED"
SNAKE_CASE_RE = re.compile(r"^[a-z][a-z0-9_]*$")

SCHEMA = {
    "parties.csv": {
        "key": "party_id",
        "header": ["party_id", "name", "name_ar", "ideology", "founded",
                   "leader_2021", "leader_current", "color", "official_slogan"],
        "id_re": r"^P\d{3}$",
        # Not every party has a slogan, and P009 is a catch-all bucket rather
        # than a party, so its empty fields are not missing data.
        "optional": ["official_slogan"],
        "exclude_rows": {"party_id": {"P009"}},
    },
    "sources.csv": {
        "key": "source_id",
        "header": ["source_id", "title", "author_org", "date", "url", "type",
                   "primary_secondary", "reliability", "bias_lean", "bias_label",
                   "country", "election_year", "party_actor", "status", "notes"],
        "id_re": r"^S\d{3}$",
        "ids": [],
        "optional": ["notes"],
        "enum": {"primary_secondary": {"primary", "secondary"}},
        "number": {"reliability": (1, 5), "bias_lean": (-5, 5), "election_year": (1950, 2100)},
    },
    "claims.csv": {
        "key": "claim_id",
        "header": ["claim_id", "party_id", "election_year", "claim", "domain",
                   "baseline", "target", "deadline", "unit", "class",
                   "verification_score", "source_ids", "confidence",
                   # NOTE: the live column is "additional notes" (space). It is
                   # accepted so the gate stays green while data/claims.csv is
                   # open in LibreOffice; rename it to additional_notes in one
                   # commit once the file is closed. Flagged as NON_SNAKE_CASE.
                   "additional notes"],
        "id_re": r"^C\d{3}$",
        "ids": ["source_ids"],
        "fk": {"party_id": "parties.csv"},
        "optional": ["additional notes"],
        "enum": {"class": {"Q", "V", "S", "N"},
                 "confidence": {"HIGH", "MEDIUM", "LOW", "ILLUSTRATIVE"}},
        "number": {"verification_score": (1, 5), "election_year": (1950, 2100)},
    },
    "promises.csv": {
        "key": "promise_id",
        "header": ["promise_id", "party_id", "term_start", "term_end", "promise",
                   "domain", "status", "outcome_metric", "source_ids", "confidence"],
        "id_re": r"^PR\d{3}$",
        "ids": ["source_ids"],
        "fk": {"party_id": "parties.csv"},
        "enum": {"status": {"fulfilled", "partial", "failed", "abandoned",
                            "in_progress", "pending", "unverifiable"},
                 "confidence": {"HIGH", "MEDIUM", "LOW", "ILLUSTRATIVE"}},
        "number": {"term_start": (1950, 2100), "term_end": (1950, 2100)},
    },
    "elections.csv": {
        "key": ["election_year", "party_id"],
        "header": ["election_year", "party_id", "seats", "vote_share_est",
                   "turnout", "vote_confidence", "seats_confidence"],
        "ids": [],
        "fk": {"party_id": "parties.csv"},
        "enum": {"vote_confidence": {"HIGH", "MEDIUM", "LOW"},
                 "seats_confidence": {"HIGH", "MEDIUM", "LOW"}},
        "number": {"seats": (0, 1000), "vote_share_est": (0, 100),
                   "turnout": (0, 100), "election_year": (1950, 2100)},
    },
    "indicators.csv": {
        "key": ["party_id", "election_year"],
        "header": ["party_id", "election_year", "g_score", "l_score", "m_score", "basis"],
        "ids": [],
        "fk": {"party_id": "parties.csv"},
        "number": {"g_score": (0, 1), "l_score": (0, 1), "m_score": (0, 1),
                   "election_year": (1950, 2100)},
    },
    "leaders.csv": {
        "key": None,
        "header": ["leader", "party_id", "role", "tenure", "achievements",
                   "ownership", "reported_wealth", "ostensible_motive", "conflicts",
                   "skin_in_game", "source_ids", "confidence"],
        "ids": ["source_ids"],
        "fk": {"party_id": "parties.csv"},
        "enum": {"confidence": {"HIGH", "MEDIUM", "LOW", "ILLUSTRATIVE"}},
        "number": {"skin_in_game": (0, 1)},
    },
    "timeline.csv": {
        "key": None,
        "header": ["date", "event", "actor", "election_year", "significance", "source_ids"],
        "ids": ["source_ids"],
        "date_cols": ["date"],
        # A constitutional moment or a government formation has no election year.
        "optional": ["election_year"],
        "number": {"election_year": (1950, 2100)},
    },
    # The convergence layer: an explicit, auditable editorial classification.
    # themes.csv is the controlled vocabulary; claim_themes.csv is the mapping,
    # one row per (claim, theme) pair with a literal anchor from the claim text.
    "themes.csv": {
        "key": "theme_id",
        "header": ["theme_id", "label", "group", "definition"],
        "id_re": r"^[a-z][a-z0-9_]*$",
        "ids": [],
        "optional": ["definition"],
    },
    "claim_themes.csv": {
        "key": ["claim_id", "theme_id"],
        "header": ["claim_id", "theme_id", "evidence", "confidence"],
        "ids": [],
        "fk": {"claim_id": "claims.csv", "theme_id": "themes.csv"},
        "enum": {"confidence": {"HIGH", "MEDIUM", "LOW", "ILLUSTRATIVE"}},
        "optional": [],
    },
    # Verbatim legal provisions quoted in the report and the simulator. Each is
    # tied to a primary source so a reader can check the wording against the law.
    "legal_basis.csv": {
        "key": "ref_id",
        "header": ["ref_id", "instrument", "article", "quote_ar", "quote_en",
                   "source_id", "note"],
        "id_re": r"^LB\d{2}$",
        "ids": ["source_id"],
        "optional": ["note"],
    },
    # Official 2021 votes per map unit (province/prefecture) and party, parsed
    # from the per-constituency results PDF published after 8 Sept 2021.
    "results_2021.csv": {
        "key": ["map_unit", "party"],
        "header": ["map_unit", "region", "party", "votes", "local_seats"],
        "ids": [],
        "number": {"votes": (0, 10000000), "local_seats": (0, 30)},
    },
    # The 92 local and 12 regional constituencies that fill the 395 seats, with
    # their official seat counts (House of Representatives, 8 Sept 2021).
    "constituencies.csv": {
        "key": "constituency_id",
        "header": ["constituency_id", "level", "name", "prefecture", "map_unit",
                   "region", "seats", "source_id"],
        "id_re": r"^(LC\d{3}|RC\d{2})$",
        "ids": ["source_id"],
        "enum": {"level": {"local", "regional"}},
        "number": {"seats": (1, 100)},
    },
}

# Claims that bundle this many or more numeric targets should be split.
MERGED_CLAIM_THRESHOLD = 3
NUMERIC_TARGET_RE = re.compile(
    r"\d[\d\s.,]*\s?(?:%|MMDH|MDH|DH|millions?|milliards?|places|emplois|salles|"
    r"biblioth|terrains|classes|enseignants|médecins|infirmiers|logements|"
    r"hectares|m3|MW|km|startups?|entreprises|familles|ménages|étudiants)",
    re.IGNORECASE,
)
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
LOOSE_DATE_RE = re.compile(r"^\s*\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\s*$")


class Finding:
    __slots__ = ("severity", "code", "where", "message")

    def __init__(self, severity, code, where, message):
        self.severity = severity
        self.code = code
        self.where = where
        self.message = message

    def as_dict(self):
        return {"severity": self.severity, "code": self.code,
                "where": self.where, "message": self.message}


def is_fill(value) -> bool:
    return value is None or str(value).strip().upper() in FILL_TOKENS


def split_ids(raw) -> list[str]:
    """Split a source_ids cell into tokens on , ; / and whitespace."""
    if raw is None:
        return []
    return [t for t in re.split(r"[,;/|\s]+", str(raw)) if t]


def parse_number(raw):
    """Return a float, or None when the cell is a placeholder."""
    if is_fill(raw):
        return None
    text = str(raw).strip().replace("%", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def key_of(row, key):
    if key is None:
        return None
    if isinstance(key, str):
        return row.get(key, "")
    return tuple(row.get(k, "") for k in key)


def validate():
    findings: list[Finding] = []
    tables: dict[str, list[dict]] = {}
    raw_counts: dict[str, list[int]] = {}
    fill_census: dict[str, dict[str, int]] = {}
    row_counts: dict[str, int] = {}

    def err(code, where, msg):
        findings.append(Finding(ERROR, code, where, msg))

    def warn(code, where, msg):
        findings.append(Finding(WARN, code, where, msg))

    def info(code, where, msg):
        findings.append(Finding(INFO, code, where, msg))

    # -- pass 1: existence, header, ragged rows, load -------------------------
    for name, spec in SCHEMA.items():
        path = DATA / name
        if not path.exists():
            err("MISSING_FILE", name, "file does not exist")
            continue

        # A LibreOffice lock file means a human has this CSV open in Calc right
        # now; script edits will be clobbered when they save.
        if (DATA / f".~lock.{name}#").exists():
            warn("OPEN_IN_CALC", name,
                 "a LibreOffice lock file exists -- close the CSV in Calc before "
                 "editing it from a script, or the edit is lost on save")

        with open(path, newline="", encoding="utf-8") as fh:
            rows = list(csv.reader(fh))
        if not rows:
            err("EMPTY_FILE", name, "file has no rows")
            continue

        header = [h.strip() for h in rows[0]]
        for h in header:
            if not SNAKE_CASE_RE.match(h):
                suggestion = h.strip().replace(" ", "_").lower()
                warn("NON_SNAKE_CASE", name,
                     f"column {h!r} is not snake_case; rename to {suggestion!r} "
                     f"once the file is closed in Calc")
        if header != spec["header"]:
            unexpected = [h for h in header if h not in spec["header"]]
            missing = [h for h in spec["header"] if h not in header]
            err("HEADER_MISMATCH", name,
                f"header differs from schema; unexpected={unexpected or '-'} "
                f"missing={missing or '-'}")

        # Ragged rows are how unquoted commas silently destroy a cell.
        for i, raw in enumerate(rows[1:], start=2):
            if len(raw) != len(header):
                err("RAGGED_ROW", f"{name}:{i}",
                    f"{len(raw)} fields vs {len(header)} columns -- an unquoted "
                    f"comma or a missing column; the row will be silently truncated")

        with open(path, newline="", encoding="utf-8") as fh:
            loaded = list(csv.DictReader(fh))
        tables[name] = loaded
        row_counts[name] = len(loaded)
        raw_counts[name] = [len(r) for r in rows[1:]]

    if not tables:
        return findings, tables, fill_census, row_counts

    parties = {r.get("party_id") for r in tables.get("parties.csv", [])}
    sources = {r.get("source_id") for r in tables.get("sources.csv", [])}
    # Declared foreign keys, resolved from the tables that loaded successfully.
    fk_targets = {
        "parties.csv": parties,
        "sources.csv": sources,
        "claims.csv": {r.get("claim_id") for r in tables.get("claims.csv", [])},
        "themes.csv": {r.get("theme_id") for r in tables.get("themes.csv", [])},
    }

    # -- pass 2: per-file structural checks -----------------------------------
    for name, spec in SCHEMA.items():
        rows = tables.get(name)
        if rows is None:
            continue

        seen: dict = {}
        for i, row in enumerate(rows, start=2):
            where = f"{name}:{i}"

            # primary key: uniqueness + shape
            k = key_of(row, spec["key"])
            if k not in (None, "", ()):
                if k in seen:
                    err("DUPLICATE_KEY", where,
                        f"key {k!r} already used at line {seen[k]}")
                else:
                    seen[k] = i
            id_re = spec.get("id_re")
            if id_re and spec["key"] and isinstance(spec["key"], str):
                value = str(row.get(spec["key"], "")).strip()
                if value and not re.match(id_re, value):
                    err("BAD_ID_FORMAT", where,
                        f"{spec['key']}={value!r} does not match {id_re}")

            # foreign keys
            for col, target in spec.get("fk", {}).items():
                value = str(row.get(col, "")).strip()
                if value and value not in fk_targets.get(target, set()):
                    code = ("DANGLING_PARTY" if target == "parties.csv"
                            else "DANGLING_REF")
                    err(code, where,
                        f"{col}={value!r} is not declared in {target}")

            # enums
            for col, allowed in spec.get("enum", {}).items():
                value = str(row.get(col, "")).strip()
                if value and not is_fill(value) and value not in allowed:
                    err("BAD_ENUM", where,
                        f"{col}={value!r} not in {sorted(allowed)}")

            # numeric ranges
            for col, (lo, hi) in spec.get("number", {}).items():
                raw = row.get(col)
                if is_fill(raw):
                    continue
                value = parse_number(raw)
                if value is None:
                    err("NOT_NUMERIC", where, f"{col}={raw!r} is not a number")
                elif not (lo <= value <= hi):
                    err("OUT_OF_RANGE", where,
                        f"{col}={value} outside [{lo}, {hi}]")

            # dates
            for col in spec.get("date_cols", []):
                raw = str(row.get(col, "")).strip()
                if not raw or is_fill(raw):
                    continue
                if not ISO_DATE_RE.match(raw):
                    if LOOSE_DATE_RE.match(raw):
                        warn("NON_ISO_DATE", where,
                             f"{col}={raw!r} is ambiguous (dd/mm vs mm/dd); "
                             f"use ISO YYYY-MM-DD")
                    else:
                        err("BAD_DATE", where, f"{col}={raw!r} is not a date")

            # source references -- the core of the audit trail
            for col in spec.get("ids", []):
                raw = row.get(col, "")
                tokens = split_ids(raw)
                if not tokens:
                    warn("NO_SOURCE", where, f"{col} is empty")
                    continue
                if is_fill(raw):
                    warn("PLACEHOLDER_SOURCE", where,
                         f"{col}=FILL -- no source assigned yet")
                    continue
                for tok in tokens:
                    if tok == UNSOURCED:
                        warn("UNSOURCED", where,
                             f"{col} declares {UNSOURCED} -- row is knowingly "
                             f"unattributed and must not be published")
                    elif SOURCE_ID_RE.match(tok):
                        if tok not in sources:
                            err("DANGLING_SOURCE", where,
                                f"{col} references {tok}, which is not declared "
                                f"in sources.csv")
                    else:
                        err("BAD_SOURCE_TOKEN", where,
                            f"{col} contains {tok!r}, not an S### id or {UNSOURCED}")

            # merged-claim smell (the methodology forbids it)
            if name == "claims.csv":
                hits = NUMERIC_TARGET_RE.findall(str(row.get("claim", "")))
                if len(hits) >= MERGED_CLAIM_THRESHOLD:
                    warn("MERGED_CLAIM", where,
                         f"{len(hits)} numeric targets in one row; methodology "
                         f"requires one claim per row (split into Cxxxa/b/c)")

        # FILL census per column. Columns listed as `optional` are skipped: an
        # absent slogan or an absent note is not missing data. Rows matching
        # `exclude_rows` are dropped entirely (e.g. the P009 "Others" bucket).
        optional = set(spec.get("optional", []))
        exclude = spec.get("exclude_rows", {})
        counted = [r for r in rows if not any(
            str(r.get(col, "")).strip() in values
            for col, values in exclude.items())]
        census: dict[str, int] = {}
        for row in counted:
            for col in spec["header"]:
                if col in optional:
                    continue
                if is_fill(row.get(col)):
                    census[col] = census.get(col, 0) + 1
        fill_census[name] = census
        row_counts[name] = len(counted)
        for col, n in sorted(census.items(), key=lambda kv: -kv[1]):
            warn("FILL_CELLS", name,
                 f"{col}: {n}/{len(counted)} rows are still FILL")

    # -- pass 3: cross-file coverage -----------------------------------------
    claims = tables.get("claims.csv", [])
    elections = tables.get("elections.csv", [])
    indicators = tables.get("indicators.csv", [])

    claim_years = sorted({str(r.get("election_year", "")).strip()
                          for r in claims if not is_fill(r.get("election_year"))})
    election_years = sorted({str(r.get("election_year", "")).strip() for r in elections})
    indicator_years = sorted({str(r.get("election_year", "")).strip() for r in indicators})

    for year in claim_years:
        if year not in election_years:
            info("NO_ELECTION_ROW", "elections.csv",
                 f"claims exist for {year} but no elections.csv row -- "
                 f"electoral efficiency E cannot be computed")
        if year not in indicator_years:
            info("NO_INDICATOR_ROW", "indicators.csv",
                 f"claims exist for {year} but no indicators.csv row -- "
                 f"G/L/M cannot be computed")

    scorable = [r for r in claims if parse_number(r.get("verification_score")) is not None]
    if claims and not scorable:
        warn("NO_SCORABLE_CLAIMS", "claims.csv",
             f"0/{len(claims)} claims have a numeric verification_score, so "
             f"claim credibility C is 0.5 for every party unless fixed")

    claim_parties = {str(r.get("party_id", "")).strip() for r in claims}
    covering = sorted(parties - claim_parties - {"P009"})
    if covering:
        info("PARTIES_WITHOUT_CLAIMS", "claims.csv",
             f"no claims recorded for {covering} -- these parties cannot be "
             f"compared on claim credibility")

    have_sources = {r.get("party_id") for r in claims}
    del have_sources

    # -- theme coverage (the convergence layer) -------------------------------
    # A claim missing from claim_themes.csv is silently absent from the
    # convergence matrix, which would understate duplication. Say so.
    themes = tables.get("themes.csv", [])
    claim_themes = tables.get("claim_themes.csv", [])
    theme_ids = {r.get("theme_id") for r in themes}
    mapped: dict[str, set] = {}
    for r in claim_themes:
        cid = str(r.get("claim_id", "")).strip()
        tid = str(r.get("theme_id", "")).strip()
        if cid and tid:
            mapped.setdefault(cid, set()).add(tid)
    for r in claims:
        cid = str(r.get("claim_id", "")).strip()
        if cid and not mapped.get(cid):
            warn("CLAIM_WITHOUT_THEME", f"claims.csv:{cid}",
                 f"{cid} is not mapped to any theme in claim_themes.csv, so it "
                 f"is invisible to the convergence analysis")
    used = {t for ts in mapped.values() for t in ts}
    for tid in sorted(theme_ids - used):
        info("EMPTY_THEME", "claim_themes.csv",
             f"theme {tid!r} has no claim mapped to it")

    # -- pass 4: corroboration (the project's own rule) -----------------------
    for i, row in enumerate(claims, start=2):
        tokens = [t for t in split_ids(row.get("source_ids"))
                  if SOURCE_ID_RE.match(t)]
        unique = len(set(tokens))
        if unique == 1:
            warn("SINGLE_SOURCE", f"claims.csv:{i}",
                 f"{row.get('claim_id')} rests on a single source ({tokens[0]}); "
                 f"the project's rule requires 2+ independent sources to call a "
                 f"claim corroborated")

    return findings, tables, fill_census, row_counts


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def render(findings, fill_census, row_counts, use_color: bool):
    def paint(text, code):
        if not use_color:
            return text
        return f"\033[{code}m{text}\033[0m"

    sev_style = {ERROR: ("31;1", "ERROR"), WARN: ("33", " WARN"), INFO: ("36", " INFO")}

    print("Morocco electoral data -- validation gate")
    print(f"data dir: {DATA}")
    print("=" * 78)

    if not findings:
        print(paint("No findings. Data is structurally sound.", "32;1"))
    else:
        by_sev = {}
        for f in findings:
            by_sev.setdefault(f.severity, []).append(f)
        for sev in (ERROR, WARN, INFO):
            group = by_sev.get(sev, [])
            if not group:
                continue
            style, label = sev_style[sev]
            print()
            print(paint(f"[{label}] {len(group)} finding(s)", style))
            # collapse repetitive codes, show up to 6 examples each
            by_code = {}
            for f in group:
                by_code.setdefault(f.code, []).append(f)
            for code, items in by_code.items():
                print(f"  {code} ({len(items)})")
                for f in items[:6]:
                    print(f"    - {f.where}: {f.message}")
                if len(items) > 6:
                    print(f"    ... and {len(items) - 6} more")

    print()
    print("-" * 78)
    print("Completeness census (rows still marked FILL)")
    print("-" * 78)
    print(f"{'file':<18}{'rows':>6}  {'columns needing data'}")
    for name, census in fill_census.items():
        if not census:
            print(f"{name:<18}{row_counts.get(name, 0):>6}  -")
            continue
        cols = ", ".join(f"{c}({n})" for c, n in
                         sorted(census.items(), key=lambda kv: -kv[1])[:6])
        more = "" if len(census) <= 6 else f" +{len(census) - 6} more"
        print(f"{name:<18}{row_counts.get(name, 0):>6}  {cols}{more}")

    counts = {s: sum(1 for f in findings if f.severity == s) for s in (ERROR, WARN, INFO)}
    print()
    print("=" * 78)
    verdict = paint("FAIL", "31;1") if counts[ERROR] else (
        paint("PASS (with warnings)", "33;1") if counts[WARN] else paint("PASS", "32;1"))
    print(f"{verdict}  errors={counts[ERROR]} warnings={counts[WARN]} info={counts[INFO]}")
    return counts


def main(argv=None):
    ap = argparse.ArgumentParser(description="Validate the electoral data registry.")
    ap.add_argument("--strict", action="store_true",
                    help="treat warnings as failures (use in CI before publishing)")
    ap.add_argument("--json", metavar="PATH", help="also write findings as JSON")
    ap.add_argument("--quiet", action="store_true", help="print only the summary line")
    args = ap.parse_args(argv)

    try:
        findings, _tables, fill_census, row_counts = validate()
    except Exception as exc:  # pragma: no cover - defensive
        print(f"validator crashed: {exc!r}", file=sys.stderr)
        return 2

    if args.quiet:
        counts = {s: sum(1 for f in findings if f.severity == s)
                  for s in (ERROR, WARN, INFO)}
        print(f"validate: errors={counts[ERROR]} warnings={counts[WARN]} info={counts[INFO]}")
    else:
        counts = render(findings, fill_census, row_counts,
                        use_color=sys.stdout.isatty())

    if args.json:
        Path(args.json).write_text(json.dumps({
            "generated": dt.datetime.now().isoformat(timespec="seconds"),
            "findings": [f.as_dict() for f in findings],
            "fill_census": fill_census,
            "row_counts": row_counts,
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        if not args.quiet:
            print(f"[ok] JSON findings -> {args.json}")

    if counts[ERROR]:
        return 1
    if args.strict and counts[WARN]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
