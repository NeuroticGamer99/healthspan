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
Chosen option: **[TBD]**

### Positive Consequences
-

### Negative Consequences / Tradeoffs
-

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

## Proposed Schema Sketch (Option 2 or 3)

```sql
CREATE TABLE range_frameworks (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,  -- e.g. 'Lab Standard', 'Function Health', 'Attia', 'Brewer', 'Hyman'
    description TEXT,
    source_url  TEXT
);

CREATE TABLE framework_ranges (
    id              INTEGER PRIMARY KEY,
    framework_id    INTEGER NOT NULL REFERENCES range_frameworks(id),
    biomarker_id    INTEGER NOT NULL REFERENCES biomarkers(id),
    range_low       REAL,
    range_high      REAL,
    range_text      TEXT,   -- for non-numeric targets or notes
    effective_date  DATE,   -- NULL if not versioned / always current
    notes           TEXT
);
```

Note: the lab range on each `results` row remains — it is a historical fact about what the lab reported, not a framework comparison.

## Links
- Related: [design-rationale.md](../design-rationale.md) — original per-result reference range decision
- Related: [data-model.md](../data-model.md)
