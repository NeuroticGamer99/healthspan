# Architecture Decision Records

This folder contains the Architecture Decision Records (ADRs) for the Healthspan platform.

---

## What is an ADR?

An Architecture Decision Record captures a single architectural decision: the context that drove it, the options considered, and the reasoning behind the chosen outcome. ADRs are written at the time the decision is made and are never retroactively changed — if a decision is reversed, a new ADR supersedes the old one.

The value of ADRs is institutional memory. A future contributor — or the original author months later — can read an ADR and understand not just *what* was decided but *why*, and what was explicitly rejected.

**Authoritative references:**

- [Documenting Architecture Decisions](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions) — Michael Nygard's original post that defined the format (2011)
- [ADR GitHub organization](https://adr.github.io/) — tooling, templates, and community resources
- [MADR — Markdown Architectural Decision Records](https://adr.github.io/madr/) — the template variant this project follows

---

## Status values

| Status | Meaning |
|---|---|
| **Proposed** | Under discussion; not yet binding |
| **Proposed — stub** | Placeholder created; content not yet written |
| **Accepted** | Decision is made; implementation may proceed |
| **Accepted (partially)** | Core decision is made; open sub-decisions remain |
| **Deprecated** | Was accepted; no longer applies |
| **Superseded by ADR-XXXX** | Replaced by a later decision |

---

## Template

New ADRs should use [`0000-template.md`](0000-template.md) as the starting point.

The template follows the MADR structure:
- **Status** — current state
- **Context and Problem Statement** — what situation prompted the decision
- **Decision Drivers** — the forces shaping the outcome
- **Considered Options** — what was evaluated
- **Decision Outcome** — what was chosen and why, with positive and negative consequences
- **Pros and Cons of the Options** — comparative analysis
- **Links** — related ADRs and external references

File naming convention: `NNNN-short-hyphenated-title.md` where `NNNN` is the next available zero-padded number.

---

## Index

| ADR | Title | Status |
|---|---|---|
| [ADR-0001](0001-mcp-server-language.md) | Implementation Language | Superseded by ADR-0023 |
| [ADR-0002](0002-ai-provider-interface.md) | AI Client Interface | Proposed |
| [ADR-0003](0003-database-backend.md) | Database Backend | Proposed |
| [ADR-0004](0004-data-ingestion-strategy.md) | Data Ingestion Strategy | Accepted (partially) |
| [ADR-0005](0005-reference-range-frameworks.md) | Reference Range Frameworks | Proposed |
| [ADR-0006](0006-application-architecture.md) | Application Architecture | Accepted |
| [ADR-0007](0007-mcp-transport.md) | MCP Server Transport | Accepted |
| [ADR-0008](0008-process-lifecycle.md) | Process Lifecycle Management | Accepted |
| [ADR-0009](0009-database-migration.md) | Database Migration Approach | Accepted |
| [ADR-0010](0010-cli-plugin-model.md) | Plugin Architecture | Accepted |
| [ADR-0011](0011-event-bus.md) | Event Bus and Transport Adapters | Proposed |
| [ADR-0012](0012-job-abstraction.md) | Job Abstraction for Long-Running Operations | Proposed |
| [ADR-0013](0013-encryption-at-rest.md) | Encryption at Rest | Accepted |
| [ADR-0014](0014-websocket.md) | WebSocket / Bidirectional Communication | Proposed |
| [ADR-0015](0015-data-export.md) | Data Export and Portability | Proposed |
| [ADR-0016](0016-automation-plugin-type.md) | Automation Plugin Type | Proposed — stub |
| [ADR-0017](0017-notification-channels.md) | Notification Channel Plugin Type | Proposed — stub |
| [ADR-0018](0018-fhir-interoperability.md) | FHIR / HL7 Interoperability | Proposed — stub |
| [ADR-0019](0019-multi-device-sync.md) | Multi-Device Sync | Proposed — stub |
| [ADR-0020](0020-plugin-registry.md) | Plugin Registry / Marketplace | Proposed — stub |
| [ADR-0021](0021-time-series-aggregation.md) | Time-Series Data Aggregation Strategy | Proposed — stub |
| [ADR-0022](0022-semver.md) | Version Policy (SemVer 2.0.0) | Accepted |
| [ADR-0023](0023-distribution-mechanism.md) | Distribution Mechanism | Accepted |
| [ADR-0024](0024-plugin-extensions.md) | Plugin System Extensions — pip Dependencies and Versioning | Accepted |
