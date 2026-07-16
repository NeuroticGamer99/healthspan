# Open Questions

Architectural and technical decisions that need resolution before or during implementation. Personal action items (e.g. data collection tasks) are tracked separately in `specs/personal/`.

---

## Schema — Must Resolve Before Data Entry

~~**Timezone handling**~~ → Resolved — see Resolved section.

~~**Longitudinal data correction**~~ → Resolved — see Resolved section and [ADR-0027](adr/0027-audit-trail-and-corrections.md).

~~**Audit trail**~~ → Resolved — see Resolved section and [ADR-0027](adr/0027-audit-trail-and-corrections.md).

~~**Event sourcing as the audit trail implementation pattern**~~ → Resolved (rejected) — see Resolved section and [ADR-0027](adr/0027-audit-trail-and-corrections.md).

~~**CQRS (Command Query Responsibility Segregation)**~~ → Resolved (CQRS-lite) — see Resolved section and [ADR-0027](adr/0027-audit-trail-and-corrections.md).

---

## Architecture — Undecided ADRs

One ADR has a TBD decision that should be resolved as design progresses:

~~**Database backend ([ADR-0003](adr/0003-database-backend.md))**~~ → Resolved (SQLite-only for v1) — see Resolved section and [ADR-0003](adr/0003-database-backend.md).

~~**AI client interface ([ADR-0002](adr/0002-ai-provider-interface.md))**~~ → Resolved (MCP-based pluggability) — see Resolved section and [ADR-0002](adr/0002-ai-provider-interface.md).

~~**Reference range frameworks ([ADR-0005](adr/0005-reference-range-frameworks.md))**~~ → Resolved (option 2, with a mandatory UCUM unit and unit-normalized comparison) — see Resolved section and [ADR-0005](adr/0005-reference-range-frameworks.md).

---

## Schema

~~**Biomarker alias table**~~ → Resolved (`biomarker_aliases` table, server-side exact-match resolution) — see Resolved section and [ADR-0054](adr/0054-biomarker-name-alias-fallback.md).

~~**Intervention dose history**~~ → Resolved — see Resolved section.

~~**Biomarker category taxonomy**~~ → Resolved (first-class `categories` table + `category_id` FK, reserved `not_assigned` default, system-axis seed vocabulary) — see Resolved section and [ADR-0055](adr/0055-biomarker-category-taxonomy.md).

**Biomarker cross-cutting tags ([ADR-0055](adr/0055-biomarker-category-taxonomy.md))**
ADR-0055 ships a single *primary* category per biomarker (physiological-system axis). The owner also wants cross-cutting classification — the *theme* axis the two source lists kept reaching for (Biological Age-adjacent, Stress & Aging, Male Health, Inflammation, supplementing, cardiac risk) and containment facets like oncology ⊂ screening. That is deferred as a **purely additive** change: a `biomarker_categories`/`tags` many-to-many table *alongside* the untouched `category_id`, so the primary category ("which section does this render under") is unaffected and tags answer "what else is it about." Concrete first tag vocabulary: `oncology`, `male_health`, `stress_and_aging`, `supplementing`, `cardiac_risk`, `inflammation`. Trigger: Phase 4 (the MCP surface makes cross-cut queries valuable), or the first real cross-cut need before then.

**Biomarker molar-mass persistence ([ADR-0056](adr/0056-units-module-api-and-molar-context.md), [ADR-0031](adr/0031-units-and-ucum.md))**
Phase 3 WI-1's units module takes molar mass as an explicit `convert(..., molar_mass=)` argument (grams per mole); a molar conversion (mg/dL ↔ mmol/L) attempted without it fails loud ([ADR-0056](adr/0056-units-module-api-and-molar-context.md) §3). Nothing yet *stores* molar mass — the `biomarkers` catalog ([ADR-0030](adr/0030-biomarker-identity.md)) has no such column — so the comparison path (WI-3) and the CLI (WI-4) currently have no reference source to pass that argument from. Where molar mass lives (a `biomarkers.molar_mass` column alongside `canonical_unit`, or a separate lookup) and which biomarkers carry one is deliberately deferred. Trigger: WI-2's reference-data/schema work (the migration that builds the catalog is the natural home), or WI-3's first molar comparison, whichever comes first.

**Additive `ALTER TABLE` migrations must recreate the affected `*_current` view ([data-model.md](data-model.md), [ADR-0027](adr/0027-audit-trail-and-corrections.md))**
The migration-0001 `*_current` views are `SELECT * FROM <table> WHERE superseded_by IS NULL`, and SQLite binds `SELECT *` to the base table's columns at `CREATE VIEW` time. A later migration that does `ALTER TABLE … ADD COLUMN` will therefore not surface the new column through the current-state view until the view is dropped and recreated. Convention to adopt when the first additive migration lands: any migration adding a column to a content table also `DROP VIEW`/`CREATE VIEW`s that table's `*_current` view (and a schema-integrity test asserts the view column set matches the base table). Trigger: the first `ALTER TABLE ADD COLUMN` migration (the header of `0001_initial_schema.sql` anticipates these for `import_batches`/`jobs`). Surfaced by the 2026-07-11 end-of-Phase-1 holistic review.

~~**Clinical documents — full-text search strategy**~~ → Resolved (FTS5 external-content over `body`) — see Resolved section and [ADR-0041](adr/0041-clinical-document-fts.md).

**Subjective observation vocabulary ([data-model.md](data-model.md), Subjective Observations)**
The journal type starts freeform — `body` text only. Should a structured vocabulary (tags; 1–10 scales for energy, mood, pain, sleep quality) be added, and when? Structured fields make longitudinal plots of subjective state possible, but structure that arrives before the logging habit does tends to kill the habit. Ties to the Levels export question (Data Ingestion below): if the "how I'm feeling" tracker is in the activity-log export, its note-type vocabulary should map onto whatever is chosen here. Defer until real entries accumulate and show what structure they actually carry.

**Derived data points — deferred schema ([ADR-0044](adr/0044-derived-data-points.md))**
ADR-0044 fixes the principle (derived values are a distinct content class, never stored in source-data tables; interim home is the `result_data` attachment on analyses) and deliberately defers the derived-series table. Sub-questions recorded for when it is designed: (1) internally computed (formula stored, rebuildable) vs. externally computed (opaque snapshot) — the two subtypes behave differently under the [ADR-0027](adr/0027-audit-trail-and-corrections.md) correction model; (2) does a derived series get a `biomarker_id` ([ADR-0030](adr/0030-biomarker-identity.md))? (3) may derived series be range-compared ([ADR-0005](adr/0005-reference-range-frameworks.md)) — HOMA-IR has published ranges, so a blanket "never" is wrong; (4) staleness marking when a source row feeding a derived point is superseded. Design trigger: the analyses table exists in a running system and accumulated attachments show which subtype dominates.

**Accumulation evidence (2026-07-14, Phase 3 D1 discussion).** Two concrete *externally-computed* instances from the owner's Function Health data, recorded to inform sub-questions (1) and (3): **Biological Age** (opaque composite, unit years) and the IGF-1 **Z-Score** (opaque, unit SD, **published reference range −2 to +2**). Both are values the owner cannot recompute but wants imported for longitudinal analysis. They sharpen the deferral in two ways: (1) the externally-computed subtype behaves like *received source data* under the correction model — correcting the owner's stored IGF-1 does not invalidate a Z-Score Function computed from its own measurement, so the "never in source tables" hazard (rebuildable staleness) does not apply to it the way it does to internally-computed HOMA-IR; (3) the Z-Score's −2..+2 range is a live counterexample to any blanket "derived values are never range-compared." These do **not** resolve the deferral (still Phase-5-triggered) and are explicitly **not** Phase 3 biomarkers — importing them is ADR-0044-gated, not part of the [ADR-0055](adr/0055-biomarker-category-taxonomy.md) taxonomy.

---

## Data Entry

**Manual entry efficiency**
When entering a batch of lab results from the same draw, the lab name, draw date, and reference ranges repeat across every row. What tooling or entry pattern avoids this repetition? Options include a draw-level entry template (enter lab + date once, then enter results), a simple import format (CSV with a header row capturing draw metadata), or accepting repetition and relying on copy-paste. Ties directly to the ingestion strategy decision (ADR-0004).

---

## Data Ingestion

**CGM indexing strategy**
What indexes on `cgm_readings` optimize time-range queries at scale (potentially millions of rows)? Composite index on `(timestamp)` is the baseline — are partial indexes or covering indexes worth adding up front?

**Levels export — column schemas and timestamp format**
Four export types confirmed from the Levels export page:
- Glucose data → CSV (async: Levels emails a download link when ready)
- Zone data → JSON (zone scores and glucose response)
- Activity logs → CSV (food, exercise, and notes)
- Nutrition logs → CSV (food with nutritional metadata)

Remaining unknowns that must be resolved before building any Levels import adapter:
1. **Exact column names and timestamp format** for each export file — determines timezone handling (inferred vs explicit) and schema mapping
2. **Whether "how I'm feeling" / subjective tracker is included** in the activity logs export or not exported at all
3. **JSON structure of the Zone data export** — needed to design the `levels.zones` schema

The async email delivery for glucose data makes full automation impractical. Watch folder import is the recommended approach for glucose: user triggers export, receives email link, saves file to configured directory, importer picks it up automatically.

Inspect a real export of each type before designing any import adapter.

**Dexcom Developer API**
Dexcom has an official OAuth-based REST API accessible from a desktop application — no mobile app required. Investigate: (1) What scopes/endpoints are available for historical CGM data? (2) What is the data format and timestamp convention? (3) Are there rate limits relevant to a full historical backfill? (4) Does the API require a Dexcom account separate from the sensor hardware, or is it tied to the existing account? This is the preferred long-term CGM source alongside or instead of Levels glucose export.

**Apple Health XML export**
The iOS Health app exports all HealthKit data as XML — a broad aggregation source covering any HealthKit-connected app (Levels, Dexcom, Fitbit, Apple Watch, etc.) in a single file. Investigate: (1) What is the XML schema and which data types relevant to this platform are included? (2) How large does the export become over years of data? An import adapter here could replace several individual source-specific adapters for iOS users.

**Samsung Health export and API**
Samsung Galaxy devices run Samsung Health alongside Google Health Connect. Relevant for development and testing (project developer uses Samsung S23 Ultra). Investigate: (1) What does the Samsung Health manual CSV export include and in what format? (2) Is the Samsung Health web developer API still active and what data does it expose?

**Google Health Connect**
Android's health data aggregation platform (replacing Google Fit). No bulk desktop export mechanism; live API requires a native Android app. If a native Android companion app is ever built, Health Connect is the single integration point for all Android health app data.

**Fitbit historical data**
Google Takeout (JSON bulk export) vs Fitbit API for pulling historical data. Takeout is simpler for a one-time backfill; API is better for ongoing sync. What does the Takeout JSON structure look like for the metrics we care about (steps, resting HR, sleep, HR zones)?

**Body composition device export formats**
What export options exist for each body composition device (currently InBody 120 and InBody 580 via Enara Health)? CSV, PDF, or API? Determines whether ingestion can be automated or requires manual data entry. Answer will vary per device and per provider.

---

## Operations

~~**Backup cadence and retention defaults ([ADR-0038](adr/0038-backup-execution-and-verification.md))**~~ → Resolved (daily, retain 14) — see Resolved section and [ADR-0038](adr/0038-backup-execution-and-verification.md).

**Recovery Kit OS print pathways ([ADR-0033](adr/0033-plaintext-artifact-disposal.md), [ADR-0047](adr/0047-crypto-surface-implementation-decisions.md))**
WI-2 shipped kit rendering as terminal display plus explicit `--output` file (ADR-0047 §4). The **orphan startup sweep shipped in Phase 2 WI-1** (`recovery_kit.sweep_orphans`, called from Core Service startup; [ADR-0049](adr/0049-core-service-skeleton-implementation-decisions.md)), best-effort (overwrite-then-unlink). **Sweep target narrowed 2026-07-12 (full-codebase review finding):** it originally globbed `*recovery-kit*`, which also matched the deliberate `--output` kit filename — so a user who saved their kit into the data directory would have their only offline key copy silently disposed on the next `service start`. The sweep now targets only the disjoint spool naming `recovery_kit.ORPHAN_SPOOL_GLOB` (`.healthspan-recovery-kit-*.spool`), which a deliberate `.txt` kit can never match. Still deferred: ADR-0033's OS print integration (`lp`/`lpr` streaming, Windows temp-file shell print with verified disposal) — the sweep's only *producer* of orphans, **and it must write its transient file via `recovery_kit.orphan_spool_filename` so the sweep can catch a crash between render and disposal**. The print pathways land with the user-documentation milestone; until then the sweep hook has no producer. Trigger: user-documentation milestone (or the first Windows shell-print pathway).

**Direct-database exclusive-access enforcement ([ADR-0038](adr/0038-backup-execution-and-verification.md), [ADR-0042](adr/0042-process-supervision-and-single-instance-locking.md))**
Phase 2 WI-1 shipped the ADR-0042 single-instance advisory lock (`healthspan/locking.py`) and applied it as `cli_support.exclusive_database_access` to the three sanctioned direct-database commands — `db migrate`, `db backup`, `db restore` — which now acquire `<database-path>.lock` fail-fast and refuse while the Core Service (or another instance) holds it ([ADR-0049](adr/0049-core-service-skeleton-implementation-decisions.md) §6). `db restore` holds it for its whole duration, per ADR-0042. The `keys` rekeying commands (`change-passphrase`/`rotate-secret-key`/`convert-mode`) joined the guard in **WI-2b** (the deferred item (1)): each acquires the lock fail-fast before its first prompt and holds it through the rekey. **Still open:** the in-service scheduled `backup.database` job (ADR-0038's scheduled producer, of which the WI-4 CLI is the offline counterpart) — deferred until the job/scheduler surface exists.

**Keyring credential entries are machine-global — multi-database operation ([ADR-0026](adr/0026-named-scoped-tokens.md), [ADR-0051](adr/0051-auth-lifecycle-and-rate-limiting-implementation-decisions.md))**
ADR-0026 fixes the keyring convention as service `healthspan`, usernames `token:<name>` and `mcp-client-secret` — one namespace per machine. The WI-2b bootstrap therefore overwrites those entries whenever it mints into *any* empty `tokens` table: starting the service against a second database (a test database, a fresh `--config`) silently replaces the first database's stored plaintexts, whose hashes remain only in the first database — the CLI then presents the wrong credential and gets uniform 401s. Single-database operation is the design center and unaffected. Resolving multi-database support would need per-database namespacing of the keyring entries, which changes an Accepted ADR-0026 convention → extension ADR. Trigger: any decision to support more than one live database per machine (or the first user report of the clobber).

**Non-loopback binding hardening ([ADR-0049](adr/0049-core-service-skeleton-implementation-decisions.md), [security.md](security.md) Network Security)**
Phase 2 WI-1 added the `[service] host` config key (default `127.0.0.1`, loopback-only). The parser *accepts* a non-loopback bind (`0.0.0.0` or a specific interface), but security.md's Network Security controls that a LAN bind requires — Host-header validation and CORS as DNS-rebinding defenses, and HTTPS for any non-loopback address — are **not implemented** in `service.py`/`_run_uvicorn`. Impact is currently bounded: the only live route is the status-word-only unauthenticated liveness endpoint. But a user who repoints `host` and later gains authenticated data routes would be exposed, so **non-loopback binding is unsupported until these controls land**. Trigger: the first authenticated data route (WI-2/WI-3), where the controls belong with the auth/middleware layer — or an explicit LAN-deployment milestone.

**Rekey crash-durability barrier ([ADR-0028](adr/0028-key-derivation-and-rotation.md), [ADR-0047](adr/0047-crypto-surface-implementation-decisions.md))**
`_rekey` preserves the `.pending` sidecar once `db.rekey` has succeeded, so an interrupted rotation is *recoverable* — `unlock()` detects the stray `.pending` and guides the fix (ADR-0047 §7). Full crash-*consistency* — an `fsync` of the rekeyed database pages before, and of the directory after, the `os.replace(pending, sidecar)`, under `synchronous=NORMAL` WAL — is not yet enforced, so a power loss in the narrow window between the sidecar install and the pages reaching disk could still require the pending-file recovery rather than opening cleanly. Deferred: a correct barrier needs crash-injection testing and is platform-nuanced. Trigger: the process-supervision / durability hardening pass (Phase 2+, alongside the ADR-0042 lock). Surfaced by the 2026-07-11 end-of-Phase-1 holistic review.

---

## Testing

~~**Canary manifest derivation — interim script vs. fixture loader**~~ → Resolved (Phase 1 WI-3b) — see Resolved section.

**Parallelize the CI test job under xdist ([testing-strategy.md](testing-strategy.md) Test Execution and Performance, [ADR-0045](adr/0045-repository-workflow-and-ci-enforcement.md))**
Local/manual full runs now use `pytest -n auto` (pytest-xdist), taking the suite from ~400 s to ~52 s. The **CI** test job is deliberately left serial because it feeds the mandatory log-canary gate, which streams all captured stdout/stderr/logs from one serial run (`--capture=tee-sys --log-cli-level=DEBUG`) into a single file for the scanner; xdist's per-worker capture and unsupported live-CLI logging would change what reaches that file and could silently weaken the gate. Resolving this means proving (or reworking) the canary scan under xdist — e.g. scanning each worker's captured output, or a post-run per-worker log-file sweep — then adding `-n auto` to the CI step. Trigger: CI wall-clock becomes a real constraint (it fans out across three OSes today, so per-OS time is not yet the pain point), or the E2E tests land (their spawned-process capture is the same canary-coupling concern at larger scale).

**Reduce Argon2id cost in logic-only key tests ([testing-strategy.md](testing-strategy.md) Test Execution and Performance, [ADR-0028](adr/0028-key-derivation-and-rotation.md))**
The rotation and `keys` CLI tests (`test_rotation`, `test_cli_keys`) derive keys at the production default (64 MiB, t=3, p=4) and are the suite's concentrated Argon2 cost (~20% of the serial run). They verify rotation *logic*, not parameter strength, so deriving at the OWASP floor (19 MiB, t=2, p=1 — the cheapest the sidecar will accept) would cut that ~5× with no loss of what those tests actually assert. This touches security-test fidelity and needs a shared test-params convention that does **not** weaken the known-answer KDF vectors (which must stay at their asserted params) — so it is an explicit decision, not a drive-by. Deferred as a lower-value follow-up now that xdist has taken the wall-clock pain off the table. Trigger: the parallel run stops being fast enough, or a batch of new key-path tests makes the cluster dominant again.

---

## Resolved

- **Biomarker category taxonomy** → First-class `categories` catalog table with a `category_id` FK on `biomarkers` (migration 0004, Phase 3 WI-2), replacing the free-text `category` column. `category_id` is `NOT NULL DEFAULT 0` pointing at a reserved `not_assigned` row (id 0, delete-guarded, display-renamable) — "not assigned" is a spec-defined concept in the data, not a NULL whose meaning lives in client code; this also keeps naive joins and by-category aggregations correct. Primary axis is **physiological system**; a theme earns a category only when its members have no system home (`environmental_toxins`, `screening`), else it is a future tag. Single primary category per biomarker now; cross-cutting tags deferred additively (see the open "Biomarker cross-cutting tags" entry above). Nineteen-category system-axis seed (`autoimmunity`, `allergy`, `body_composition`, `electrolytes`, `environmental_toxins`, `heart`, `hematology`, `hormones`, `immune`, `inflammation`, `kidney`, `liver`, `lipoproteins`, `metabolic`, `nutrients`, `pancreas`, `screening`, `thyroid`, `urine`) + the reserved default, owner-editable data forever after. Computed scores (Biological Age, IGF-1 Z-Score) are ADR-0044 derived data, not categorized biomarkers. Decided 2026-07-14 (Phase 3 decision gate D1). See [ADR-0055](adr/0055-biomarker-category-taxonomy.md).

- **Biomarker alias table** → Add it: `biomarker_aliases` (migration 0004, Phase 3 WI-2) — alias display form plus a normalized form (`NFKC → casefold → trim → collapse whitespace`) that is the resolver key, with provenance. Resolution is a single Core Service capability over the union of normalized canonical names and aliases: exact match on the normalized form only, fail-loud on unknowns, no fuzzy auto-matching on any non-interactive path (fuzzy is permitted only as interactive *suggestion*, confirmed by a human and recorded as a new alias). Namespace ambiguity is prevented at write time (schema `UNIQUE` on the normalized alias; app-level normalized checks across aliases ↔ canonical names). `POST /v1/import` `lab_results` rows accept exactly one of `biomarker_id`/`biomarker_name`, resolved before conflict detection. Output is canonical by construction — aliases exist only at resolution time. The LOINC lane ([ADR-0030](adr/0030-biomarker-identity.md)/[ADR-0032](adr/0032-biomarker-loinc-cardinality.md)) is untouched; this is the manual/PDF fallback it anticipated. Decided 2026-07-14 (Phase 3 decision gate D2). See [ADR-0054](adr/0054-biomarker-name-alias-fallback.md).

- **Canary manifest derivation — interim script vs. fixture loader** → The fixture loader owns it. `tests/fixture_loader.py` derives the manifest from the parsed typed fixture records (a declared per-column registry of owner health fields), and `scripts/scan_log_canary.py` imports and consumes it — the Phase-0 interim raw-text regex derivation is retired, eliminating the two-definition drift. Deriving from parsed records covers values reachable only through parsing, and the loader enforces the numeric grep-distinctness rule at derivation time. Fixture format narrowed from "JSON or SQL" to JSON only (raw SQL is not parseable into typed records). Resolved 2026-07-11 in the WI-3b PR; see [testing-strategy.md](testing-strategy.md) (Synthetic Test Data, CI Gates). No ADR: test infrastructure, no external contract, no new dependency, no security invariant touched.

- **Backup cadence and retention defaults** → Daily schedule, retention count 14. Recorded in [ADR-0038](adr/0038-backup-execution-and-verification.md)'s `[backup]` configuration section per the config-defaults-in-the-owning-ADR pattern; decided 2026-07-08 as a direct edit while ADR-0038 was still Proposed, immediately before the batch acceptance flip. Both knobs remain user-configurable.

- **Event sourcing** → Rejected. Everything it would buy this platform (audit, corrections, time-travel) the lighter pattern also delivers, without permanent materialized-view machinery; the one unique capability (replay-as-recovery) duplicates encrypted backups. See [ADR-0027](adr/0027-audit-trail-and-corrections.md).

- **Audit trail** → `audit_log` table in migration 0001: append-only (immutability enforced by schema triggers), written by the Core Service data-access layer in the *same transaction* as every mutation so it cannot drift. Records table, row, operation, old/new row images (JSON), UTC timestamp, actor (token name per ADR-0026), import batch, job, and reason. Distinct from ADR-0026's `auth_audit` (security record) and application logs (operational record). See [ADR-0027](adr/0027-audit-trail-and-corrections.md).

- **Longitudinal data correction** → `superseded_by` self-FK on every data table (option a). Value corrections insert the corrected row and point the original at it — never mutate in place; current state is `WHERE superseded_by IS NULL` via per-table `*_current` views. Carve-out: designated metadata repairs (the timezone correction workflow) update in place, fully audited. Deletes are hard deletes with a mandatory audit row preserving the full row image; clients flag the request and offer a backup first; supersession-chain rows are not deletable. See [ADR-0027](adr/0027-audit-trail-and-corrections.md).

- **CQRS** → CQRS-lite only. Writes exclusively via the validated Core REST path (mutation + audit row in one transaction, Core-emitted `data.*` event); reads hit current-state tables/views or ADR-0021 aggregates, which are rebuildable caches — never authoritative — invalidated by `data.imported`/`data.corrected`/`data.deleted` events. No formal command/query split. See [ADR-0027](adr/0027-audit-trail-and-corrections.md).

- **Timezone storage convention** → UTC as ground truth (ISO 8601). Every timestamp table carries four columns: `*_utc` (UTC, ground truth), `*_local_recorded` (original value from source, immutable), `*_local_tz` (IANA timezone name, best guess), `*_tz_inferred` (boolean — 1 if timezone was assumed not known). Correction workflow: update `local_tz`, recompute `*_utc` from `local_recorded`, clear `tz_inferred`. See [design-rationale.md](design-rationale.md) for the full convention.

- **Intervention dose history** → `intervention_dose_history` child table with FK to `interventions`. Key fields: `change_type` enum (`initiation`, `increase`, `decrease`, `hold`, `resumption`, `discontinuation`), `authority_type` enum (`prescribing_physician`, `supervising_clinician`, `self`, `protocol`), `ordered_by` free text (NULL when self), `reason` enum (`scheduled_titration`, `lab_result`, `symptom_response`, `side_effect`, `cost_or_availability`, `physician_directed`, `protocol_change`, `other`). `authority_type` and `reason` are orthogonal — a lab-result-driven change can be either physician-directed or self-adjusted. See [data-model.md](data-model.md).

- **Cloud backup strategy** → Cloud backup of the encrypted SQLite file is explicitly safe and recommended — but only the output of `healthspan db backup`, never the live database file. The database is AES-256 ciphertext (SQLCipher, ADR-0013), so the cloud provider cannot read it, and the provider is in the "do not trust" tier for storage — the encryption model handles confidentiality correctly. That is a separate question from consistency: the live file runs in WAL mode, so a sync client snapshotting it mid-write can capture a torn, unrecoverable copy — encryption makes a torn copy worse, not better, since it can't be partially salvaged. `healthspan db backup` produces a checkpointed, self-consistent, encrypted snapshot; that snapshot — and only that snapshot — is safe to sync continuously to Dropbox, iCloud Drive, OneDrive, Backblaze, or any similar service. See [ADR-0019](adr/0019-multi-device-sync.md) for the single-writer + backup-only-sync pattern.

- **Implementation language** → Python. Single language across all components; best ecosystem fit for data tooling, GUI (PySide6), and MCP server (fastmcp). See [ADR-0001](adr/0001-mcp-server-language.md) (Accepted; only its Nuitka distribution choice was superseded by ADR-0023).
- **Database backend** → SQLite-only for v1. Already committed in practice by SQLCipher encryption (ADR-0013) and the single-dialect migration runner (ADR-0009); PostgreSQL would require a new ADR revisiting both. See [ADR-0003](adr/0003-database-backend.md).
- **AI client interface** → MCP-based pluggability. The MCP server is the provider interface; client choice is user configuration, including fully local LLMs. See [ADR-0002](adr/0002-ai-provider-interface.md).
- **MCP transport** → Streamable HTTP (the MCP spec deprecated HTTP+SSE in its 2025-03-26 revision). Long-lived HTTP server, required for process isolation and AI client pluggability. See [ADR-0007](adr/0007-mcp-transport.md) and [ADR-0029](adr/0029-mcp-streamable-http.md).
- **Application architecture** → Layered process-isolated. Core Service as stable REST API contract; all other processes are clients. See [ADR-0006](adr/0006-application-architecture.md).
- **Process lifecycle** → Launcher script default; Docker Compose supported. See [ADR-0008](adr/0008-process-lifecycle.md).
- **Database migration** → Custom runner as `healthspan db migrate` CLI subcommand. See [ADR-0009](adr/0009-database-migration.md).
- **Ingestion strategy** → Structured pipeline; all writes via Core REST API bulk import endpoint with validation and atomic transactions. See [ADR-0004](adr/0004-data-ingestion-strategy.md).
- **CLI extensibility** → Directory-scanning plugin model; users drop `.py` files into plugins directory. See [ADR-0010](adr/0010-cli-plugin-model.md).
- **Clinical documents — full-text search strategy** → SQLite FTS5 external-content virtual table over the `body` column, tokenizer `porter unicode61 remove_diacritics 2` (recall-favoring for AI natural-language search; rebuild-reversible). The index inherits the SQLCipher boundary — its shadow tables are ordinary encrypted SQLite tables — so there is no second security surface. Sync is by INSERT/UPDATE/DELETE triggers shipped in migration 0001 with the documents table; the index covers all rows and current-state is filtered at query time via a `superseded_by IS NULL` join, keeping the triggers mechanical under the ADR-0027 correction model. Application-level scan is rejected (dead end as narrative accumulates); embedding/semantic search is a future additive plugin layer over FTS ([ADR-0010](adr/0010-cli-plugin-model.md)), not a replacement, and its vector store must live inside the encryption boundary. See [ADR-0041](adr/0041-clinical-document-fts.md).

- **Reference range frameworks** → Named framework table with per-biomarker range rows (option 2). Frameworks are first-class, queryable, and extensible as data additions; `effective_date` provides point-in-time lookup (option 3) for free without committing every framework to dated maintenance. Every range row carries a mandatory UCUM `unit`, and comparison unit-normalizes to the biomarker's canonical unit — closing the mg/dL-vs-g/L silent mis-flag. See [ADR-0005](adr/0005-reference-range-frameworks.md), [ADR-0030](adr/0030-biomarker-identity.md), [ADR-0031](adr/0031-units-and-ucum.md).
