# ADR-0031: Units and UCUM

## Status
Accepted

## Context and Problem Statement
Health values are meaningless without units, and comparing values across units without normalization produces dangerous errors. The [architecture-review-2026-06-10.md](../architecture-review-2026-06-10.md) flagged this as a real safety bug in the reference-range sketch ([ADR-0005](0005-reference-range-frameworks.md)): an ApoB target expressed in mg/dL compared against a result in g/L silently produces garbage flags (a factor-of-100 error). The platform therefore needs (a) a standard, unambiguous way to record units, and (b) a defined path to normalize units before any comparison.

Units appear in at least four places: the unit a lab reported a result in, a canonical unit per biomarker (see [ADR-0030](0030-biomarker-identity.md)'s `canonical_unit`), the unit a reference-range framework's thresholds are expressed in ([ADR-0005](0005-reference-range-frameworks.md)), and the dose of an intervention ([data-model.md](../data-model.md) intervention dose history). All must speak the same units language for normalization to be possible.

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
- Intervention dose `unit` ([data-model.md](../data-model.md) intervention dose history) is a UCUM string too. A dose is not compared to a biomarker `canonical_unit`, but storing it as UCUM keeps dose-vs-lab trend correlation (e.g. `mg/wk` vs `mg/d` across a titration history) normalizable when that analysis is built, rather than stranding it behind free-text later.
- No value is ever compared against a range, or trended against another value, without first normalizing both to the biomarker's canonical unit. A comparison whose units cannot be reconciled must fail loudly (surface an error/flag), never silently produce a result.

Storing UCUM strings costs nothing today and is the prerequisite that keeps every downstream option open. It is adopted now, unconditionally.

### Conversion engine — resolved: `ucumvert` (+ `pint`)
Research into the Python UCUM ecosystem (2026-07) found a thin landscape:

- **ucumvert** (MIT, actively maintained, v0.3.x as of mid-2026) — implements the full UCUM 2.2 grammar via `lark` and converts UCUM → `pint`. The strongest option, but pre-1.0, **one-directional (UCUM → pint only)**, and the author cautions that the generated definitions should be reviewed before being trusted.
- **pyucum** (source repo [`stomioka/ucum`](https://github.com/stomioka/ucum)) — does both validation and conversion, but disqualifyingly by **remote network calls** to NLM and xml4pharma web services (the `xml4pharmaserver.com` endpoint performs the LOINC-based molar conversion); incompatible with the local-first, privacy-first model, and unmaintained since 2019.
- **ucum-lhc** — the NLM/Regenstrief reference implementation, but JavaScript, not Python.
- **pint** alone — a capable general units library, but it does not parse UCUM grammar natively; UCUM strings would be hand-mapped.

**Decision (2026-07-09): adopt `ucumvert` (+ `pint`)** as the conversion engine, wrapped behind a small internal units module, with our own verification of the specific biomarkers/units in real use. `ucumvert` and `pint` become the project's first runtime dependencies when the units module lands.

The two disqualifying-sounding caveats do not obstruct any path to v1:

- **One-directionality (no `pint` → UCUM serialization) is not needed on the v1 path.** Every UCUM string the system emits is one it was given and stored — a lab's reported unit, a biomarker's `canonical_unit` ([ADR-0030](0030-biomarker-identity.md)), a framework threshold's `unit` ([ADR-0005](0005-reference-range-frameworks.md)), an intervention dose `unit`. The core operation — normalize a value in unit A against canonical unit B — parses *both* endpoints UCUM → `pint`, converts numerically, and yields a scalar in the already-known canonical UCUM unit. It never asks "what UCUM string names this `pint` quantity?" The property suite's round-trip is a numeric round-trip through conversion factors, which one-directional parsing satisfies. `pint` → UCUM would only be required to name a *computed* unit never declared in UCUM (composite/derived units, or FHIR export of a derived value) — territory deferred with [ADR-0044](0044-derived-data-points.md), and even then a small hand-maintained `pint`-unit → UCUM lookup for the handful of derived units suffices.
- **Pre-1.0 maturity is contained by the internal units module.** The module is the only code that imports `ucumvert`/`pint`; our verification layer audits the generated definitions for exactly the biomarkers/units in real use (a small set at v1), and the property-based suite is the standing regression net. Because nothing downstream depends on *which* engine runs — only on canonical-unit normalization — swapping the engine later is a contained change behind the module, at the cost of a superseding ADR.

The property-based conversion suite in [testing-strategy.md](../testing-strategy.md) is the acceptance harness: its properties (identity, round-trip, composition, order preservation, molar conversions with mandatory biomarker context) are written against the internal units-module API, and the `ucumvert`-backed implementation must pass the suite unchanged.

### Positive Consequences
- Units are standard and machine-parseable from migration 0001; the safety bug in [ADR-0005](0005-reference-range-frameworks.md) is structurally closed
- Aligns result units with FHIR `Quantity` and with [ADR-0030](0030-biomarker-identity.md)/[ADR-0018](0018-fhir-interoperability.md) at no extra cost
- The engine choice is decoupled and reversible — nothing downstream depends on *which* converter runs, only on canonical-unit normalization

### Negative Consequences / Tradeoffs
- The chosen engine (`ucumvert`) is pre-1.0 and one-directional, so it is used behind an internal units module with a local verification/validation layer over the biomarkers/units in real use, rather than blind trust — the property-based suite is the regression net
- `ucumvert` and `pint` are the project's first runtime dependencies; landing them activates the pip-audit CI gate and the release-blocking publish-time audit ([ADR-0045](0045-repository-workflow-and-ci-enforcement.md))

## Pros and Cons of the Options

### Free-text unit strings (status quo)
- Con: not reliably comparable; the mg/dL vs g/L class of silent error is exactly what the review flagged as a safety bug

### UCUM strings + canonical unit + normalized comparison (chosen)
- Pro: standard, machine-parseable, FHIR-aligned, and closes the safety bug structurally
- Pro: keeps the conversion-engine decision separable and reversible (engine resolved to `ucumvert` + `pint` behind an internal units module; swappable later behind that module)
- Con: the chosen engine is pre-1.0, so it needs a local validation layer over the units in real use rather than blind trust

## Links
- Extended by: [ADR-0056](0056-units-module-api-and-molar-context.md) — concretizes the internal units module's API and the molar-context mechanism this ADR left open (Phase 3 WI-1)
- Extends/depends on: [ADR-0030](0030-biomarker-identity.md) — the `canonical_unit` column and value model
- Related: [ADR-0005](0005-reference-range-frameworks.md) — framework range `unit` column and unit-normalized comparison
- Related: [ADR-0018](0018-fhir-interoperability.md) — UCUM is FHIR's `Quantity.code` system
- Related: [ADR-0032](0032-biomarker-loinc-cardinality.md) — method-variant LOINC codes can carry different reported units
- External: [ucumvert](https://github.com/dalito/ucumvert) (chosen engine), [pyucum / stomioka-ucum](https://github.com/stomioka/ucum) (evaluated, rejected — remote-API-dependent), [UCUM at NLM](https://ucum.nlm.nih.gov/)
- Related: [testing-strategy.md](../testing-strategy.md) — the property-based conversion suite is the acceptance harness the `ucumvert`-backed units module must pass
- Resolves review item 3.E (units portion) and supports 3.D from [architecture-review-2026-06-10.md](../architecture-review-2026-06-10.md)
