# ADR-0057: Reference-Data Catalog, Alias Resolver, and Catalog-Import Implementation Decisions (Phase 3 WI-2)

## Status
Accepted

## Context and Problem Statement

[ADR-0055](0055-biomarker-category-taxonomy.md) (D1) and [ADR-0054](0054-biomarker-name-alias-fallback.md) (D2) fixed the Phase 3 decision gates — first-class categories with a reserved default, and a `biomarker_aliases` resolver — but both deliberately left their DDL, engine mechanics, and startup wiring for "the implementing WI-2 PR." Implementing them (migration 0004; the generalized import engine of [ADR-0052](0052-bulk-import-identity-and-conflict-resolution.md); the read surface of [ADR-0053](0053-read-endpoint-surface-and-pagination.md)) forced a set of concrete decisions the two gate ADRs did not — and, per their design, should not — settle themselves.

Following the [ADR-0047](0047-crypto-surface-implementation-decisions.md)/[ADR-0049](0049-core-service-skeleton-implementation-decisions.md)/[ADR-0056](0056-units-module-api-and-molar-context.md) precedent, the accumulated WI-2 implementation decisions land as one batched Proposed ADR in the same PR as the implementing change.

## Decision Drivers

- SQLite cannot `ALTER` a column's type or its `REFERENCES` target safely under every pragma setting — the `biomarkers` rebuild must get the FK-rewrite hazard right, and that hazard is not obvious from the SQL alone
- `foreign_key_check` proves referential integrity, not the presence of a specific sentinel row — the reserved-row invariant (ADR-0055 §2) needs its own enforcement point
- The import engine (ADR-0052) was built for content tables with `superseded_by`/`import_batch_id`; catalog tables have neither, and the engine must not silently assume they do
- Wrong-biomarker resolution is a confidently-wrong-data defect (ADR-0054 §Decision Drivers) — the resolver and its write-time uniqueness guards must be exact-match and fail-loud, with no path for drift between resolution and alias-derivation normalization
- The import engine's batch model (ADR-0052: payload `id` is a batch-local handle) has a real limit when the referenced rows are catalog rows rather than intra-batch siblings — that limit needed a place to live
- The seed data is generic reference data (CLAUDE.md, personal-data containment) and low-stakes by ADR-0055 §6's own admission, but the judgment calls made should still be traceable

## Decision Outcome

### 1. Migration 0004 shape: categories catalog, reserved row, delete-guard trigger, biomarkers rebuild

[`0004_categories_and_aliases.sql`](../../src/healthspan/migrations/0004_categories_and_aliases.sql) creates `categories` (id/name/description, `UNIQUE(name)`), seeds the reserved `not_assigned` row at id 0 and the 19 system-axis categories (ADR-0055 §6), then rebuilds `biomarkers` to replace the free-text `category` column with `category_id INTEGER NOT NULL DEFAULT 0 REFERENCES categories(id)`, mapping any pre-existing free-text value to its category id (defaulting to 0) via a subselect — written correctly for the general non-empty case even though the table is empty on every real upgrade path.

The reserved row is guarded by a **`BEFORE DELETE` SQL trigger** (`categories_reserved_no_delete`, `WHEN OLD.id = 0`) rather than an application-only check. This was chosen over app-only enforcement because it is always-on regardless of write path (a future direct-DB tool, a bug in application validation, or a manual `sqlite3` session are all covered) and it mirrors the existing precedent of the 0001 `audit_log` append-only triggers — the project's established pattern for structurally preventing a specific mutation rather than merely discouraging it in code. App-level validation is not layered on top for this specific guard; the trigger is the single enforcement point and its `RAISE(ABORT, ...)` message names the ADR.

### 2. `PRAGMA legacy_alter_table = ON` bracket around the `biomarkers` RENAME

The rebuild's `ALTER TABLE biomarkers RENAME TO biomarkers_old` step is bracketed by `PRAGMA legacy_alter_table = ON` / `PRAGMA legacy_alter_table = OFF`, restored to `OFF` immediately after the rename. This is a non-obvious correctness fact any future migration author extending this pattern must know:

- With SQLite's own default (`legacy_alter_table = OFF`), `ALTER TABLE ... RENAME TO` rewrites the `REFERENCES` clause of every *other already-existing* table naming `biomarkers` in its schema text — here, `lab_results` and `framework_ranges` (migration 0001) — to point at `biomarkers_old` instead, silently, with no error at rename time.
- The migration then `DROP TABLE biomarkers_old`, which would leave `lab_results`/`framework_ranges` referencing a table name that no longer exists — silent FK corruption that `foreign_key_check` would not catch until (or unless) a row referencing the stale name is checked, because SQLite resolves the FK target by name at check time and would report the child row as violating a *reference to a nonexistent table*, not as an obviously-wrong error.
- `legacy_alter_table = ON` restores SQLite's pre-3.25 rename behavior, which does not rewrite other tables' `REFERENCES` clauses, so `lab_results`/`framework_ranges` keep naming `biomarkers` (the identifier survives the rename/recreate because the new table is created under the same name).
- The pragma is turned back `OFF` immediately after the rename statement (not left on for the rest of the migration or the connection) so a later `ALTER` in this or any future migration keeps SQLite's safer modern default unless it explicitly opts back in.

For the identical reason, **`biomarker_aliases` is created after the `biomarkers` rebuild**, not before it — its `REFERENCES biomarkers(id)` must resolve against the final table, not the transient `biomarkers_old`.

### 3. Reserved-row presence assertion at Core Service startup

`db.reserved_category_present(conn)` (`SELECT 1 FROM categories WHERE id = 0`) is asserted in `service.verify_schema`, **after** the existing `schema_version` match. Ordering is required, not incidental: on a pre-migration-4 database the `categories` table does not exist at all, so the reserved-row check can only run once the schema-version check has already confirmed migration 4 has applied. A missing reserved row raises `ServiceStartupError` with a message naming the ADR and the remediation (restore from backup or re-seed), so the failure surfaces loudly at process start rather than as a confusing far-from-cause `foreign_key_check` failure the first time an import defaults a biomarker to `category_id = 0`. This closes the gap ADR-0055 §2 identified: `foreign_key_check` proves every `category_id` resolves to *some* row, never that id 0 specifically survives.

### 4. Import-engine generalization: `has_supersession` / `has_provenance`

`ImportableTable` gains two booleans, both defaulting `True` (so every existing content-table registration is unaffected): `has_supersession` and `has_provenance`. `has_provenance=False` omits `import_batch_id` from the row `INSERT`; `has_supersession=False` omits `superseded_by` from both the `INSERT` and the `_find_id` lookup (no `AND superseded_by IS NULL` clause), and — the load-bearing part — routes `_reconcile`'s genuine-difference case to `_update_in_place` unconditionally. `_supersede` asserts `spec.has_supersession` and never executes against a table lacking the column; there is no code path by which a catalog table's row could be superseded into a state with nothing to receive it. All four catalog tables (`categories`, `labs`, `biomarkers`, `biomarker_aliases`) register with both flags `False`. Batch-level `import` audit rows remain one per (batch, table) regardless of these flags — only the per-row reconciliation path changes.

### 5. The `biomarker_name` resolver: one place, exact-match, pre-conflict-detection

`resolve_biomarker_name(conn, name)` is the single resolution point (ADR-0054 §3). It normalizes `name` via `normalize_name` and looks it up against the **union** namespace of normalized `biomarkers.canonical_name` and stored `biomarker_aliases.alias_normalized`, both mapping to `biomarker_id`. Zero or more than one match is a collected, fail-loud validation error naming the unresolved string — never a fuzzy fallback, never a "best guess." Resolution runs inside `_resolve_payload`, before ADR-0052 conflict detection, so natural-key matching and conflict-policy handling downstream only ever see a resolved `biomarker_id`; a `lab_results` row supplying `biomarker_name` is indistinguishable from one supplying `biomarker_id` by the time it reaches `_validate_identity_table`.

`normalize_name` (NFKC → casefold → strip → collapse internal whitespace runs to one space) is the single function used both by the resolver and by the alias-derive step (decision 7 below), so resolution and alias storage cannot drift apart on what counts as "the same name."

### 6. Cross-table normalized-uniqueness write validation

`_validate_alias_canonical_uniqueness` enforces, in the validation pass (before any write), what a plain `UNIQUE` constraint cannot express across two tables:

- an alias whose `alias_normalized` collides with any biomarker's normalized `canonical_name` is rejected;
- a biomarker whose normalized `canonical_name` collides with an existing alias's `alias_normalized`, or with another biomarker's normalized `canonical_name`, is rejected;
- an alias exactly equal (after normalization) to its own biomarker's canonical spelling is rejected as redundant rather than stored.

These checks run over **both already-stored rows and other rows in the same import batch** — a batch that introduces a colliding biomarker and alias pair in the same call is caught, not just a batch colliding with prior state. Alias-to-alias collision is handled separately, by the schema-level `UNIQUE` constraint on `alias_normalized` (a natural key, not this function's concern).

Because this cross-table check has no schema constraint backing it (unlike the single-table `UNIQUE`s, which the database enforces at INSERT), a read-then-write check is only race-free while a write lock is held. `run_import` therefore takes `BEGIN IMMEDIATE` **before** resolution and validation, not merely before apply: two concurrent imports serialize on the lock, so the second validates against the first's committed state and a canonical/alias collision cannot slip through the TOCTOU window an outside-the-lock check would leave open. A validation failure rolls the transaction back (releasing the lock) and writes nothing. The lock-before-validation ordering is regression-tested (a locked second connection makes even a would-be-invalid batch raise `database is locked` before validation runs); the serialization it depends on is SQLite's single-writer `BEGIN IMMEDIATE` guarantee, not bespoke logic, so a full two-writer interleaving harness is deliberately not added.

### 7. Server-derived `alias_normalized` / `created_utc`

`biomarker_aliases` import rows accept `biomarker_id`, `alias`, and optional `source` from the client. `alias_normalized` (via `normalize_name`) and `created_utc` (via `utc_now_iso`) are always derived server-side; any client-supplied values for these two columns are overwritten, never trusted. This matches the existing posture toward `import_batch_id` and `superseded_by` on content tables — provenance and derived-identity columns are never client-authoritative.

`created_utc` is a **`server_owned`** column (a new `ImportableTable` field): stamped once at insert, but excluded from the `compared` set and left untouched by an in-place reconcile update. Without this it would be a compared non-key column re-stamped on every import, so an otherwise-identical re-import (the WI-4 CLI's confirm-and-record flow re-recording a known alias) would be classified as a conflict/correction rather than `unchanged` — churning the timestamp and, under `reject`, failing loud spuriously. A blank (whitespace-only) `alias`, whose normalized form is empty, is rejected in validation rather than stored with an empty `alias_normalized`.

### 8. A single `_resolve_payload` pass, applied once, before both validation and apply

Name resolution (decision 5) and alias derivation (decision 7) both happen inside one `_resolve_payload` function, called once per import, producing a materialized payload that both the validation pass and the apply pass consume unchanged. Neither pass re-derives `alias_normalized`, `created_utc`, or a resolved `biomarker_id` independently — validation and apply are guaranteed to see identical rows, closing off a whole class of "passed validation on one derivation, failed differently on another" bugs.

### 9. Same-batch constraint: catalog references must already be stored

Name/alias resolution (decision 5) and cross-table uniqueness (decision 6) only ever consult already-**stored** `categories`/`biomarkers`/`biomarker_aliases` rows — never a row created earlier in the same import batch. A `lab_results` row naming a `biomarker_name` that was only just introduced by a `biomarkers` row earlier in the same call fails resolution; a biomarker must land in a prior, already-committed import before it can be referenced by name in a later one. This is a genuine architectural limit, not an oversight: it is documented in [api-reference.md](../api-reference.md) as the "same-batch visibility rule," and it is consistent with how `lab_results.biomarker_id` already treats `biomarkers` as a plain pre-existing foreign key (ADR-0052) rather than a batch-local handle — only `lab_draws`/`lab_results` intra-batch wiring (via the payload `id` handle) gets same-batch visibility; catalog tables never do.

### 10. `?category=` case-insensitive name resolution; biomarker rows carry the category name

`list_biomarkers(category=...)` resolves the filter case-insensitively to a `category_id` (`WHERE b.category_id = (SELECT id FROM categories WHERE name = ? COLLATE NOCASE)`), consistent with ADR-0055 §1's binding lookup semantics. Biomarker read rows are serialized with `category` set to the category **name** (preserving the Phase 2 response shape's `category` field, which previously carried free text) alongside the new `category_id`, via a naive `JOIN categories` — correct without a `LEFT JOIN` because the reserved-default FK guarantees every biomarker has a category row (ADR-0055 §2).

### 11. Seed scope: categories + starter biomarker catalog + labs now; frameworks/ranges deferred to WI-3

Migration 0004 seeds the 19 categories, four labs (`Quest`, `LabCorp`, `Function Health (Quest)`, `Function Health (LabCorp)`), and a curated starter biomarker catalog (~64 biomarkers) spanning lipoproteins, metabolic, liver, kidney, electrolytes, thyroid, hematology, inflammation, hormones, nutrients, pancreas, and screening. `range_frameworks`/`framework_ranges` seeding is deliberately **not** included — deferred to WI-3 — even though their read endpoints (ADR-0053-style list/get) ship in this PR and answer empty pages until then.

Notable seeding judgment calls, each following an explicit ADR-0055 §6 steer or its system-vs-theme rule (§3):

- `TPO Antibodies → autoimmunity` (an autoimmune marker, not a thyroid-function test, despite the lay association with thyroid panels).
- `Uric Acid → metabolic`.
- `Homocysteine → inflammation` (vascular/inflammatory risk use, per ADR-0055 §6's explicit derivation) rather than metabolic.
- `Ferritin → nutrients` (per ADR-0055 §6) rather than inflammation, despite Ferritin's common use as an acute-phase reactant.
- `PSA → screening` (an orphan-rescue category per ADR-0055 §3; future `oncology`/`male_health` tags are deferred, ADR-0055 §4).

`loinc_code` is left `NULL` on every seeded biomarker rather than guessed — a wrong LOINC is worse than a missing one (fail-safe; ADR-0032 owns the electronic-feed LOINC-assignment lane, not this migration).

### Positive Consequences

- The `biomarkers` rebuild's FK-rewrite hazard is now a documented fact in both the migration comments and this ADR, not a landmine for the next migration author
- The reserved-row invariant fails loud at the earliest possible point (process start) rather than mid-import
- Catalog tables reuse the exact same validated-write-path/audit machinery as content tables, with no duplicated import logic
- One resolver and one normalization function make wrong-biomarker resolution structurally hard to reach, matching the fail-loud posture ADR-0054/ADR-0005 established
- The same-batch constraint is written down as policy, not left to be rediscovered as a confusing 422 the first time someone tries to import a biomarker and its lab result in one call

### Negative Consequences / Tradeoffs

- The `legacy_alter_table` bracket is SQLite-version-sensitive trivia a future contributor must re-learn from the comment/ADR rather than from the DDL reading naturally — accepted; the alternative (hand-writing the 12-step redefinition for every FK-bearing dependent table) is far more error-prone
- The same-batch constraint (decision 9) means catalog-then-content imports need at least two calls when introducing a brand-new biomarker and its first result together — accepted as consistent with the existing `lab_results.biomarker_id` pre-existing-FK model, but it is a real usability cost the CLI (WI-4) must design around
- A single primary category per biomarker still forces "best home" choices (decision 11) for double-booking markers, inherited from ADR-0055 and not reopened here
- Two write paths (aliases, biomarkers) now carry application-enforced normalized-uniqueness validation that SQLite's schema cannot express — it needs, and has, dedicated test coverage, but it is a durable maintenance surface
- Holding `BEGIN IMMEDIATE` across resolution and validation (not just apply) lengthens the write-lock hold for a large batch by the validation-read time — accepted under the single-writer model ([ADR-0037](0037-core-service-concurrency-and-driver.md)); it is the price of making the application-level cross-table check race-free

## Consequences for Other Documents

- **[data-model.md](../data-model.md)**: Migration 0004 section (categories, reserved row, delete-guard, `biomarkers` rebuild, `biomarker_aliases`) — landed this PR
- **[api-reference.md](../api-reference.md)**: reference-data endpoints, import contract for the four catalog tables, `biomarker_name` resolution, same-batch visibility rule, case-insensitive `?category=` — landed this PR
- **[open-questions.md](../open-questions.md)**: no new deferral; `range_frameworks`/`framework_ranges` seeding remains the existing WI-3 trigger already recorded under ADR-0055

## Links

- Extended by: [ADR-0058](0058-range-comparison-implementation-decisions.md) §6 — adds `range_frameworks`/`framework_ranges` to this ADR's catalog-import registry, and generalizes it with a nullable natural-key column (`ImportableTable.nullable_key`) for the ADR-0005 dateless default; §9's same-batch constraint applies to both unchanged
- Implements: [ADR-0054](0054-biomarker-name-alias-fallback.md) — the alias table, resolver, and normalization rule
- Implements: [ADR-0055](0055-biomarker-category-taxonomy.md) — the categories catalog, reserved default, and delete-guard
- Extends: [ADR-0052](0052-bulk-import-identity-and-conflict-resolution.md) — the import engine this generalizes (`has_supersession`/`has_provenance`), and the natural-key/conflict machinery the resolver feeds into
- Extends: [ADR-0053](0053-read-endpoint-surface-and-pagination.md) — adds the `categories`/`range-frameworks`/`framework-ranges` resources to its read surface, under the same list/get + pagination pattern
- Related: [ADR-0030](0030-biomarker-identity.md) — the biomarker identity and value model this catalog work concretizes without reversing
- Related: [ADR-0031](0031-units-and-ucum.md) — the UCUM unit strings validated in the seeded biomarker catalog
- Related: [ADR-0027](0027-audit-trail-and-corrections.md) — catalog writes/deletes are audited insert/update/delete, never supersession
- Related: [ADR-0035](0035-migration-execution-semantics.md) — the migration runner discipline (`foreign_keys=OFF`/`BEGIN IMMEDIATE`/`foreign_key_check`/`COMMIT`) this migration runs inside
- Related: [ADR-0037](0037-core-service-concurrency-and-driver.md) — the single-connection-open shape `verify_schema` preserves when adding the reserved-row assertion
