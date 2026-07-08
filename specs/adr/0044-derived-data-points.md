# ADR-0044: Derived Data Points — Distinct Class Now, Schema Deferred

## Status
Proposed

## Context and Problem Statement
Analysis — the owner's, an AI client's, eventually an analysis plugin's — produces structured, plottable data points: a computed HOMA-IR series, a regression-derived trend, statistics computed outside the tool that the owner wants tracked longitudinally instead of maintained in a spreadsheet. The data model has no home for them. The only derived-data concept so far is the ADR-0021/[ADR-0027](0027-audit-trail-and-corrections.md) aggregate layer: rebuildable caches, never authoritative, invisible plumbing — not user-visible tracked series.

The cost of having no home is not the missing feature — it is **contamination by disguise**. The path of least resistance for a computed value with no legitimate table is manual entry as a lab result, which silently destroys the source/derived distinction that range comparison ([ADR-0005](0005-reference-range-frameworks.md)), biomarker identity ([ADR-0030](0030-biomarker-identity.md)), and every MCP answer rely on. Declaring derived data out of scope does not make it absent; it makes it disguised.

Yet the full schema is not designable well today, because "derived series" is secretly two things with different behavior under the correction model ([ADR-0027](0027-audit-trail-and-corrections.md)): **internally computed** points (formula stored with them; rebuildable — when a source row feeding them is superseded, recompute) versus **externally computed** imports (opaque snapshots; cannot be recomputed, only marked stale). They differ in identity questions too: does a derived series get a `biomarker_id`? May it be range-compared — HOMA-IR has published reference ranges, so "derived values are never range-compared" is simply false as a blanket rule. Designing the table now, before the interpretive layer ([ADR-0043](0043-ai-authored-analyses-and-annotate-scope.md)) exists and before any real usage shows which subtype dominates, would be guessing.

## Decision Drivers
- Contamination by disguise must be closed now, and closing it is cheap — it is a principle, not a table
- The two subtypes (rebuildable vs. snapshot) have different correction semantics; conflating them in a prematurely designed schema is the expensive trap
- The design will be materially better informed after the analyses table is in use and its attachments show what derived data actually gets produced
- The project has 44 ADRs and no code: decisions should be principle-sized now, schema-sized when implementation reaches them

## Considered Options
1. **Design the full first-class derived-series schema now**
2. **Declare derived data out of scope**
3. **Decide the principle now, defer the schema** (chosen)

## Decision Outcome
Chosen: **option 3.** Four things are decided now; the schema is explicitly not.

1. **Derived points are a distinct content class.** `derivation` is a peer of source and interpretation in the provenance model ([provenance-and-derived-data.md](../provenance-and-derived-data.md)) and is never stored in source-data tables. This is half of INV-6 ([security.md](../security.md)); [ADR-0043](0043-ai-authored-analyses-and-annotate-scope.md) establishes the other half.

2. **Enforcement is structural where it can be, normative where it cannot.** For machine writers the boundary is structural: no AI-held credential carries source-data write scope by default ([ADR-0043](0043-ai-authored-analyses-and-annotate-scope.md)), so a computed value physically cannot enter a source table from the write path that produces most derived data. For the owner it is normative: nothing technically stops typing a computed HOMA-IR in as a manual lab result, and the control is a sanctioned home that removes the incentive, plus documentation stating the rule — the honest-limits framing [ADR-0033](0033-plaintext-artifact-disposal.md) established for best-effort disposal.

3. **Interim home: the structured attachment on analyses.** An analysis row ([ADR-0043](0043-ai-authored-analyses-and-annotate-scope.md), [data-model.md](../data-model.md)) may carry a small `result_data` JSON block of computed points alongside the narrative. That makes derived output reviewable, searchable (the narrative is FTS-indexed), attributed, supersession-versioned, and exportable — but not plottable as a first-class series. Accepted as the interim state: the points are *kept, in the tool, correctly classed*, which is the part that cannot wait.

4. **Schema design is deferred with an explicit trigger.** The real derived-series table is designed when (a) the analyses table exists in a running system and (b) accumulated `result_data` attachments show which subtype dominates. The recorded sub-questions travel with the deferral ([open-questions.md](../open-questions.md), Schema): internal-vs-external subtype split and their correction semantics; whether a derived series gets a `biomarker_id` ([ADR-0030](0030-biomarker-identity.md)); whether and when derived series are range-compared ([ADR-0005](0005-reference-range-frameworks.md)); staleness marking when a feeding source row is superseded ([ADR-0027](0027-audit-trail-and-corrections.md)).

**ADR-0021 aggregates are unaffected.** They remain internal, rebuildable, non-authoritative caches invalidated by `data.*` events. This ADR is about user-visible, deliberately tracked series — a different animal that happens to share the word "derived."

### Positive Consequences
- The contamination path is closed at principle cost: every computed value has a sanctioned, correctly-classed home from day one
- No premature schema to redesign when real usage reveals the subtype mix
- The interim answer ships with the interpretive layer — no new machinery at all

### Negative Consequences / Tradeoffs
- Derived points are not plottable as first-class series until the deferred design lands; the JSON attachment is opaque to SQL (accepted at expected volumes)
- Enforcement against owner-entered disguise is normative only — stated honestly rather than pretended away
- A deferral is a promise: the trigger condition must actually be honored, or the spreadsheet quietly returns

## Pros and Cons of the Options

### Full schema now
- Pro: complete story immediately; plottable from first implementation
- Con: guesses the rebuildable-vs-snapshot split, the identity question, and the range-comparison rule with zero usage data; highest rework risk of the three

### Out of scope
- Pro: zero cost now
- Con: contamination by disguise — computed values enter as fake lab results, silently poisoning the source tier; the manual-spreadsheet pain the platform exists to remove persists

### Principle now, schema deferred (chosen)
- Pro: buys the integrity protection immediately at near-zero cost; defers exactly the part that benefits from waiting
- Con: interim home is reviewable but not plottable; requires discipline to revisit

## Links
- Related: [ADR-0043](0043-ai-authored-analyses-and-annotate-scope.md) — companion decision; the analyses table and `annotate` write path this ADR's interim home rides on; together they establish INV-6
- Related: [ADR-0021](0021-time-series-aggregation.md) — internal aggregates; explicitly distinct from user-visible derived series
- Related: [ADR-0027](0027-audit-trail-and-corrections.md) — correction semantics that split the two derived subtypes
- Related: [ADR-0030](0030-biomarker-identity.md), [ADR-0005](0005-reference-range-frameworks.md) — identity and range-comparison questions recorded with the deferral
- Related: [ADR-0033](0033-plaintext-artifact-disposal.md) — precedent for stating enforcement limits honestly
- Related: [provenance-and-derived-data.md](../provenance-and-derived-data.md) — the content-class model; [security.md](../security.md) — INV-6
- Related: [open-questions.md](../open-questions.md) — the deferred schema's recorded sub-questions
