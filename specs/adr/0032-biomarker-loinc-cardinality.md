# ADR-0032: Biomarker–LOINC Cardinality

## Status
Proposed — stub

## Context and Problem Statement
[ADR-0030](0030-biomarker-identity.md) gives each biomarker an internal surrogate key and a nullable canonical `loinc_code`. That decision deliberately treats `loinc_code` as the biomarker's *canonical* LOINC and leaves a harder relationship unmodeled: **one canonical biomarker concept can correspond to several distinct LOINC codes.**

LOINC encodes method, specimen, property, and timing into the code itself, so what a user thinks of as a single trended biomarker often maps to multiple codes. LDL cholesterol is the standard example: direct-measure LDL-C, calculated LDL-C (Friedewald), and other method variants each have their own LOINC code (e.g. 18262-6, 13457-7, 2089-1). Different labs — or the same lab over time — may report the "same" biomarker under different codes. The inverse also occurs: a code the platform has not seen may need to resolve to an existing biomarker concept.

This matters because the platform's core value is a coherent longitudinal series (see [design-rationale.md](../design-rationale.md)), and method differences are exactly the kind of thing that can make a trend misleading (echoing the assay-variability rationale that already drives lab source being first-class). Collapsing method variants hides a real signal; splitting them fragments the series. Neither extreme is obviously right, so this needs its own reasoned decision.

## Decision Drivers
- The canonical concept the user trends must stay stable even when labs report varying LOINC codes for it
- Method differences (direct vs. calculated LDL-C; different immunoassay platforms) can be clinically meaningful and should not be silently erased
- The actually-reported LOINC on each result should be preserved for provenance, FHIR fidelity ([ADR-0018](0018-fhir-interoperability.md)), and later reinterpretation
- Resolution of an incoming code to a biomarker must be deterministic and auditable
- Whatever is chosen must fit the surrogate-key identity model of [ADR-0030](0030-biomarker-identity.md) and the alias approach in [open-questions.md](../open-questions.md)

## Considered Options (to be evaluated)
- **Canonical LOINC only** — store one canonical LOINC per biomarker ([ADR-0030](0030-biomarker-identity.md)) and discard the reported code; simplest, but loses method provenance
- **Reported LOINC per result** — add a `reported_loinc` column to result rows capturing exactly what the lab sent, alongside the biomarker's canonical LOINC; preserves provenance without fragmenting the series
- **Biomarker ↔ LOINC mapping table** — a child table listing all LOINC codes that map to a biomarker (canonical + accepted variants), driving deterministic import-time resolution; most expressive, most machinery
- **Method as a first-class dimension** — model method/specimen distinctions explicitly (e.g. a `method` attribute) so direct vs. calculated LDL-C can be trended separately when wanted and together when not

These are not mutually exclusive — the reported-LOINC column and a mapping table likely compose.

## Decision Outcome
TBD — deferred. [ADR-0030](0030-biomarker-identity.md) stands without this: `biomarkers.loinc_code` holds the canonical code today, and this ADR will decide how reported codes and method variants are captured and resolved before multi-lab lab-result data with electronic LOINC feeds is imported at scale.

## Links
- Extends: [ADR-0030](0030-biomarker-identity.md) — biomarker identity and the canonical `loinc_code`
- Related: [ADR-0018](0018-fhir-interoperability.md) — reported LOINC feeds `Observation.code` fidelity
- Related: [open-questions.md](../open-questions.md) — biomarker alias resolution
- Related: [design-rationale.md](../design-rationale.md) — longitudinal series integrity and assay/method variability
- Raised by review item 3.E follow-up in [architecture-review-2026-06-10.md](../architecture-review-2026-06-10.md)
