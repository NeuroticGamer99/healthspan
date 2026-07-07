# ADR-0005: Reference Range Frameworks

## Status
Proposed

## Context and Problem Statement
Lab results have a single reference range from the reporting lab, stored per result row (see design-rationale.md). However, there are multiple clinically meaningful ways to interpret whether a value is optimal:

- **Lab standard range** — what the reporting lab flags as abnormal; already captured per result row
- **Longevity-optimized ranges** — more restrictive targets used by longevity-focused platforms (e.g. Function Health's "healthy lifespan" ranges)
- **Practitioner-specific optimal ranges** — targets published or recommended by specific clinicians (e.g. Dr. Peter Attia, Dr. Ford Brewer, Dr. Mark Hyman)

The current schema answers "was this result flagged by the lab?" but cannot answer "is this result in Dr. Attia's optimal range for this biomarker?" without additional schema support.

## Decision Drivers
- Analytical questions frequently involve comparing results to optimal targets, not just lab flags
- Different frameworks use different thresholds for the same biomarker (e.g. optimal LDL-P varies significantly between standard care and longevity medicine)
- Frameworks evolve — a practitioner may update their targets over time
- The lab range on a result row is a historical fact (what the lab reported); optimal ranges are a separate, queryable reference dataset
- New frameworks should be addable without schema changes

## Considered Options
- No framework table — embed optimal ranges as hardcoded query parameters
- Named framework table with per-biomarker range rows
- Named framework table with versioning (effective date per range entry)

## Decision Outcome
Chosen option: **Named framework table with per-biomarker range rows (option 2)**, with a mandatory unit on every range row.

Option 2 makes frameworks first-class, queryable, and extensible as data additions rather than schema changes — the analytical requirement that motivated this ADR. Full versioning (option 3) is not adopted as a distinct model because the `effective_date` column below already provides it for free the moment a framework's targets are dated: a `NULL` `effective_date` means "always current," and a populated one enables point-in-time lookup, without committing every framework to dated maintenance up front.

**Safety correction (review item 3.D):** the original sketch stored `range_low`/`range_high` with no unit. That is unsafe — an Attia ApoB target in mg/dL compared against a result in g/L silently produces garbage flags (a factor-of-100 error). Every range row therefore carries a **mandatory `unit`** as a UCUM string ([ADR-0031](0031-units-and-ucum.md)), and comparison must **unit-normalize** both the result and the range to the biomarker's `canonical_unit` ([ADR-0030](0030-biomarker-identity.md)) before flagging. A comparison whose units cannot be reconciled must fail loudly, never silently flag. Comparison must also respect the result value model — a censored (`comparator` non-NULL) or qualitative (`value_num IS NULL`) result is not a plain number (see [ADR-0030](0030-biomarker-identity.md)).

### Positive Consequences
- Frameworks are first-class, queryable entities; new practitioners/guidelines are data additions, not schema changes
- Point-in-time framework lookups are available via `effective_date` without a separate versioning model
- Unit-normalized comparison closes the mg/dL-vs-g/L class of silent mis-flagging

### Negative Consequences / Tradeoffs
- Comparison requires a join to the framework range and a unit normalization step rather than a bare numeric comparison
- Framework range data must be curated and entered, including a correct UCUM unit per row

## Pros and Cons of the Options

### No framework table — hardcoded query parameters
- Pro: No additional schema; frameworks live in query logic or MCP tool parameters
- Con: Ranges are not persistent or queryable — must be maintained in code or prompts
- Con: Cannot ask "across all my results, which are outside Attia's targets?" without embedding all targets in the query
- Con: Does not scale as the number of biomarkers and frameworks grows

### Named framework table with per-biomarker range rows
- Pro: Frameworks are first-class entities — queryable, maintainable, extensible
- Pro: New frameworks (new practitioners, updated guidelines) are data additions, not schema changes
- Pro: Enables queries like "show all results outside Framework X for biomarker Y over time"
- Pro: Framework ranges are independent of result rows — no duplication
- Con: Requires a join to compare a result against a framework range
- Con: Framework data must be curated and entered (ranges are not always precisely published)

### Named framework table with versioning (effective date per range entry)
- Pro: Captures the fact that practitioners update their targets over time
- Pro: Point-in-time queries ("was this result optimal per Attia's 2023 targets?") become possible
- Con: Adds complexity to range lookup — must find the range effective at the result's draw date
- Con: Most framework updates are not precisely dated; versioning may be premature

## Schema (Option 2)

```sql
CREATE TABLE range_frameworks (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,  -- e.g. 'Lab Standard', 'Function Health', 'Attia', 'Brewer', 'Hyman'
    description TEXT,
    source_url  TEXT
) STRICT;

CREATE TABLE framework_ranges (
    id              INTEGER PRIMARY KEY,
    framework_id    INTEGER NOT NULL REFERENCES range_frameworks(id),
    biomarker_id    INTEGER NOT NULL REFERENCES biomarkers(id),
    range_low       REAL,
    range_high      REAL,
    unit            TEXT NOT NULL,  -- UCUM string; mandatory (ADR-0031) — see safety correction above
    range_text      TEXT,           -- for non-numeric targets or notes
    effective_date  TEXT,           -- ISO-8601 date; NULL = always current, populated = point-in-time (option 3 for free)
    notes           TEXT,
    UNIQUE (framework_id, biomarker_id, effective_date)
) STRICT;

-- The UNIQUE constraint above does not constrain the dateless default: SQLite treats NULLs
-- as distinct in a UNIQUE index, so it would permit two effective_date IS NULL rows for the
-- same (framework, biomarker). A partial unique index closes that gap, making the "always
-- current" default provably singular per (framework, biomarker):
CREATE UNIQUE INDEX ux_framework_ranges_default
    ON framework_ranges (framework_id, biomarker_id)
    WHERE effective_date IS NULL;
```

Notes:
- The lab range on each `results` row remains — it is a historical fact about what the lab reported, not a framework comparison.
- `unit` is `NOT NULL` by design: a numeric range with no unit is the safety bug this ADR corrects. Comparison normalizes the range and the result to the biomarker's `canonical_unit` ([ADR-0030](0030-biomarker-identity.md)) via the mechanism in [ADR-0031](0031-units-and-ucum.md).
- **Point-in-time lookup rule (deterministic).** For a result drawn on date *D*, the applicable range for a `(framework_id, biomarker_id)` pair is the row with the greatest `effective_date ≤ D`; if no dated row qualifies, the `effective_date IS NULL` row is the dateless default. The `UNIQUE` constraint and the partial index above guarantee this resolves to at most one row — dated rows are unique per date, the dateless default is unique per pair — so point-in-time resolution is never ambiguous.
- **STRICT tables and column types.** These tables are declared `STRICT` (real per-column type enforcement — see [ADR-0035](0035-migration-execution-semantics.md)), which is why `effective_date` is `TEXT` (ISO-8601) rather than a `DATE` affinity name: STRICT permits only `INT`/`INTEGER`/`REAL`/`TEXT`/`BLOB`/`ANY` as column types.

## Links
- Related: [design-rationale.md](../design-rationale.md) — original per-result reference range decision
- Related: [data-model.md](../data-model.md)
- Depends on: [ADR-0030](0030-biomarker-identity.md) — biomarker `canonical_unit` and the result value model comparison must respect
- Depends on: [ADR-0031](0031-units-and-ucum.md) — UCUM unit strings and unit-normalized comparison
- Resolves review item 3.D from [architecture-review-2026-06-10.md](../architecture-review-2026-06-10.md)
- Resolves: [architecture review 2026-07-06](../architecture-review-2026-07-06.md), item 3.B (framework portion) — `UNIQUE(framework_id, biomarker_id, effective_date)` + partial default index + deterministic point-in-time lookup rule; STRICT-legal `effective_date TEXT`
