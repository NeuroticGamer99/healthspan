# ADR-0052: Bulk-Import Identity, Natural Keys, and Conflict Resolution (Phase 2 WI-3)

## Status
Proposed

## Context and Problem Statement

Phase 2 WI-3 implements the bulk import endpoint ([ADR-0004](0004-data-ingestion-strategy.md)) and the repository/audit layer that writes `audit_log` in the mutation transaction ([ADR-0027](0027-audit-trail-and-corrections.md)). Both ADRs fix the *behavior* — full-batch validation, atomic transactions, dry-run, the explicit `reject`/`skip`/`upsert` policies, batch-level insert audit, supersession-on-upsert — but leave three things undecided that implementation cannot proceed without:

1. **What identifies a row.** `reject`/`skip`/`upsert` all turn on "does this incoming row conflict with an existing one" — but the data tables have no natural unique constraint beyond their surrogate `id`, and an import client cannot know the server-assigned `id` of a row it has not yet inserted. A lab panel is also *relational within one batch*: a `lab_results` row's `lab_draw_id` must point at a `lab_draws` row created in the same request, whose real `id` does not exist until insert time.
2. **What "the same row" means per table.** ADR-0027's conflict-policy semantics ("incoming row identical → no-op; genuinely differs → supersede") presume a match key and a notion of which columns count as a value change. Both are per-table.
3. **How the natural key is enforced** so the engine cannot create two current rows that collide.

Phase 2 registers only the two lab tables as importable (the milestone imports the Phase-1 lab fixtures; other content tables gain import support with their Phase-7 adapters — [development-plan.md](../development-plan.md)). The repository engine is built table-agnostic; this ADR fixes the contract for the two tables wired now.

Per the CLAUDE.md decision-capture convention these land as one Proposed ADR: the identity model constrains every future importable table and the conflict rules extend how ADR-0027's supersession applies through the import path ([ADR-0049](0049-core-service-skeleton-implementation-decisions.md)/[ADR-0050](0050-token-store-and-auth-implementation-decisions.md) WI-decisions-bundle precedent).

## Decision Drivers

- ADR-0004's guarantees are binding: validation before any write, all errors collected together, one atomic transaction, explicit conflict policy, dry-run that writes nothing
- ADR-0027's integrity rules are binding: a clinical value can never silently vanish from a data table; corrections supersede and keep both images; bulk inserts audit at batch level, never per row
- ADR-0030's value model must round-trip through import unchanged (comparator, qualitative `value_text`); ADR-0031 units are stored as reported, no conversion this phase ([project decision, 2026-07-11](../development-plan.md))
- The import client owns *content*, not database identity — surrogate keys are the store's, so the API must not require the client to know or supply them
- Single-writer Core (INV-1) plus `BEGIN IMMEDIATE` gives serialized writes, but the natural-key invariant should survive a bug or a future second writer, not rest on discipline alone

## Decision Outcome

### 1. Payload shape and identity: per-table row map, `id`s are batch-local handles

`POST /v1/import` carries a per-table map of rows — the same shape as the Phase-1 synthetic fixtures (`{"lab_draws": [...], "lab_results": [...]}`) — alongside a required `conflict_policy` and the batch provenance (`source`, optional `adapter_id`/`adapter_version`/`note`). A row may carry an `id`, but it is a **batch-local handle**, used only to wire intra-batch foreign keys: a `lab_results` row's `lab_draw_id` names the payload `id` of a `lab_draws` row in the *same* request. The server assigns real primary keys and rewrites the child FK to the resolved value. A handle never becomes persistent identity and never matches an existing row.

Foreign keys that point *outside* the batch — `lab_id`, `biomarker_id` — are validated for existence against the catalog (structural validation per the [2026-07-11 project decision](../development-plan.md); name→canonical resolution and catalog seeding stay Phase 3). This is why the milestone seeds the catalog first, then imports lab data.

### 2. Conflict identity is a defined natural key per table, enforced in schema

Matching is by a **natural business key**, never by surrogate `id`:

| Table | Natural key | Rationale |
|---|---|---|
| `lab_draws` | `(lab_id, draw_utc)` | one blood-draw event at one lab at one instant |
| `lab_results` | `(lab_draw_id, biomarker_id)` | one result per biomarker within a draw |

`lab_results` keys on the *resolved* `lab_draw_id` — the reused-or-created draw's real id (§3) — so a re-imported panel's results match the results already under that draw. Migration 0003 makes each key a partial `UNIQUE` index `WHERE superseded_by IS NULL`, so the invariant holds over *current* rows while superseded rows drop out of the index (a replacement becomes the key's single current occupant). The engine relies on the constraint as the authoritative duplicate detector rather than a check-then-insert race.

### 3. Per-table conflict resolution: values supersede, identity reuses

The two tables play different structural roles, so ADR-0027's two mutation categories ("value corrections supersede; designated metadata repairs update in place") map onto them cleanly:

**`lab_results` — value rows.** The clinical value lives here (`value_num`, `comparator`, `value_text`, `unit`, `reference_*`, `notes`). Any genuine difference on those columns is a **value correction**: insert the corrected row, set the existing row's `superseded_by`, write one per-row `correct` `audit_log` row with both images and an auto `reason` (`upsert re-import, batch <id>`), count it `rows_corrected`. Identical → **no-op**, `rows_unchanged`. Absent → **insert**, counted `rows_inserted` (batch-level audit only, §4).

**`lab_draws` — identity/container rows.** A draw is an identity: its key columns `(lab_id, draw_utc)` *are* the match, and everything else (`draw_local_recorded`, `draw_local_tz`, `draw_tz_inferred`, `draw_context`, `fasting`, `notes`) is **designated metadata** in ADR-0027's sense — descriptive context of the draw event, not a clinical value. So a matched draw is **reused** (its id resolves the batch's child FKs), and a genuine metadata difference under `upsert` is an **in-place `update`** (`operation = 'update'`, full old/new images audited), never a supersession. This is deliberate: superseding a draw would assign a new id and orphan every result already pointing at the old one; keeping the draw id stable is what lets §2's result key match across re-imports. A draw therefore never supersedes through import, and no clinical value is ever in a draw to lose. The designated-metadata declaration for `lab_draws` is recorded in [data-model.md](../data-model.md).

Policy mapping (a *conflict* = an incoming row whose natural key already exists and whose compared columns differ):

| Policy | New key | Identical | Differs (result) | Differs (draw metadata) |
|---|---|---|---|---|
| `reject` | insert | no-op | batch fails, nothing written | batch fails |
| `skip` | insert | no-op, `rows_unchanged` | keep existing, `rows_skipped` | keep existing, `rows_skipped` |
| `upsert` | insert | no-op, `rows_unchanged` | supersede, `rows_corrected` | in-place update, `rows_corrected` |

`reject` fails the whole batch on the *first* conflict discovered during validation, but — per ADR-0004 — validation collects **all** errors first, so the response names every conflicting row, not just one.

### 4. Audit and provenance: batch-level for inserts, per-row for changes

Realizing ADR-0027's granularity rule: a committed import writes one `import_batches` provenance row and, per table the batch *targeted*, exactly one `audit_log` row with `operation = 'import'`, `row_id = NULL`, `import_batch_id` set, and a summary `new_values` JSON `{rows_inserted, rows_corrected, rows_skipped, rows_unchanged, conflict_policy, source, adapter_id, adapter_version}`. **Zero** per-row `insert` audit rows — the imported rows each carry `import_batch_id`, which with the batch row answers "where did this come from". Mutations of *existing* rows keep per-row image audit: `correct` for a result supersession, `update` for a draw metadata repair. `actor` is the authenticating token name ([ADR-0026](0026-named-scoped-tokens.md), `request.state.token`). Everything — provenance row, data writes, audit rows — commits in the one transaction; a rollback (any validation-clean batch that fails mid-write, or a `reject` conflict) leaves no `import_batches` row and no audit row, and a dry-run writes nothing at all.

`data.imported`/`data.corrected` event emission is deferred to Phase 4 with the event bus ([project decision, 2026-07-11](../development-plan.md)); the `audit_log` rows are the durable record until then.

### 5. Endpoint surface

`POST /v1/import`, scope `import`. `?dry_run=true` runs full validation and returns the would-be summary counts, writing nothing. `conflict_policy` is a **required** body field (`reject`|`skip`|`upsert`) — there is no implicit default that mutates data (ADR-0004). The structured response carries per-table summary counts and, on validation failure, the full collected error list; `422` for a batch that fails validation (unknown FK, value-model violation, unresolvable intra-batch handle, or a `reject` conflict), `200` for a committed or dry-run batch. The concrete request/response/error shapes are recorded in [api-reference.md](../api-reference.md), Data import, in this PR.

ADR-0004's "structured response" lists *rows rejected* as a success-response field. Under this design there is no partial-success `200` — a `reject` conflict fails the whole batch before any write, and `skip`/`upsert` never reject a row — so a rejected-row count in a success body is structurally unreachable. That information ships instead as the per-row `422` error list, which names every offending row. The other three counts map directly: rows imported → `rows_inserted`, rows skipped → `rows_skipped` (plus `rows_corrected`/`rows_unchanged`, which ADR-0004 did not distinguish).

### Positive Consequences

- The client never needs a server id: content-only payloads, identity owned by the store, intra-batch relations expressed with handles
- Re-importing a whole panel is stable and cheap: unchanged rows no-op, only genuinely-changed values supersede, and the draw id never moves so result matching holds
- The natural-key invariant is real schema, not convention — a bug or a future direct writer that duplicates a current row is rejected by the constraint
- No clinical value can vanish: results supersede with both images; draws carry no value to lose

### Negative Consequences / Tradeoffs

- The natural keys are a commitment: two distinct draws at the same `(lab_id, draw_utc)`, or two results for the same biomarker in one draw, are unrepresentable as *current* rows. Both are clinically implausible; if one ever arises it is a new ADR and a migration, not a silent workaround
- Treating draw metadata as in-place `update` means a draw-context correction is not a supersession chain — accepted: ADR-0027 designates exactly this class of repair, and the immutable `*_local_recorded` still records what the source said
- Only two tables are importable this phase; the engine is generic but the natural keys and column classifications for other tables are deferred to their adapters (ADR-0052 is extended, not contradicted, when they land)

## Consequences for Other Documents

- **[ADR-0004](0004-data-ingestion-strategy.md)** (Accepted, partially): navigation link — the endpoint shape, identity model, and per-table conflict resolution concretize its "to be finalized" import behavior
- **[ADR-0027](0027-audit-trail-and-corrections.md)** (Accepted): navigation link — the value/metadata mapping onto `lab_results`/`lab_draws` and the batch-audit realization apply its rules unchanged
- **[api-reference.md](../api-reference.md)**: Data import endpoint — request/response/error shapes, status codes, scope — same PR
- **[data-model.md](../data-model.md)**: `lab_draws`/`lab_results` natural keys and indexes (migration 0003); `lab_draws` designated-metadata declaration; `import_batches` as the import provenance record — same PR
- **[open-questions.md](../open-questions.md)**: no new deferral; the manual-entry-efficiency and alias-fallback entries stay Phase 3

## Links

- Implements: [ADR-0004](0004-data-ingestion-strategy.md) — bulk import endpoint behavior
- Implements: [ADR-0027](0027-audit-trail-and-corrections.md) — in-transaction audit, supersession, batch-level insert audit
- Related: [ADR-0030](0030-biomarker-identity.md) — the value model import round-trips (comparator, qualitative results)
- Related: [ADR-0031](0031-units-and-ucum.md) — units stored as reported; no conversion this phase
- Related: [ADR-0026](0026-named-scoped-tokens.md) — `import` scope; `actor` from token identity
- Related: [ADR-0035](0035-migration-execution-semantics.md) — migration 0003 discipline; "drift fails loudly"
- Related: [ADR-0037](0037-core-service-concurrency-and-driver.md) — the thread-affine pool the repository writes through
- Related: [ADR-0049](0049-core-service-skeleton-implementation-decisions.md), [ADR-0050](0050-token-store-and-auth-implementation-decisions.md) — the WI-decisions-bundle pattern
