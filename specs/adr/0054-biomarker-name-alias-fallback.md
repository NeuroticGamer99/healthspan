# ADR-0054: Name-Based Biomarker Alias Fallback (Phase 3 D2)

## Status
Proposed

## Context and Problem Statement

[ADR-0030](0030-biomarker-identity.md) made biomarker identity an internal surrogate key with LOINC as a strong attribute, and was honest about the residue: electronic feeds carrying LOINC resolve directly, but PDF-extracted, older, and manually entered results often carry only a lab's name for the biomarker — "Vit D 25-OH", "HDL Cholesterol", "A1c". The open question it left ([open-questions.md](../open-questions.md), Schema) was whether to add a `biomarker_aliases` table now or handle lab-to-canonical name normalization at entry time in each client.

Phase 3 is exactly the manual/PDF lane — its CLI manual-entry tooling is the first consumer of name-based resolution — so the question's "before bulk data entry begins" trigger has fired. The owner's requirement (2026-07-14, Phase 3 kickoff): **a request may identify a biomarker without its canonical name, but the platform always reports out in canonical terms.**

## Decision Drivers

- Entry-time normalization implemented per client (CLI now; PDF extraction, MCP, GUI later) drifts — the risk the open question named; one resolver must own the mapping
- A fuzzy match that silently picks the wrong biomarker is the same defect family as the mg/dL-vs-g/L silent mis-flag ([ADR-0005](0005-reference-range-frameworks.md)): confidently wrong data — resolution must fail loudly, never guess
- Aliases as *data* (not code) follows the pattern that won in ADR-0005: new name forms are data additions, no schema or code change
- Each lab's naming quirks should be learned once, at the moment a human confirms them, and resolve silently forever after
- Reporting must stay canonical by construction, not by client discipline

## Considered Options

- Per-client name mapping at entry time (no schema) — rejected: N resolvers, guaranteed drift
- `biomarker_aliases` table with server-side resolution — chosen
- Fuzzy server-side matching — rejected as an *automatic* mechanism (see drivers); fuzziness belongs in interactive suggestion only

## Decision Outcome

### 1. `biomarker_aliases` table (migration 0004, lands with Phase 3 WI-2)

```sql
CREATE TABLE biomarker_aliases (
    id               INTEGER PRIMARY KEY,
    biomarker_id     INTEGER NOT NULL REFERENCES biomarkers(id),
    alias            TEXT NOT NULL,          -- as encountered/curated (display form)
    alias_normalized TEXT NOT NULL UNIQUE,   -- normalization of alias (rule in §2); resolver key
    source           TEXT,                   -- provenance: 'seed', 'manual-entry', a lab name, ...
    created_utc      TEXT NOT NULL
) STRICT;

CREATE INDEX ix_biomarker_aliases_biomarker ON biomarker_aliases (biomarker_id);
```

Catalog data, like `biomarkers` itself — no supersession columns, no `*_current` view; deletes are hard deletes with the mandatory [ADR-0027](0027-audit-trail-and-corrections.md) audit row. `source` is cheap provenance for auditing a suspect alias later ("seeded" vs. "recorded during manual entry from LabCorp").

### 2. Normalization rule, and exact-match-only resolution

The normalized form of a name is: Unicode NFKC → casefold → trim → collapse each internal whitespace run to a single space. Nothing else — no punctuation stripping, no stemming, no edit distance. Resolution is an **exact match on the normalized form**; anything less than exact is a failure, reported loudly with the unresolved name.

Fuzzy matching is permitted only as *interactive suggestion*: a client (the WI-4 CLI) may show near-matches for an unresolved name and ask the human to confirm — and on confirmation records the new alias — but the platform never auto-selects a fuzzy match on any non-interactive path.

### 3. One resolver, one namespace, provably unambiguous

Resolution is a Core Service capability, in exactly one place. The resolution namespace is the union of normalized `biomarkers.canonical_name` values and normalized aliases, mapping to `biomarker_id`. Ambiguity is prevented at write time, since SQLite cannot express cross-table uniqueness:

- `alias_normalized` is `UNIQUE` among aliases (schema-enforced above);
- writing an alias whose normalized form equals any biomarker's normalized canonical name is rejected (it is either redundant — same biomarker — or ambiguous — different biomarker; both are errors);
- writing a biomarker whose normalized canonical name equals an existing alias's normalized form, or another biomarker's normalized canonical name, is rejected. (`canonical_name`'s existing `UNIQUE` is byte-exact only; the normalized-level check is the resolver's write-path validation.)

An alias pointing at its own biomarker's exact canonical spelling is rejected as redundant rather than stored.

### 4. The import endpoint accepts names

`POST /v1/import` `lab_results` rows accept **exactly one of `biomarker_id` or `biomarker_name`** (both present or both absent per row is a `422`). `biomarker_name` is resolved through §3's resolver during the validation pass — before conflict detection, so [ADR-0052](0052-bulk-import-identity-and-conflict-resolution.md)'s natural keys and conflict semantics operate on resolved `biomarker_id`s unchanged. An unresolvable name fails validation with the collected-errors `422`, naming the row and the unresolved string; per ADR-0052, the whole batch rejects before any write. This extends the ADR-0052 import contract; it does not alter any existing behavior for `biomarker_id` rows.

Aliases themselves are written through the same reference-data import path (Phase 3 WI-2 registers `biomarker_aliases` as an importable table alongside the other catalog tables), which is what the CLI's confirm-and-record flow calls.

### 5. Output is canonical by construction

An alias exists only at resolution time — nothing downstream of the resolver stores, joins on, or serializes an alias. Result rows carry `biomarker_id`; read surfaces report the biomarker's `canonical_name`. No read endpoint changes are required to satisfy the owner's report-in-canonical requirement; it falls out of the data flow.

### 6. Scope: the fallback lane only

Direct LOINC resolution ([ADR-0030](0030-biomarker-identity.md), [ADR-0032](0032-biomarker-loinc-cardinality.md)) remains the primary identity lane for electronic feeds and is untouched. This ADR is the manual/PDF fallback ADR-0030 anticipated — it reduces to nothing for a feed that carries codes.

### Positive Consequences

- One resolver serves the CLI now and every Phase 7 adapter later; no per-client drift
- The alias vocabulary grows organically at entry time, each mapping confirmed by a human exactly once
- Wrong-biomarker resolution is structurally prevented (exact match + unambiguous namespace), not policed by convention
- Canonical-only reporting requires no client discipline

### Negative Consequences / Tradeoffs

- Two write paths gain normalized-level validation (aliases and biomarkers) that a plain `UNIQUE` cannot express — application-enforced, so it needs test coverage
- An interactive confirm step is friction on first encounter of each new name form — accepted; it is the mechanism that keeps automatic resolution safe
- `biomarker_name` fattens the import contract slightly (one more mutually-exclusive field and error family)

## Consequences for Other Documents

- **[open-questions.md](../open-questions.md)**: Biomarker alias table → Resolved — this PR
- **[development-plan.md](../development-plan.md)**: decision-gates table, name-based alias fallback row → decided — this PR
- **[data-model.md](../data-model.md)**: `biomarker_aliases` table — with the implementing WI-2 PR
- **[api-reference.md](../api-reference.md)**: import contract (`biomarker_name`), reference-data endpoints — with the implementing WI-2 PR
- **[ADR-0052](0052-bulk-import-identity-and-conflict-resolution.md)** (Proposed): navigation link "Extended by ADR-0054" — this PR

## Links

- Extends: [ADR-0052](0052-bulk-import-identity-and-conflict-resolution.md) — the import contract gains `biomarker_name`
- Implements the fallback anticipated by: [ADR-0030](0030-biomarker-identity.md) — "a name-based alias fallback therefore still has a role under this model"
- Related: [ADR-0032](0032-biomarker-loinc-cardinality.md) — the code-based lane this fallback complements
- Related: [ADR-0027](0027-audit-trail-and-corrections.md) — audit obligations on catalog writes/deletes
- Related: [ADR-0005](0005-reference-range-frameworks.md) — the fail-loud-never-guess safety posture this ADR inherits
- Resolves: [open-questions.md](../open-questions.md), Schema — "Biomarker alias table"
