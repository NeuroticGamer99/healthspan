# ADR-0030: Biomarker Identity and Value Representation

## Status
Accepted

## Context and Problem Statement
The schema treats each biomarker as a first-class entity (`biomarkers` table) identified by a canonical name, with every result row carrying a `biomarker_id` foreign key (see [design-rationale.md](../design-rationale.md)). Two gaps in that model surface as soon as real lab data is entered:

1. **Identity is name-only.** The canonical name is a human-chosen display string. It gives the platform no standard, machine-stable identifier to (a) resolve the same biomarker reported under different names by different labs, or (b) interoperate with the wider health-data ecosystem. [ADR-0018](0018-fhir-interoperability.md)'s FHIR export needs a coded identifier (`Observation.code`); the alias problem in [open-questions.md](../open-questions.md) needs an anchor that is not a free-text name.

2. **The value column is assumed to be a bare number.** Real lab data is not always a plain `REAL`: results come back below or above assay detection limits (`<0.1`, `>150`), and some biomarkers are qualitative ("Reactive", "Not Detected", "Trace"). A single numeric column silently mishandles both — a below-detection ApoB stored as `0.1` is a wrong number, not a censored one, and a qualitative antibody panel has no numeric value at all.

This ADR decides how a biomarker is identified and how a result's value is represented. Units are a distinct concern decided in [ADR-0031](0031-units-and-ucum.md); the fact that one biomarker concept can correspond to several LOINC codes is a distinct concern deferred to [ADR-0032](0032-biomarker-loinc-cardinality.md).

## Decision Drivers
- A biomarker needs a machine-stable, standard identifier — not only a chosen display name — to anchor interoperability and alias resolution
- LOINC is the de facto standard identifier for laboratory observations; most electronic lab feeds already carry it, and FHIR `Observation.code` *is* a LOINC coding
- Not every biomarker has a LOINC code — body-composition device metrics (phase angle, ECW/TBW), Levels proprietary zone scores, and other derived values have none
- Existing schema and design already assume an internal `biomarker_id` surrogate key on every result and framework-range row; identity decisions should not invalidate that
- LOINC is maintained externally and released twice yearly; codes can be deprecated — the physical schema should not be coupled to that lifecycle
- Result values must faithfully represent below/above-detection and qualitative results from the first real panel, not just clean numerics

## Considered Options
- **Identity:** LOINC code as the natural/primary key vs. internal surrogate PK with LOINC as an attribute
- **Value:** single numeric column vs. numeric value + comparator + text-value triple

## Decision Outcome

### Identity: internal surrogate PK, LOINC as a strong attribute
`biomarkers.id` (INTEGER) remains the identifier and the target of every `biomarker_id` foreign key. A nullable, UNIQUE `loinc_code` column records the biomarker's canonical LOINC where one exists. The canonical name remains the human display label.

```sql
CREATE TABLE biomarkers (
    id             INTEGER PRIMARY KEY,
    canonical_name TEXT NOT NULL,          -- human display label
    loinc_code     TEXT UNIQUE,            -- canonical LOINC; NULL when none exists
    canonical_unit TEXT,                   -- UCUM string; see ADR-0031
    category       TEXT,                   -- lipids, metabolic, thyroid, ...
    ...
);
```

This captures the full interoperability value of LOINC — the code sits directly on the biomarker row, so FHIR export joins it once and drops it into `Observation.code` — without paying the costs of a natural key: no synthetic codes polluting the key space for LOINC-less metrics, no cascading foreign-key rewrite when an external code is deprecated, no text foreign keys on high-volume tables, and no forced code assignment at manual-entry time (LOINC starts `NULL` and is filled in later as an ordinary attribute update).

The `loinc_code` here is the biomarker's *canonical* LOINC. A biomarker concept the user trends across labs (e.g. LDL-C) may be reported under several distinct LOINC codes depending on method; modeling that many-to-one relationship — and where the lab's actually-reported code is stored — is deferred to [ADR-0032](0032-biomarker-loinc-cardinality.md). This ADR deliberately does not assume `loinc_code` is a clean one-to-one mapping.

**Honest scope of the alias problem:** LOINC *reduces* but does not eliminate name-based aliasing. Electronic feeds (e.g. Quest, LabCorp) commonly report LOINC, which resolves directly to a biomarker; but PDF-extracted, older, and manually entered results often carry only a name. A name-based alias fallback (the `biomarker_aliases` question in [open-questions.md](../open-questions.md)) therefore still has a role under this model, not a replaced one.

### Value: numeric value + comparator + text value
Result rows represent a value with three cooperating columns, following FHIR's `valueQuantity.comparator` shape:

```sql
value_num   REAL,   -- numeric magnitude; NULL for purely qualitative results
comparator  TEXT,   -- one of '<', '<=', '>=', '>' ; NULL means an exact value
value_text  TEXT,   -- qualitative/textual result ('Reactive', 'Not Detected'); NULL when numeric
```

- A normal quantitative result: `value_num = 92`, `comparator = NULL`.
- A below-detection result `<0.1`: `value_num = 0.1`, `comparator = '<'` — the magnitude is the detection threshold and the comparator marks it as censored, never conflated with a measured `0.1`.
- A qualitative result: `value_num = NULL`, `value_text = 'Not Detected'`.

Comparison logic (reference-range flagging, trend analysis) must treat a non-NULL `comparator` as a censored bound, and must ignore or specially handle rows where `value_num IS NULL`.

### Database-level enforcement (migration 0001)

The value model is enforced by `CHECK` constraints on the results table, not left to application code — the database-level analog of the validation boundary, so a malformed value cannot exist even if written through a future code path that bypasses validation:

- `CHECK (value_num IS NOT NULL OR value_text IS NOT NULL)` — every result carries a numeric magnitude, a text value, or both; a row with neither is meaningless and is rejected.
- `CHECK (comparator IS NULL OR value_num IS NOT NULL)` — a censoring comparator has nothing to bound without a magnitude, so `comparator` may be set only when `value_num` is.
- `CHECK (comparator IN ('<', '<=', '>=', '>'))` — the comparator domain is closed to the four FHIR `valueQuantity.comparator` values. An exact value (`comparator IS NULL`) satisfies this: SQLite evaluates the `IN` to `NULL` there, and a `CHECK` fails only on a `FALSE` result, so no separate `comparator IS NULL OR` guard is needed.

These constraints compose unchanged with the `STRICT` table declaration recorded in [ADR-0035](0035-migration-execution-semantics.md); the three value columns above are all STRICT-legal (`REAL`, `TEXT`, `TEXT`).

### Positive Consequences
- One machine-stable, standard identifier per biomarker where it exists, with the display name preserved for humans
- FHIR export gets its `Observation.code` from a single column with no extra machinery ([ADR-0018](0018-fhir-interoperability.md) becomes nearly free for LOINC-bearing biomarkers)
- All existing `biomarker_id` foreign keys and the surrogate-key design stand unchanged
- Below/above-detection and qualitative results are represented faithfully from migration 0001, not lossily coerced to a number
- The physical schema is decoupled from LOINC's external release/deprecation lifecycle

### Negative Consequences / Tradeoffs
- Retrieving a biomarker's LOINC requires reading the `biomarkers` row (a one-row join), rather than it being the key already in hand — a negligible cost
- Comparison and analysis logic is more involved than reading a single numeric column — but this complexity is inherent to lab data, not introduced by the schema
- Two coded systems now describe a biomarker (internal `id` and external `loinc_code`); consumers must know that `id` is authoritative for joins and `loinc_code` for interoperability

## Pros and Cons of the Options

### Identity — LOINC as natural/primary key
- Pro: the identifier is itself the interoperability code; no join to obtain LOINC
- Pro: uniqueness of a biomarker's LOINC is enforced structurally by the primary key
- Con: LOINC-less biomarkers (body composition, proprietary scores) force synthetic codes, mixing real and homegrown identifiers in the key space — and FHIR export must still branch on them, so "interoperability for free" only holds for the real-LOINC subset
- Con: one canonical concept can span several LOINC codes (method variants), forcing either fragmentation of the longitudinal series or a "canonical LOINC" that is a surrogate in disguise
- Con: couples the physical schema to an externally versioned code system — deprecations become cascading foreign-key migrations
- Con: text foreign keys on high-volume tables cost more storage and index space than an integer surrogate
- Con: manual/PDF entry rarely has the LOINC at insert time, forcing early synthetic codes and later primary-key mutation

### Identity — internal surrogate PK + LOINC attribute (chosen)
- Pro: captures the full interoperability value (LOINC on the row, single-join export) with none of the natural-key costs
- Pro: preserves every existing `biomarker_id` foreign key and the stable canonical-concept identity the platform's longitudinal analysis depends on
- Pro: `loinc_code` can start NULL and be enriched later without touching keys
- Con: one extra column and the conceptual overhead of two identifiers per biomarker

### Value — single numeric column
- Pro: simplest possible schema
- Con: silently corrupts below/above-detection results and cannot represent qualitative results at all — a correctness failure on real data

### Value — numeric + comparator + text (chosen)
- Pro: represents quantitative, censored, and qualitative results faithfully; matches the proven FHIR model, easing [ADR-0018](0018-fhir-interoperability.md)
- Con: comparison and analysis code must handle comparator and NULL-numeric cases explicitly

## Links
- Related: [ADR-0031](0031-units-and-ucum.md) — units (UCUM) and the `canonical_unit` column referenced above
- Related: [ADR-0032](0032-biomarker-loinc-cardinality.md) — one biomarker concept, many LOINC codes; where the reported code is stored
- Related: [ADR-0005](0005-reference-range-frameworks.md) — framework range comparison relies on this value model and the canonical unit
- Related: [ADR-0018](0018-fhir-interoperability.md) — `Observation.code` is a LOINC coding; `valueQuantity.comparator` is the value model adopted here
- Related: [open-questions.md](../open-questions.md) — biomarker alias table (reduced, not replaced, by LOINC)
- Related: [design-rationale.md](../design-rationale.md) — canonical biomarker names and the multi-source lab data rationale
- Resolves review item 3.E (biomarker identity portion) from [architecture-review-2026-06-10.md](../architecture-review-2026-06-10.md)
- Resolves: [architecture review 2026-07-06](../architecture-review-2026-07-06.md), item 3.B (value-model portion) — `CHECK` constraints enforcing the numeric/comparator/text model in the schema
