# ADR-0031: Units and UCUM

## Status
Proposed

## Context and Problem Statement
Health values are meaningless without units, and comparing values across units without normalization produces dangerous errors. The [architecture-review-2026-06-10.md](../architecture-review-2026-06-10.md) flagged this as a real safety bug in the reference-range sketch ([ADR-0005](0005-reference-range-frameworks.md)): an ApoB target expressed in mg/dL compared against a result in g/L silently produces garbage flags (a factor-of-100 error). The platform therefore needs (a) a standard, unambiguous way to record units, and (b) a defined path to normalize units before any comparison.

Units appear in at least three places: the unit a lab reported a result in, a canonical unit per biomarker (see [ADR-0030](0030-biomarker-identity.md)'s `canonical_unit`), and the unit a reference-range framework's thresholds are expressed in ([ADR-0005](0005-reference-range-frameworks.md)). All three must speak the same units language for normalization to be possible.

This ADR decides the units *representation* and the *normalization requirement*. The choice of a specific conversion engine/dependency is a separable question captured here as an open sub-decision, deliberately not settled ahead of research.

## Decision Drivers
- Units must be recorded in a standard, machine-parseable form — free-text unit strings ("mg/dl", "mg/dL", "milligrams per deciliter") are not reliably comparable
- UCUM (Unified Code for Units of Measure) is the healthcare-standard unit encoding and is what FHIR `Quantity.code` expects — it aligns with [ADR-0018](0018-fhir-interoperability.md) and [ADR-0030](0030-biomarker-identity.md)
- Comparison across frameworks and labs must unit-normalize to a canonical unit per biomarker, or it is unsafe
- The platform is local-first and privacy-first: any conversion mechanism must run locally, never call a remote units service
- Only a small number of biomarkers are in real use today; a heavy general-purpose dependency is not yet justified if it is not production-ready
- Some conversions are not pure scalar factors and need biomarker context (e.g. mg/dL ↔ mmol/L depends on molar mass) — the mechanism must accommodate that

## Considered Options
- Free-text unit strings (status quo) — rejected outright as the safety bug
- **Store units as UCUM strings, canonical unit per biomarker, normalize at comparison time** — with the conversion engine as a separable sub-decision

## Decision Outcome
Chosen: **units are stored as UCUM strings; every biomarker has a canonical unit (UCUM); all comparisons normalize to that canonical unit.**

- The unit a result was reported in is stored as a UCUM string on the result row.
- `biomarkers.canonical_unit` ([ADR-0030](0030-biomarker-identity.md)) holds the biomarker's canonical UCUM unit.
- Reference-range framework thresholds ([ADR-0005](0005-reference-range-frameworks.md)) carry a UCUM `unit` column.
- No value is ever compared against a range, or trended against another value, without first normalizing both to the biomarker's canonical unit. A comparison whose units cannot be reconciled must fail loudly (surface an error/flag), never silently produce a result.

Storing UCUM strings costs nothing today and is the prerequisite that keeps every downstream option open. It is adopted now, unconditionally.

### Open sub-decision — the conversion engine (to be resolved before implementation)
Research into the Python UCUM ecosystem (2026-07) found a thin landscape:

- **ucumvert** (MIT, actively maintained, v0.3.x as of mid-2026) — implements the full UCUM 2.2 grammar via `lark` and converts UCUM → `pint`. The strongest option, but pre-1.0, **one-directional (UCUM → pint only)**, and the author cautions that the generated definitions should be reviewed before being trusted.
- **pyucum** (source repo [`stomioka/ucum`](https://github.com/stomioka/ucum)) — does both validation and conversion, but disqualifyingly by **remote network calls** to NLM and xml4pharma web services (the `xml4pharmaserver.com` endpoint performs the LOINC-based molar conversion); incompatible with the local-first, privacy-first model, and unmaintained since 2019.
- **ucum-lhc** — the NLM/Regenstrief reference implementation, but JavaScript, not Python.
- **pint** alone — a capable general units library, but it does not parse UCUM grammar natively; UCUM strings would be hand-mapped.

Two candidate paths remain, to be decided in a follow-up (this ADR stays Proposed until then):

1. **Adopt `ucumvert` (+ `pint`)** as the conversion engine, wrapped behind a small internal units module, with our own verification of the specific biomarkers/units in real use — accepting a 0.x dependency and providing UCUM output ourselves where round-trip is needed.
2. **Curated conversion table** — store UCUM strings but drive conversion from a small hand-maintained table (canonical unit + factor/offset, biomarker-aware for molar conversions) covering only the biomarkers in use, deferring a general library until breadth demands it.

Either path preserves the stored-as-UCUM decision above; they differ only in the engine behind normalization.

The property-based conversion suite in [testing-strategy.md](../testing-strategy.md) is the acceptance harness for this sub-decision: its properties (identity, round-trip, composition, order preservation, molar conversions with mandatory biomarker context) are written against the internal units-module API, and whichever engine is chosen must pass the suite unchanged.

### Positive Consequences
- Units are standard and machine-parseable from migration 0001; the safety bug in [ADR-0005](0005-reference-range-frameworks.md) is structurally closed
- Aligns result units with FHIR `Quantity` and with [ADR-0030](0030-biomarker-identity.md)/[ADR-0018](0018-fhir-interoperability.md) at no extra cost
- The engine choice is decoupled and reversible — nothing downstream depends on *which* converter runs, only on canonical-unit normalization

### Negative Consequences / Tradeoffs
- The conversion engine is left open; a follow-up decision is required before reference-range comparison is implemented
- The best available Python UCUM library is pre-1.0 and one-directional, so whichever path is chosen needs a local verification/validation layer rather than blind trust

## Pros and Cons of the Options

### Free-text unit strings (status quo)
- Con: not reliably comparable; the mg/dL vs g/L class of silent error is exactly what the review flagged as a safety bug

### UCUM strings + canonical unit + normalized comparison (chosen)
- Pro: standard, machine-parseable, FHIR-aligned, and closes the safety bug structurally
- Pro: keeps the conversion-engine decision separable and reversible
- Con: requires a follow-up engine decision and a local validation layer over an immature library or a curated table

## Links
- Extends/depends on: [ADR-0030](0030-biomarker-identity.md) — the `canonical_unit` column and value model
- Related: [ADR-0005](0005-reference-range-frameworks.md) — framework range `unit` column and unit-normalized comparison
- Related: [ADR-0018](0018-fhir-interoperability.md) — UCUM is FHIR's `Quantity.code` system
- Related: [ADR-0032](0032-biomarker-loinc-cardinality.md) — method-variant LOINC codes can carry different reported units
- External: [ucumvert](https://github.com/dalito/ucumvert) (candidate engine), [pyucum / stomioka-ucum](https://github.com/stomioka/ucum) (evaluated, rejected — remote-API-dependent), [UCUM at NLM](https://ucum.nlm.nih.gov/)
- Related: [testing-strategy.md](../testing-strategy.md) — the property-based conversion suite is the acceptance harness for the open conversion-engine sub-decision
- Resolves review item 3.E (units portion) and supports 3.D from [architecture-review-2026-06-10.md](../architecture-review-2026-06-10.md)
