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
| **Accepted (partially superseded by ADR-XXXX)** | Decision stands, but a later ADR replaced a distinct part of it |
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
| [ADR-0001](0001-mcp-server-language.md) | Implementation Language | Accepted (partially superseded by ADR-0023) |
| [ADR-0002](0002-ai-provider-interface.md) | AI Client Interface | Accepted |
| [ADR-0003](0003-database-backend.md) | Database Backend | Accepted |
| [ADR-0004](0004-data-ingestion-strategy.md) | Data Ingestion Strategy | Accepted (partially) |
| [ADR-0005](0005-reference-range-frameworks.md) | Reference Range Frameworks | Accepted |
| [ADR-0006](0006-application-architecture.md) | Application Architecture | Accepted |
| [ADR-0007](0007-mcp-transport.md) | MCP Server Transport | Accepted |
| [ADR-0008](0008-process-lifecycle.md) | Process Lifecycle Management | Accepted |
| [ADR-0009](0009-database-migration.md) | Database Migration Approach | Accepted |
| [ADR-0010](0010-cli-plugin-model.md) | Plugin Architecture | Accepted |
| [ADR-0011](0011-event-bus.md) | Event Bus and Transport Adapters | Accepted |
| [ADR-0012](0012-job-abstraction.md) | Job Abstraction for Long-Running Operations | Accepted |
| [ADR-0013](0013-encryption-at-rest.md) | Encryption at Rest | Accepted |
| [ADR-0014](0014-websocket.md) | WebSocket / Bidirectional Communication | Proposed |
| [ADR-0015](0015-data-export.md) | Data Export and Portability | Proposed |
| [ADR-0016](0016-automation-plugin-type.md) | Automation Plugin Type | Proposed — stub |
| [ADR-0017](0017-notification-channels.md) | Notification Channel Plugin Type | Proposed — stub |
| [ADR-0018](0018-fhir-interoperability.md) | FHIR / HL7 Interoperability | Proposed — stub |
| [ADR-0019](0019-multi-device-sync.md) | Multi-Device Sync | Proposed |
| [ADR-0020](0020-plugin-registry.md) | Plugin Registry / Marketplace | Proposed — stub |
| [ADR-0021](0021-time-series-aggregation.md) | Time-Series Data Aggregation Strategy | Proposed — stub |
| [ADR-0022](0022-semver.md) | Version Policy (SemVer 2.0.0) | Accepted |
| [ADR-0023](0023-distribution-mechanism.md) | Distribution Mechanism | Accepted |
| [ADR-0024](0024-plugin-extensions.md) | Plugin System Extensions — pip Dependencies and Versioning | Accepted |
| [ADR-0025](0025-plugin-host-process-matrix.md) | Plugin Host-Process Matrix and Core Service Isolation | Accepted |
| [ADR-0026](0026-named-scoped-tokens.md) | Named Scoped Bearer Tokens | Accepted |
| [ADR-0027](0027-audit-trail-and-corrections.md) | Audit Trail and Data Corrections — Event Sourcing Rejected | Accepted |
| [ADR-0028](0028-key-derivation-and-rotation.md) | Key Derivation, Rotation, and Key Lifetime | Accepted |
| [ADR-0029](0029-mcp-streamable-http.md) | MCP Transport Refresh — Streamable HTTP | Accepted |
| [ADR-0030](0030-biomarker-identity.md) | Biomarker Identity and Value Representation | Accepted |
| [ADR-0031](0031-units-and-ucum.md) | Units and UCUM | Accepted |
| [ADR-0032](0032-biomarker-loinc-cardinality.md) | Biomarker–LOINC Cardinality | Proposed — stub |
| [ADR-0033](0033-plaintext-artifact-disposal.md) | Plaintext Artifact Disposal | Accepted |
| [ADR-0034](0034-clinical-document-storage.md) | Clinical Document Original File Storage | Accepted |
| [ADR-0035](0035-migration-execution-semantics.md) | Migration Execution Semantics and Connection Pragmas | Accepted |
| [ADR-0036](0036-plugin-package-installation-integrity.md) | Plugin Package Installation Integrity | Accepted |
| [ADR-0037](0037-core-service-concurrency-and-driver.md) | Core Service Concurrency Model and Database Driver Choice | Accepted |
| [ADR-0038](0038-backup-execution-and-verification.md) | Backup Execution and Verification | Accepted |
| [ADR-0039](0039-startup-sequence-and-passphrase-handoff.md) | Startup Sequence — Migration Ownership and Passphrase Handoff | Accepted |
| [ADR-0040](0040-health-endpoint-authentication.md) | Health Endpoint Authentication — Liveness Exemption and Monitor Scope | Accepted |
| [ADR-0041](0041-clinical-document-fts.md) | Clinical Document Full-Text Search | Accepted |
| [ADR-0042](0042-process-supervision-and-single-instance-locking.md) | Process Supervision and Single-Instance Locking | Accepted |
| [ADR-0043](0043-ai-authored-analyses-and-annotate-scope.md) | AI-Authored Analyses and the Annotate Scope | Proposed |
| [ADR-0044](0044-derived-data-points.md) | Derived Data Points — Distinct Class Now, Schema Deferred | Proposed |
| [ADR-0045](0045-repository-workflow-and-ci-enforcement.md) | Repository Workflow and CI Enforcement | Accepted |
| [ADR-0046](0046-filesystem-layout-and-config-discovery.md) | Filesystem Layout and Configuration Discovery | Accepted |
| [ADR-0047](0047-crypto-surface-implementation-decisions.md) | Crypto-Surface Implementation Decisions (WI-2) | Accepted |
| [ADR-0048](0048-migration-file-packaging.md) | Migration File Packaging and Discovery | Accepted |
| [ADR-0049](0049-core-service-skeleton-implementation-decisions.md) | Core-Service-Skeleton Implementation Decisions (Phase 2 WI-1) | Proposed |
| [ADR-0050](0050-token-store-and-auth-implementation-decisions.md) | Token-Store and Auth-Enforcement Implementation Decisions (Phase 2 WI-2) | Proposed |
| [ADR-0051](0051-auth-lifecycle-and-rate-limiting-implementation-decisions.md) | Auth Lifecycle, Rate-Limiting, and Bootstrap Implementation Decisions (Phase 2 WI-2b) | Proposed |
| [ADR-0052](0052-bulk-import-identity-and-conflict-resolution.md) | Bulk-Import Identity, Natural Keys, and Conflict Resolution (Phase 2 WI-3) | Proposed |
| [ADR-0053](0053-read-endpoint-surface-and-pagination.md) | Read-Endpoint Surface, Keyset Pagination, and Value-Fidelity Serialization (Phase 2 WI-4) | Proposed |
| [ADR-0054](0054-biomarker-name-alias-fallback.md) | Name-Based Biomarker Alias Fallback (Phase 3 D2) | Proposed |
| [ADR-0055](0055-biomarker-category-taxonomy.md) | Biomarker Category Taxonomy — First-Class Categories with a Reserved Default (Phase 3 D1) | Proposed |
| [ADR-0056](0056-units-module-api-and-molar-context.md) | Units-Module API and Molar Context (Phase 3 WI-1) | Proposed |
| [ADR-0057](0057-reference-data-and-catalog-import-implementation-decisions.md) | Reference-Data Catalog, Alias Resolver, and Catalog-Import Implementation Decisions (Phase 3 WI-2) | Proposed |
