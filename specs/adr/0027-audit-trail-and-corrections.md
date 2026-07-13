# ADR-0027: Audit Trail and Data Corrections — Event Sourcing Rejected

## Status
Accepted

## Context and Problem Statement
Three intertwined schema questions in [open-questions.md](../open-questions.md) block migration 0001: how mutations are audited, how incorrect data is corrected without losing history, and whether event sourcing should be the storage model that solves both at once. They must be decided together — an audit trail bolted on after the first migration has no record of anything that came before it, and the correction pattern shapes every data table's schema.

The candidate patterns pull in different directions. Full event sourcing makes the append-only event stream the authoritative data and derives current state as materialized views — audit and correction fall out for free, at the price of permanent read-model machinery. The traditional alternative — mutable rows plus a separately-written audit log — is simpler but historically drifts: any code path that forgets the audit write silently corrupts the integrity record.

## Decision Drivers
- Every data mutation must leave a user-facing integrity record from migration 0001 onward — this is clinical data; "what changed, when, by what" must always be answerable
- Corrections must preserve history: an incorrectly entered lab value that gets fixed must keep both values visible, permanently
- "What did we believe on date X, before the correction on date Y" must remain answerable (longitudinal analysis over corrected data)
- The audit record must be structurally unable to drift from the data it describes
- Write volume is bimodal — a few imports per week plus rare manual corrections, but a single bulk import can carry millions of rows (CGM backfill — data-model.md); read patterns are analytical. Complexity and audit storage must be proportionate to *this* workload
- Recovery already has a story: encrypted backups (ADR-0013, ADR-0019). The storage model does not need to double as the recovery mechanism
- Must work in SQLite/SQLCipher with plain SQL — no dedicated event store

## Considered Options
1. **Full event sourcing + CQRS** — append-only event stream as the authoritative data; current state materialized into read views
2. **Mutable rows + independently-written audit log** — audit as a parallel concern, written wherever mutation code remembers to
3. **Mutable current-state tables + in-transaction `audit_log` + `superseded_by` corrections + CQRS-lite** — audit and correction as first-class schema features of a conventional relational model

## Decision Outcome
Chosen option: **Option 3.**

The deciding observation: everything event sourcing would buy this platform, the lighter pattern also delivers. The audit trail comes from an append-only `audit_log` written in the same transaction as every mutation — transactionally unable to drift. Natural corrections come from `superseded_by` supersession chains. Time-travel queries come from walking those chains against audit timestamps. The one capability genuinely lost is *replay as the rebuild/recovery mechanism* — and recovery is already owned by encrypted backups (ADR-0019), which a single-user store needs regardless.

What option 1 would cost, forever: every read path in the system — importers, analysis plugins, MCP tools, GUI dashboards — queries materialized projections instead of tables, and those projections must be kept transactionally consistent with the stream; every schema change becomes an event-versioning problem; every contributor must understand fold/replay semantics to touch the data layer. That is CQRS infrastructure carried permanently to solve write-contention and integration problems a single-writer analytical store does not have.

Option 2 is the drift trap the audit requirement exists to prevent, and is rejected outright.

### Positive Consequences
- The audit trail cannot drift: the audit row commits or the mutation doesn't
- Queries stay plain SQL against current-state tables — no projection layer between any reader and the data
- Corrected history is permanent and visible; "show me this result before and after correction" is a walk of the supersession chain
- Provenance is rich for free: ADR-0026 puts an authenticated token identity on every write, so every audit row records *who* — plus the import batch or job that carried the change
- ADR-0021's aggregates get a clean contract: derived, rebuildable read models, never authoritative — with `data.corrected` / `data.deleted` events as their invalidation signal

### Negative Consequences / Tradeoffs
- Every current-state query must exclude superseded rows (`WHERE superseded_by IS NULL`) — mitigated by per-table `*_current` views defined in the same migrations (see below)
- `audit_log` grows without bound by design — but it scales with *mutations*, not data volume (bulk-import inserts audit at batch level, below), and is deliberately never pruned
- Old/new value JSON duplicates mutated data into the audit table — an accepted storage cost for the mutation record; bulk-import inserts are deliberately *not* duplicated (see batch-level audit below)
- No stream replay: rebuilding a damaged database means restoring a backup, not refolding events

---

## The `audit_log` Table

Created in **migration 0001**, before any data table receives a row.

| Column | Content |
|---|---|
| `id` | Monotonic primary key |
| `table_name`, `row_id` | What was touched (`row_id` is `NULL` for batch-level `import` rows) |
| `operation` | `insert`, `update`, `correct`, `delete`, `import` (see semantics below) |
| `old_values`, `new_values` | Full row images as JSON (`NULL` where inapplicable: no `old_values` on insert, no `new_values` on delete) |
| `occurred_at_utc` | System timestamp, UTC only — the timestamp quadruple is for clinically meaningful times; audit rows are system events |
| `actor` | Token name from the authenticated request (ADR-0026); job tokens (`job:<uuid>`) identify job children |
| `import_batch_id` | FK, `NULL` unless the mutation came through bulk import (ADR-0004) |
| `job_id` | FK, `NULL` unless the mutation was performed by a job (ADR-0012) |
| `reason` | Free text, `NULL` unless supplied (correction reasons, delete justifications) |

**Append-only, enforced in the schema.** Triggers were rejected for audit *capture* (below), but they are exactly right for audit *immutability*: migration 0001 installs `BEFORE UPDATE` and `BEFORE DELETE` triggers on `audit_log` that `RAISE(ABORT)`. Even a bug in first-party code cannot rewrite history.

**One table, three records — deliberately distinct:**

| Record | Purpose | Defined in |
|---|---|---|
| `audit_log` | User-facing **data integrity** record — what happened to my health data | this ADR |
| `auth_audit` | **Security** record — authentication and authorization outcomes | ADR-0026 |
| Application logs | **Operational** record — never contain health data | observability.md |

## Capture Mechanism: Application Layer, Not Triggers

Audit rows are written by the Core Service's data-access layer, inside the mutation's transaction. This was weighed against SQLite triggers, whose appeal is structural: they fire on every mutation regardless of code path and cannot be forgotten. Triggers lost on three grounds:

1. **Triggers cannot see provenance.** The actor token, import batch, job ID, and reason exist only in the application request context. SQLite has no session variables; smuggling context to triggers requires a per-connection temp-table protocol — more machinery than the drift risk it removes.
2. **The drift risk is already architecturally bounded.** The Core Service is the *only* process that can open the database (ADR-0025, INV-1), and every write funnels through the REST API (ADR-0004) into one first-party repository layer. "Some code path forgot to audit" reduces to a bug in a single module — testable, reviewable, first-party.
3. **Trigger bodies enumerating every column as JSON** would need regeneration with every migration — a standing maintenance tax.

The residual risk is closed mechanically: the test suite includes a **mutation-matrix test** with two contracted shapes — every per-row mutation path (`insert`, `update`, `correct`, `delete`), against every table, asserts exactly one `audit_log` row *per mutated row* in the same transaction; the bulk-import path asserts exactly one `import` row per (batch, table) whose summary counts reconcile against the actual table deltas, and **zero** per-row insert audit rows. A rolled-back mutation of either shape leaves no audit row (see testing-strategy.md).

## Bulk-Import Audit: Batch-Level for Inserts

Audit granularity is keyed on **which write path a mutation takes** — never on row count or a volume threshold. The workload is bimodal: manual mutations arrive a few rows at a time, but data-model.md forecasts CGM backfills of millions of rows in a single import batch. Per-row insert audit at that scale would roughly triple the batch's write volume and permanently store a JSON duplicate of every reading in a table that is never pruned. So bulk-import *inserts* are audited at batch level, while every mutation of *existing* data keeps per-row image audit — there, the old/new images are the whole point.

| Write path | Operation | Audit granularity |
|---|---|---|
| `/v1/import` — rows inserted | `import` | One `audit_log` row per (batch × table touched) |
| `/v1/import` — existing rows changed under `upsert` | `correct` | Per-row, with images (below) |
| Regular POST endpoints (manual entry) | `insert` | Per-row, unchanged |
| Corrections, metadata repairs, deletes | `correct` / `update` / `delete` | Per-row with images, unchanged |

**The batch audit row** — one per (import batch × table touched), so `table_name` stays meaningful when a lab-panel import writes both `lab_draws` and `lab_results`:

- `operation = 'import'`, `row_id = NULL`
- `new_values` holds a summary JSON: `{rows_inserted, rows_corrected, rows_skipped, rows_unchanged, conflict_policy, source, adapter_id, adapter_version}`
- `import_batch_id`, `actor`, `job_id`, `occurred_at_utc` populated as normal
- Written inside the batch's single atomic transaction (ADR-0004): rollback leaves no audit row; dry-run writes nothing

"Where did this row come from" is answered by the `import_batch_id` column every imported row already carries, joined to the batch audit row and the import-batch provenance record.

**Conflict-policy semantics** (policies per ADR-0004):

- `upsert`, incoming row identical to the existing row on the compared columns: **no-op** — no mutation, no audit row; counted as `rows_unchanged` in the summary. This identity short-circuit is what keeps whole-file re-imports cheap.
- `upsert`, incoming row genuinely differs: the **supersession path** — insert the corrected row, set the existing row's `superseded_by`, write one per-row `correct` audit row with both images and `import_batch_id`, `reason` auto-populated (e.g. `upsert re-import, batch <id>`); Core emits `data.corrected`. Treating overwrite as an in-place `update` instead would make bulk import the only path in the system where a clinical value can vanish from a data table — exactly the exception this ADR forbids. Only rows that actually changed at the source supersede, so chain volume stays proportional to real revisions, not to re-import size.
- `skip`: no mutation, no per-row audit; counted in the summary.
- `reject`: the batch fails; nothing is written.

**What batch-level insert audit preserves and loses.** "What changed, when, by what" for inserts is answered by the batch audit row (when, who, how many) plus the data rows themselves (what — each carries `import_batch_id`). Every *subsequent* mutation of an imported row still captures full images: corrections supersede, deletes record `old_values`. No value can ever be lost — a row's content is always in the live table, a supersession chain, or a delete audit image. What is given up: `audit_log` alone is no longer a self-contained replica of inserted content; reconstructing exactly what a batch inserted, for rows never since mutated, requires the data tables.

*Seam for ADR-0021 (deferred):* if CGM rows are later made supersession-exempt (re-import replaces rather than supersedes), that exemption is declared per table — exactly like designated-metadata columns — and changes only the differing-row `upsert` rule above for that table. The batch-audit shape is unaffected.

## Correction Model: `superseded_by` Supersession

Every data table carries a nullable self-referencing foreign key:

```sql
superseded_by INTEGER NULL REFERENCES <same_table>(id)
```

- **A value correction never mutates the original row.** It inserts the corrected row, sets the original's `superseded_by` to the new row's ID, and writes one `audit_log` row with `operation = 'correct'` capturing both images. All in one transaction, from which the Core Service emits `data.corrected` (a reserved event only Core may emit — ADR-0026).
- **Current state** is `WHERE superseded_by IS NULL`. Migrations define a `<table>_current` view alongside each data table so readers (and ADR-0021 aggregates) consume the filter by name instead of re-stating it. A partial index (`... WHERE superseded_by IS NULL`) keeps current-state queries flat as chains accumulate.
- **Corrections of corrections chain**: the middle row is both a superseder and superseded. "What did we believe on date X" walks the chain backward using the audit rows' `occurred_at_utc` — a query feature, not a storage model.
- **Superseded rows are never deleted.** They are the history the pattern exists to preserve.

**Carve-out — designated metadata corrections.** The timezone correction workflow ([design-rationale.md](../design-rationale.md)) intentionally updates `*_local_tz`, recomputes `*_utc`, and clears `*_tz_inferred` **in place**: the clinical observation is unchanged, only its recorded time context is repaired, and `*_local_recorded` remains immutable as the record of what the source said. Such updates use `operation = 'update'` and are fully audited (old/new images), but do not create supersession rows. The rule: **value corrections supersede; designated metadata repairs update.** Which columns qualify as designated metadata is declared per table in the schema documentation — the default for any column is supersession.

**Not a third category — derived denormalizations are computed, not stored.** A value derived from other rows — e.g. an intervention's "current dose", which is just the latest `intervention_dose_history` entry ([data-model.md](../data-model.md)) — is neither a value correction nor a metadata repair. Rather than admit a third mutation category to exempt such a cache from audit and supersession, the platform does not store it at all: derived values are exposed as views or repository-layer queries over their source rows, so there is no in-place update to categorize. The two-category rule stands intact.

## Delete Semantics: Hard Delete + Mandatory Audit

With supersession covering corrections, true deletion is rare — the canonical case is an erroneous duplicate import. The decision is **hard delete**, not a `deleted_at` soft-delete flag that every query in the system would have to filter around forever:

- The row is removed; the `audit_log` row (`operation = 'delete'`) preserves the full row image in `old_values`, so what was deleted is always answerable and manual restoration is possible
- The Core Service emits `data.deleted` (reserved, Core-emitted) so aggregates and subscribers react
- **Deletion is treated as a flagged, deliberate act in clients**: the confirmation identifies exactly what will be deleted and **offers a pre-delete backup through the mechanism the client can actually reach** — a delete implies the Core Service is up, so that mechanism is the `backup.database` job ([ADR-0038](0038-backup-execution-and-verification.md)), never `healthspan db backup`, which refuses to run against a live service. The CLI's built-in delete submits the job directly (`cli-admin` carries the job's declared `admin` scope, [ADR-0026](0026-named-scoped-tokens.md)); the `gui` token deliberately lacks `admin`, so the GUI's offer is honest rather than fake — its confirmation shows the age of the last successful verified backup (read from job history via its `jobs` scope) and, when that is stale or absent, directs the user to an admin surface (the CLI). The offer is informational, not blocking. The platform's recovery story is backups, so the delete flow is where a backup is surfaced (architecture review 2026-07-07, item 1.B)
- Rows that are part of a supersession chain (either end) are not deletable — correcting history and erasing it are different operations, and the latter does not exist for chained rows

## CQRS-Lite: Degree of Read/Write Separation

The full command/query split is rejected along with event sourcing. What remains, stated as the recorded decision:

- **Writes** go exclusively through the validated Core REST API path (ADR-0004): validation → mutation + audit row in one transaction → Core-emitted `data.*` event
- **Reads** query current-state tables and views directly, or ADR-0021's aggregate tables where they exist
- **Aggregates are caches, never authoritative**: derived entirely from raw current-state data, rebuildable from scratch at any time, invalidated/recomputed in response to `data.imported`, `data.corrected`, and `data.deleted` events — this resolves ADR-0021's open question on correction invalidation

## Consequences for Other Documents

- **open-questions.md**: longitudinal correction, audit trail, event sourcing, and CQRS entries move to Resolved
- **data-model.md**: cross-cutting concerns updated to cite this ADR as the decision, including the batch-level audit rule for bulk-import inserts
- **ADR-0021** (Proposed — stub): invalidation open question answered — aggregates are rebuildable read models invalidated by `data.*` events
- **ADR-0011** (Proposed): no content change — `data.imported` / `data.corrected` / `data.deleted` are already cataloged as reserved Core-emitted events; navigation link added
- **testing-strategy.md**: audit-trail coverage targets added (mutation matrix with the two-shape per-row/batch contract, rollback, immutability triggers, supersession chains)

## Pros and Cons of the Options

### Full event sourcing + CQRS
- Pro: audit, correction, and time-travel are inherent, not designed-in
- Pro: replay can rebuild state from the stream
- Con: every reader queries projections that must be kept consistent — permanent machinery for a single-writer analytical store
- Con: schema evolution becomes event versioning; the contributor bar for touching the data layer rises system-wide
- Con: the one unique capability (replay-as-recovery) duplicates what encrypted backups already provide

### Mutable rows + independently-written audit log
- Pro: simplest to start
- Con: the audit trail is only as complete as the most forgetful code path — drift is a *when*, not an *if*; rejected outright for an integrity record

### In-transaction audit + supersession + CQRS-lite (chosen)
- Pro: audit transactionally cannot drift; corrections preserve history; time-travel works; plain SQL everywhere
- Pro: provenance (actor, batch, job) captured naturally at the application layer
- Con: `WHERE superseded_by IS NULL` discipline (mitigated by `*_current` views); unbounded audit growth (negligible at this volume); no stream replay (backups own recovery)

## Links
- Resolves: [open-questions.md](../open-questions.md) — longitudinal data correction, audit trail, event sourcing, CQRS
- Resolves: [architecture review 2026-06-10](../architecture-review-2026-06-10.md), item 3.A
- Resolves: [architecture review 2026-07-06](../architecture-review-2026-07-06.md), item 3.A — bulk-import audit granularity (batch-level for inserts)
- Resolves: [architecture review 2026-07-07](../architecture-review-2026-07-07.md), item 1.B — the pre-delete backup offer names a mechanism each client can reach
- Related: [ADR-0038](0038-backup-execution-and-verification.md) — the `backup.database` job is the in-service pre-delete backup; `healthspan db backup` refuses to run against a live service
- Related: [ADR-0021](0021-time-series-aggregation.md) — aggregates as rebuildable read models; invalidation via `data.*` events
- Related: [ADR-0026](0026-named-scoped-tokens.md) — `actor` from token identity; `auth_audit` as the distinct security record
- Related: [ADR-0011](0011-event-bus.md) — reserved `data.*` events emitted by Core on validated mutations
- Related: [ADR-0012](0012-job-abstraction.md) — `job_id` provenance on job-performed mutations
- Related: [ADR-0004](0004-data-ingestion-strategy.md) — `import_batch_id` provenance; the single validated write path
- Related: [ADR-0052](0052-bulk-import-identity-and-conflict-resolution.md) — how the batch-level/per-row audit granularity and supersession map onto `lab_draws`/`lab_results` in the Phase 2 import path
- Related: [ADR-0009](0009-database-migration.md) — `audit_log` and its immutability triggers land in migration 0001
- Related: [ADR-0019](0019-multi-device-sync.md) / [ADR-0013](0013-encryption-at-rest.md) — encrypted backups own the recovery story that replay would otherwise provide
- Related: [design-rationale.md](../design-rationale.md) — timezone correction workflow (the designated in-place metadata repair)
