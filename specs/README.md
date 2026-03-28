# specs/ — Architecture and Design Documentation

This directory contains all architectural documentation for the Healthspan platform. It is the primary entry point for understanding how the system is designed and why.

---

## How this documentation is organized

There are two layers of documentation here, each with a different purpose:

**Design documents** (this directory) are living documents that evolve as the project develops. They capture requirements, design principles, data models, and open questions. They may reference multiple ADRs and synthesize information across concerns.

**Architecture Decision Records** ([`adr/`](adr/)) are immutable historical records. Each ADR captures a single architectural decision: the context, the options considered, and the reasoning behind the choice. Once accepted, an ADR's content is not modified — if a decision changes, a new ADR supersedes the old one. See [`adr/README.md`](adr/README.md) for the full index and status conventions.

The design documents inform ADRs; the ADRs formalize specific decisions that emerge from the design process.

---

## Design documents

| Document | Purpose |
|----------|---------|
| [design-rationale.md](design-rationale.md) | Core design philosophy, architectural principles, and cross-cutting conventions (timestamp handling, canonical biomarker names, schema versioning). Start here for the "why" behind major decisions. |
| [data-model.md](data-model.md) | Inventory of all health data types the platform handles, their sources, schema considerations, and current design status. Also covers mobile health platform APIs and integration paths. |
| [security.md](security.md) | Security requirements and threat model. Covers the trust model, authentication, encryption at rest, network security, input validation, plugin security boundary, and logging prohibitions. |
| [api-reference.md](api-reference.md) | Design-time specification of the Core REST API surface. Consolidates endpoints defined across ADRs into a single reference. Stub — details added during implementation. |
| [testing-strategy.md](testing-strategy.md) | Testing approach across all layers: unit, integration, plugin, end-to-end, security, and migration tests. Covers synthetic test data, encryption testing, and cross-platform concerns. |
| [observability.md](observability.md) | Standards for health endpoints, structured logging, metrics, and request tracing across all platform processes. |
| [glossary.md](glossary.md) | Project-specific terminology with definitions and source references. Covers architecture, data model, plugin system, security, and observability terms. |
| [open-questions.md](open-questions.md) | Architectural and technical decisions that need resolution before or during implementation. Includes resolved items with links to their outcomes. |
| [reference-home-assistant-architecture.md](reference-home-assistant-architecture.md) | Research reference documenting Home Assistant's architecture in detail. Used to inform Healthspan design decisions — HA is the closest architectural analog. Not a spec for this project. |

---

## Relationship to `adr/`

Design documents and ADRs serve complementary roles:

- **design-rationale.md** explains the principles; ADRs like [ADR-0006](adr/0006-application-architecture.md) (application architecture) formalize the specific process isolation decision that follows from those principles.
- **data-model.md** catalogs what data the platform handles; ADRs like [ADR-0004](adr/0004-data-ingestion-strategy.md) (ingestion strategy) and [ADR-0005](adr/0005-reference-range-frameworks.md) (reference ranges) formalize how that data enters and is interpreted.
- **security.md** defines requirements; ADRs like [ADR-0013](adr/0013-encryption-at-rest.md) (encryption) and [ADR-0007](adr/0007-mcp-transport.md) (MCP transport) formalize the mechanisms that satisfy those requirements.
- **open-questions.md** tracks decisions that have not yet become ADRs. When an open question is resolved, it either becomes a new ADR or is documented in the resolved section with a link to the relevant ADR.

---

## arc42 mapping

The documentation structure is informed by [arc42](https://arc42.org/), a widely-adopted software architecture documentation template. The table below maps arc42 sections to their Healthspan equivalents for readers familiar with that framework. We use descriptive file names rather than arc42's numbered sections — the navigation benefit comes from the completeness checklist, not the naming convention.

| arc42 Section | Healthspan Document |
|---|---|
| 1. Introduction and Goals | [README.md](../README.md) (project overview) |
| 2. Constraints | Captured per-decision in [ADRs](adr/) (decision drivers sections) |
| 3. Context and Scope | [design-rationale.md](design-rationale.md) (project goal, design influences) |
| 4. Solution Strategy | [design-rationale.md](design-rationale.md) (architectural philosophy, key design choices) |
| 5. Building Block View | [ADR-0006](adr/0006-application-architecture.md) (process architecture), [data-model.md](data-model.md) (data layer) |
| 6. Runtime View | [ADR-0008](adr/0008-process-lifecycle.md) (lifecycle), [observability.md](observability.md) (startup sequence) |
| 7. Deployment View | [ADR-0008](adr/0008-process-lifecycle.md) (launcher, Docker, systemd), [ADR-0023](adr/0023-distribution-mechanism.md) (distribution) |
| 8. Crosscutting Concepts | [design-rationale.md](design-rationale.md) (timestamps, canonical names, versioning), [security.md](security.md), [observability.md](observability.md) |
| 9. Architecture Decisions | [adr/](adr/) (24 ADRs) |
| 10. Quality Requirements | [security.md](security.md), [observability.md](observability.md), [testing-strategy.md](testing-strategy.md) |
| 11. Risks and Technical Debt | [open-questions.md](open-questions.md) |
| 12. Glossary | [glossary.md](glossary.md) |

---

## Personal data

The [`personal/`](personal/) directory is gitignored and is the only location where personal health data or personally identifying information may be written. See the project [CLAUDE.md](../CLAUDE.md) for the full containment policy.
