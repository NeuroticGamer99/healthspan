# ADR-0018: FHIR / HL7 Interoperability

## Status
Proposed — stub

## Context and Problem Statement
FHIR R4 (Fast Healthcare Interoperability Resources) is the current healthcare data exchange standard. Patient portals, hospital systems, and insurance providers increasingly expose FHIR APIs. Supporting FHIR would enable: importing records directly from patient portals via FHIR API, exporting data in a format physicians and other health tools can consume, and participation in the broader health data ecosystem.

## Decision Drivers
- FHIR R4 is the direction the healthcare industry is moving — ignoring it limits interoperability
- FHIR import and export are distinct concerns (import adapter vs export format)
- The internal schema does not need to be FHIR-native — FHIR is a wire format, not a storage format
- Python has FHIR libraries (`fhir.resources`) that can be used in plugins
- This is complex and should not block the initial implementation

## Decision Outcome
TBD — deferred until core functionality is stable. FHIR support will be delivered as import adapter and export format plugins, not as a change to the core schema.

## Scope When Addressed
- **FHIR inbound adapter** — import `Observation`, `DiagnosticReport`, `MedicationStatement`, and `Condition` resources into the platform's native schema
- **FHIR export format** — export lab results, events, and interventions as FHIR R4 bundles (extends ADR-0015)
- **SMART on FHIR** — OAuth-based patient portal API access (more complex; depends on portal support)

## Links
- Related: [ADR-0004](0004-data-ingestion-strategy.md) — FHIR inbound as an import adapter
- Related: [ADR-0015](0015-data-export.md) — FHIR as an export format
