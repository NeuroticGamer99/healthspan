# Development Plan

High-level code development plan: the phase sequence from empty repository to a complete v1 platform. This is a living design document — phases are re-scoped as implementation teaches us things — but the **sequencing constraints** and **decision gates** recorded here are binding until explicitly revisited.

Written 2026-07-09, at the close of the spec phase: 34 ADRs Accepted, CI skeleton live ([ADR-0045](adr/0045-repository-workflow-and-ci-enforcement.md)), decision-capture convention in [CLAUDE.md](../CLAUDE.md), no code yet. **Status markers on the phase headings below are the plan's rolling state — as of 2026-07-18, Phases 0–3 are complete, real data entry has begun, and Phase 3.5 (data-lifecycle hardening) is inserted next.**

---

## Plan shape

Phases **end at usable milestones**, and are **vertical slices by default** — not horizontal layers built ahead of need. The guiding constraint that set the early sequence: reach "real lab results in an encrypted database, queryable" as early as possible (end of Phase 3), because accumulated real data is itself the design trigger for several deliberately deferred decisions (subjective-observation vocabulary, [ADR-0044](adr/0044-derived-data-points.md) derived-data schema — see [open-questions.md](open-questions.md)).

A **horizontal slice earns its place** only when real usage — not speculation — proves a cross-cutting gap that no vertical owns, and only if it still ends at a crisp, testable milestone. Phase 3.5 is the first: real data entry revealed a data-lifecycle-integrity gap (catalog hygiene, entry fidelity) that recurs at every later ingress. That is the anti-speculative-layering principle *working*, not an exception to it — the need was demonstrated, not guessed. Later phases stay coarse and vertical; the same catalog-hygiene concern is **inherited explicitly** by Phase 6 (GUI catalog CRUD) and Phase 7 (import at scale), not rediscovered there.

Rules that apply to every phase:

- Each phase decomposes into PR-sized work items. Every PR carries a `Decisions:` section per the [CLAUDE.md](../CLAUDE.md) implementation decision capture convention.
- Phase acceptance = the [testing-strategy.md](testing-strategy.md) gates for that layer green in CI, plus the milestone demonstrably working.
- **Deferred-with-trigger questions stay deferred.** The plan cites triggers; it never resolves them ahead of their trigger. The authoritative list is [open-questions.md](open-questions.md).
- API-surface decisions land in [api-reference.md](api-reference.md) in the same PR that implements them.

---

## Phase 0 — Skeleton and CI activation (the first code PR) — ✅ complete 2026-07-09 (PR #6)

An installable, empty-but-real `healthspan` package: uv build backend, `src/` layout, Python 3.14, ruff + pyright (strict) configuration, one trivial test.

This PR executes the **entire first-code-PR checklist** from [ADR-0045](adr/0045-repository-workflow-and-ci-enforcement.md):

- Add the remaining CI gates with pinned tool versions: ruff lint+format, pyright strict, the full 3-OS × Python 3.14 test matrix, the log canary, pip-audit (PR step + daily schedule).
- Apply the checked-in ruleset (`.github/rulesets/main-protection.json`).
- Align the repository merge-method settings with the ruleset (squash + rebase only).

**From this PR forward, direct-to-main ends.** All changes go through PRs gated on the `ci-ok` aggregate check.

**Milestone:** `uv pip install -e .` works on all three OSes; CI matrix green; branch protection active.

## Phase 1 — Data foundation (CLI-only, no server) — ✅ complete

Everything below the API line, exercisable entirely through the CLI — `healthspan db migrate` and `healthspan db backup` are the two sanctioned direct-database exceptions ([ADR-0006](adr/0006-application-architecture.md)), so no server is needed to make this phase real.

- Shared TOML configuration loading ([ADR-0006](adr/0006-application-architecture.md), [ADR-0046](adr/0046-filesystem-layout-and-config-discovery.md)).
- SQLCipher database provisioning, key derivation and rotation ([ADR-0013](adr/0013-encryption-at-rest.md), [ADR-0028](adr/0028-key-derivation-and-rotation.md)).
- The migration runner ([ADR-0009](adr/0009-database-migration.md), [ADR-0035](adr/0035-migration-execution-semantics.md)).
- **Migration 0001** — the fully-specified schema from [data-model.md](data-model.md) and its owning ADRs: supersession columns and `*_current` views ([ADR-0027](adr/0027-audit-trail-and-corrections.md)), append-only audit triggers, the four-column timezone convention ([design-rationale.md](design-rationale.md)), FTS5 external-content table and sync triggers ([ADR-0041](adr/0041-clinical-document-fts.md)), biomarker identity and value model ([ADR-0030](adr/0030-biomarker-identity.md)), UCUM unit columns ([ADR-0031](adr/0031-units-and-ucum.md)).
- `healthspan db backup` with verification, defaults daily / retain 14 ([ADR-0038](adr/0038-backup-execution-and-verification.md)).
- Typer CLI skeleton hosting the above ([ADR-0006](adr/0006-application-architecture.md)).

**Tests:** migration execution semantics, audit-trail immutability, FTS trigger sync, backup verification round-trip, and the start of the property-based UCUM suite ([testing-strategy.md](testing-strategy.md)) — **storage fidelity only; no unit conversion yet** (the [ADR-0031](adr/0031-units-and-ucum.md) engine sub-decision is not required for this phase).

**Milestone:** an encrypted database exists on disk, migrated to schema 0001, with a verified backup.

## Phase 2 — Core Service minimum — ✅ complete

The FastAPI Core Service, smallest useful surface:

- Startup sequence and passphrase handoff ([ADR-0039](adr/0039-startup-sequence-and-passphrase-handoff.md)); single-instance locking ([ADR-0042](adr/0042-process-supervision-and-single-instance-locking.md)). This phase ships **direct-start only** (`healthspan service start`, foreground) — with a single Core Service process there is nothing for a launcher to orchestrate. The launcher (ADR-0039's migration ownership + passphrase-over-stdin handoff) arrives in Phase 4 with the MCP Server, the second process; full supervision and full-auto-unlock arrive with Phase 6 ([ADR-0049](adr/0049-core-service-skeleton-implementation-decisions.md)).
- Named scoped token authentication ([ADR-0026](adr/0026-named-scoped-tokens.md)).
- Health and liveness endpoints ([ADR-0040](adr/0040-health-endpoint-authentication.md)); structured logging and metrics per [observability.md](observability.md) — the CI log canary gets real material here.
- The **bulk import endpoint** ([ADR-0004](adr/0004-data-ingestion-strategy.md)): full-batch validation, dry-run, atomic transactions, audit row in the same transaction ([ADR-0027](adr/0027-audit-trail-and-corrections.md)).
- Basic read/query endpoints for the data entered so far.

**Milestone:** an authenticated client can write and read data through the REST API; nothing but the two CLI exceptions touches the database directly.

## Phase 3 — Manual data entry end-to-end ← first real-value milestone — ✅ complete 2026-07-17 (PRs #24, #26, #28, #31)

- Reference data: biomarker catalog, lab sources, reference range frameworks ([ADR-0005](adr/0005-reference-range-frameworks.md)) and their read endpoints.
- CLI manual-entry tooling with a draw-level template — enter lab + draw date once, then results — resolving the manual-entry-efficiency question in [open-questions.md](open-questions.md).
- Reference range **comparison** with unit-normalized evaluation ([ADR-0005](adr/0005-reference-range-frameworks.md), [ADR-0031](adr/0031-units-and-ucum.md)).

**Decision gates entering this phase** (owned by the database owner):

1. ~~The [ADR-0031](adr/0031-units-and-ucum.md) conversion-engine sub-decision~~ — **resolved 2026-07-09: `ucumvert` (+ `pint`) behind an internal units module** ([ADR-0031](adr/0031-units-and-ucum.md), now Accepted). The property-based suite in [testing-strategy.md](testing-strategy.md) is the acceptance harness. `ucumvert`/`pint` land as new runtime dependencies here (the pip-audit CI gate has covered the dependency tree since Phase 1's first runtime dependencies — [ADR-0045](adr/0045-repository-workflow-and-ci-enforcement.md)).
2. ~~**Biomarker category taxonomy**~~ — **resolved 2026-07-14: first-class `categories` table + `category_id` FK, reserved `not_assigned` default, physiological-system seed vocabulary** ([ADR-0055](adr/0055-biomarker-category-taxonomy.md), Proposed). Lands with this phase's reference-data work item.
3. ~~The **name-based alias fallback**~~ — **resolved 2026-07-14: `biomarker_aliases` table with server-side exact-match resolution; the import endpoint accepts `biomarker_name`** ([ADR-0054](adr/0054-biomarker-name-alias-fallback.md), Proposed). Lands with this phase's reference-data work item.

Both critical-path gates are now decided; Phase 3 is fully ungated.

**Milestone:** real lab results entered into the encrypted database via the CLI, range-flagged correctly, queryable. Real data begins accumulating — which starts the clock on the accumulation-triggered deferrals.

## Phase 3.5 — Data-lifecycle hardening (inserted 2026-07-18)

*Identified after Phase 3 closed, when real data entry exposed gaps no vertical phase owns. Numbered with a decimal deliberately: it inserts a slice without renumbering Phases 4–8, whose numbers are bound to concepts ("Phase 4 = the event-bus/MCP surface", "Phase 7 = ingestion adapters") and referenced from Accepted, immutable ADRs (e.g. [ADR-0052](adr/0052-bulk-import-identity-and-conflict-resolution.md), [ADR-0053](adr/0053-read-endpoint-surface-and-pagination.md)) — a renumber would falsify those references and cannot edit the immutable bodies to follow. See "Plan shape" on why this horizontal slice earns its place.*

Phase 3 delivered catalog **growth** (add a biomarker / lab / alias) but no catalog **correction**, and `enter` captures a result's value but not the lab's own printed reference range. The first real report exercised both gaps within one session: a same-concept duplicate biomarker with no in-app way to remove or merge it, and lab-provided ranges dropped on entry. These are not Phase-3 bugs — they are a horizontal **data-lifecycle** concern (catalog hygiene, entry fidelity) that recurs at every later data ingress. Left unhardened they also block Phase 4, whose milestone is MCP over real data *with full fidelity* — which needs a clean, correctable dataset, and manual entry is the practical ingress for the owner's own reports until Phase 7's source adapters. (The Phase-2 bulk-import API is a live authenticated write path too, but it already accepts the `reference_low` / `reference_high` / `reference_text` columns — `imports.py` — so the reference-range fidelity gap is specific to the `enter` client, not the write path.)

Scope — deliberately bounded to the orphan, **not** a second Phase 3:

- **Catalog correction** — `biomarkers` / `labs` remove (unreferenced rows), merge (re-point every `biomarker_id`-dependent row — `lab_results`, `biomarker_aliases`, `framework_ranges` — onto a surviving row), and first-class alias management, closing the [open-questions.md](open-questions.md) "CLI catalog editing" deferral and the alias-vs-duplicate trap that produces same-concept duplicates.
- **Entry fidelity** — `enter` captures the lab's printed reference range into the existing `lab_results.reference_low` / `reference_high` / `reference_text` columns (added by migration 0001, serialized on reads per [ADR-0053](adr/0053-read-endpoint-surface-and-pagination.md)), which the schema already carries — and the `results` readback already shows ([ADR-0059](adr/0059-cli-manual-entry-implementation-decisions.md) §5) — but the `enter` write path leaves `NULL`.
- **A fuller built-in catalog** — the survey's biomarker/alias/category findings seed a more complete starter catalog (names and categories are risk-free reference data; a generous seed makes the tool more useful, not less). Units are *not* risk-free — a seeded `canonical_unit` must be valid UCUM and convertible, the same check `test_migration_0005` already enforces for seeded range units — so they earn that validation, not a free pass. Range *values* stay conservative — only the validated cells — because a wrong range mis-flags where a missing one merely stays silent.

Explicitly **out of scope** (owned elsewhere, must not be pulled forward, or Phase 3.5 becomes a second Phase 3 that never closes):

- The **range-model expressiveness** cluster — cohort dimensions (sex/age/fasting), non-numeric (categorical/ordinal) comparison, exclusive bounds, `physical_min` — stays in its own range-model PR ([open-questions.md](open-questions.md), Schema), each on its own trigger.
- The **derived-data** schema stays [ADR-0044](adr/0044-derived-data-points.md)-deferred to Phase 5; the interim rule, now written into the manual-entry policy, is *do not enter derived values* — every one reconstructs from stored components (the reproducible calcs) or from its stored inputs (the opaque ones), so deferring costs no information.

**Scoping instrument — the source survey.** Phase 3.5's precise work items are finalized by a deliberate **discovery activity**: reviewing the owner's real reports across all sources to enumerate the full set of data dimensions up front, rather than migrating the schema reactively one report at a time. Its output is [lab-data-dimensions.md](lab-data-dimensions.md) — a generic domain model of value shapes, range dimensions, catalog-structure needs, and source-format quirks, each marked *validated by real data* vs *known-needed but unexercised* (the axes the owner's single corpus cannot itself exercise: female / pediatric / pregnancy ranges, SI-default units, specialty result types). The survey enumerates *reality*; its **decisions** route to ADRs / [data-model.md](data-model.md) / [open-questions.md](open-questions.md) as usual, so it never becomes a shadow spec.

**Containment.** The survey analyzes real reports **in chat or under `specs/personal/`** (gitignored, raw corpus staged in `specs/personal/ingestion/`) and writes only the *structural distillation* — value shapes, dimensions, biomarker names/units, source-format quirks — to the public survey. Personal values, result patterns, and identifiers never leave `specs/personal/`. This is the [CLAUDE.md](../CLAUDE.md) containment rule applied to a discovery activity; the `/land` containment gate and `spec-reviewer` are the backstops.

**Milestone:** the manual-entry catalog is correctable (no unfixable duplicates) and entry is faithful (lab ranges captured) — a clean, trustworthy dataset ready to be Phase 4's MCP substrate.

## Phase 4 — AI surface: events, jobs, MCP

- The **launcher** ([ADR-0039](adr/0039-startup-sequence-and-passphrase-handoff.md), [ADR-0008](adr/0008-process-lifecycle.md)): the second process (the MCP Server) is the first thing worth orchestrating, so the launcher lands here — it owns migration execution, hands the passphrase to Core Service over stdin, and starts the stack in health-gated dependency order ([observability.md](observability.md)). Phase 2 shipped direct-start (`healthspan service start`) instead; full supervision (restart-with-backoff, circuit breaker) and full-auto-unlock are deferred to Phase 6 ([ADR-0042](adr/0042-process-supervision-and-single-instance-locking.md), [ADR-0049](adr/0049-core-service-skeleton-implementation-decisions.md)).
- Event bus and SSE stream ([ADR-0011](adr/0011-event-bus.md)).
- Job abstraction ([ADR-0012](adr/0012-job-abstraction.md)); imports become jobs ([ADR-0025](adr/0025-plugin-host-process-matrix.md)).
- MCP server: fastmcp, Streamable HTTP ([ADR-0007](adr/0007-mcp-transport.md), [ADR-0029](adr/0029-mcp-streamable-http.md)), implementing the full **tool output contract** already specified in [api-reference.md](api-reference.md) — censoring fidelity, structured output, pagination caps, instruction-shielded free text.
- Export endpoints ([ADR-0015](adr/0015-data-export.md)).
- **Disposition of [ADR-0014](adr/0014-websocket.md)** (WebSocket — Proposed, never on a flip list): the SSE implementation work makes it naturally resolvable; expected outcome is supersession/subsumption by [ADR-0011](adr/0011-event-bus.md). Mechanics decided 2026-07-17: since ADR-0011 is Accepted and immutable (it cannot absorb the rationale itself), the disposition is recorded as a **small new ADR that supersedes ADR-0014** — the new ADR carries the one-line rationale, 0014's status flips to `Superseded by ADR-XXXX`, and 0011 gains a navigation link.

**Milestone:** an AI client connects over MCP and queries real health data with full value fidelity.

## Phase 5 — Documents and interpretation

- Clinical document storage ([ADR-0034](adr/0034-clinical-document-storage.md)) and plaintext artifact disposal ([ADR-0033](adr/0033-plaintext-artifact-disposal.md)).
- FTS query surface over document bodies ([ADR-0041](adr/0041-clinical-document-fts.md)).
- Analyses table and the `annotate` scope ([ADR-0043](adr/0043-ai-authored-analyses-and-annotate-scope.md)); provenance invariant INV-6 enforced ([provenance-and-derived-data.md](provenance-and-derived-data.md)).

With analyses live and `result_data` attachments accumulating, the [ADR-0044](adr/0044-derived-data-points.md) derived-schema design trigger eventually fires — deferred until then, per that ADR.

**Milestone:** lab PDFs and clinical notes stored, searchable, and annotatable by the AI surface.

## Phase 6 — GUI and launcher

- Full launcher: process supervision, restart policy, process reports ([ADR-0008](adr/0008-process-lifecycle.md), [ADR-0042](adr/0042-process-supervision-and-single-instance-locking.md)), completing the passphrase handoff chain ([ADR-0039](adr/0039-startup-sequence-and-passphrase-handoff.md)).
- PySide6 GUI shell calling the same REST API as every other client ([ADR-0006](adr/0006-application-architecture.md)).

CI runs Qt tests headless (`QT_QPA_PLATFORM=offscreen` per [ADR-0045](adr/0045-repository-workflow-and-ci-enforcement.md)); visual verification is local and manual. Ordered after the MCP phase deliberately: for a single power user the AI surface delivers more value sooner, and the GUI benefits from an API stabilized by two client implementations (CLI, MCP) before it.

**Milestone:** launcher starts the platform end-to-end; GUI browses real data.

## Phase 7 — Ingestion adapters (parallel wave)

**Plugin host machinery first** ([ADR-0010](adr/0010-cli-plugin-model.md), [ADR-0024](adr/0024-plugin-extensions.md), [ADR-0025](adr/0025-plugin-host-process-matrix.md), [ADR-0036](adr/0036-plugin-package-installation-integrity.md)) — import adapters are a plugin type, so the interface lands before the wave.

Then per-source adapters, **each gated only on its own investigation** ([open-questions.md](open-questions.md), Data Ingestion) and blocking nothing else:

- Levels watch-folder import (gated on inspecting a real export of each of the four types).
- Dexcom Developer API (gated on the API investigation).
- Apple Health XML; Fitbit Takeout; Samsung Health export; InBody body-composition exports — each gated on its own format investigation.

The CGM importer wakes two deferred questions when it lands: CGM indexing strategy and time-series aggregates ([ADR-0021](adr/0021-time-series-aggregation.md); 2026-07-06 review item 3.F).

**Milestone:** at least one automated source flowing through the bulk import endpoint as jobs.

## Phase 8 — Distribution and automation

- Packaging and distribution ([ADR-0023](adr/0023-distribution-mechanism.md)); publish.yml gains its release-blocking pip-audit step with the first release ([ADR-0045](adr/0045-repository-workflow-and-ci-enforcement.md)).
- Automation rules and notification channels — **requires promoting the [ADR-0016](adr/0016-automation-plugin-type.md) and [ADR-0017](adr/0017-notification-channels.md) stubs to real Proposed ADRs first.**

Deliberately the vaguest phase; it will be re-planned when Phase 7 is underway.

---

## Parallel track (database owner, off the critical path until Phase 7)

- Inspect one real Levels export of each of the four types.
- Dexcom Developer API scoping (account, scopes, rate limits).
- Remaining source investigations as convenient (Apple Health, Samsung, Fitbit, InBody).

## Decision gates summary

| Gate | Needed by | Owner |
|---|---|---|
| ~~[ADR-0031](adr/0031-units-and-ucum.md) conversion engine~~ — **decided 2026-07-09: `ucumvert` + `pint`** | — | — |
| ~~Biomarker category taxonomy~~ — **decided 2026-07-14: `categories` table + `category_id` FK, reserved default ([ADR-0055](adr/0055-biomarker-category-taxonomy.md))** | — | — |
| ~~Name-based alias fallback~~ — **decided 2026-07-14: `biomarker_aliases` + server-side exact-match resolution ([ADR-0054](adr/0054-biomarker-name-alias-fallback.md))** | — | — |
| [ADR-0014](adr/0014-websocket.md) disposition | Phase 4 (during SSE work) | Database owner |
| [ADR-0016](adr/0016-automation-plugin-type.md)/[ADR-0017](adr/0017-notification-channels.md) promotion | Phase 8 | Database owner |
| Per-source format investigations | Phase 7 (per adapter) | Database owner |

Everything else is either decided (Accepted ADRs) or deferred with an explicit trigger in [open-questions.md](open-questions.md).

## Links

- [ADR-0006](adr/0006-application-architecture.md) — the component map the phases slice through
- [ADR-0045](adr/0045-repository-workflow-and-ci-enforcement.md) — CI enforcement; Phase 0 executes its first-code-PR checklist
- [open-questions.md](open-questions.md) — authoritative list of open and deferred decisions
- [testing-strategy.md](testing-strategy.md) — the per-layer gates that define phase acceptance
- [CLAUDE.md](../CLAUDE.md) — implementation decision capture convention binding on every PR
