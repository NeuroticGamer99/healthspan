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

**Biomarker alias table**
Add a `biomarker_aliases` table now, or handle lab-to-canonical name normalization at import time?
Adding it now keeps the schema self-contained and makes aliases a first-class concept. Handling it at import time defers complexity but risks inconsistency as data entry scales.
Narrowed by [ADR-0030](adr/0030-biomarker-identity.md): incoming LOINC codes resolve directly to a biomarker, so electronic feeds largely bypass name-based aliasing; the open question is now specifically the name-based *fallback* for PDF-extracted and manually entered results. Related: one biomarker concept can map to several LOINC codes ([ADR-0032](adr/0032-biomarker-loinc-cardinality.md)).

~~**Intervention dose history**~~ → Resolved — see Resolved section.

**Biomarker category taxonomy**
The `biomarkers` table has a `category` column (lipids, metabolic, thyroid, hormones, inflammation, etc.). This taxonomy should be defined and documented before bulk data entry begins so categories are consistent across sources.

**Additive `ALTER TABLE` migrations must recreate the affected `*_current` view ([data-model.md](data-model.md), [ADR-0027](adr/0027-audit-trail-and-corrections.md))**
The migration-0001 `*_current` views are `SELECT * FROM <table> WHERE superseded_by IS NULL`, and SQLite binds `SELECT *` to the base table's columns at `CREATE VIEW` time. A later migration that does `ALTER TABLE … ADD COLUMN` will therefore not surface the new column through the current-state view until the view is dropped and recreated. Convention to adopt when the first additive migration lands: any migration adding a column to a content table also `DROP VIEW`/`CREATE VIEW`s that table's `*_current` view (and a schema-integrity test asserts the view column set matches the base table). Trigger: the first `ALTER TABLE ADD COLUMN` migration (the header of `0001_initial_schema.sql` anticipates these for `import_batches`/`jobs`). Surfaced by the 2026-07-11 end-of-Phase-1 holistic review.

~~**Clinical documents — full-text search strategy**~~ → Resolved (FTS5 external-content over `body`) — see Resolved section and [ADR-0041](adr/0041-clinical-document-fts.md).

**Subjective observation vocabulary ([data-model.md](data-model.md), Subjective Observations)**
The journal type starts freeform — `body` text only. Should a structured vocabulary (tags; 1–10 scales for energy, mood, pain, sleep quality) be added, and when? Structured fields make longitudinal plots of subjective state possible, but structure that arrives before the logging habit does tends to kill the habit. Ties to the Levels export question (Data Ingestion below): if the "how I'm feeling" tracker is in the activity-log export, its note-type vocabulary should map onto whatever is chosen here. Defer until real entries accumulate and show what structure they actually carry.

**Derived data points — deferred schema ([ADR-0044](adr/0044-derived-data-points.md))**
ADR-0044 fixes the principle (derived values are a distinct content class, never stored in source-data tables; interim home is the `result_data` attachment on analyses) and deliberately defers the derived-series table. Sub-questions recorded for when it is designed: (1) internally computed (formula stored, rebuildable) vs. externally computed (opaque snapshot) — the two subtypes behave differently under the [ADR-0027](adr/0027-audit-trail-and-corrections.md) correction model; (2) does a derived series get a `biomarker_id` ([ADR-0030](adr/0030-biomarker-identity.md))? (3) may derived series be range-compared ([ADR-0005](adr/0005-reference-range-frameworks.md)) — HOMA-IR has published ranges, so a blanket "never" is wrong; (4) staleness marking when a source row feeding a derived point is superseded. Design trigger: the analyses table exists in a running system and accumulated attachments show which subtype dominates.

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

**Recovery Kit OS print pathways and orphan sweep ([ADR-0033](adr/0033-plaintext-artifact-disposal.md), [ADR-0047](adr/0047-crypto-surface-implementation-decisions.md))**
WI-2 shipped kit rendering as terminal display plus explicit `--output` file (ADR-0047 §4); ADR-0033's OS print integration (`lp`/`lpr` streaming, Windows temp-file shell print with verified disposal) and the orphan startup sweep are deferred. The sweep's home is the Core Service startup failure-cleanup (Phase 2); the print pathways land with it or with the user-documentation milestone, whichever comes first. Trigger: Core Service startup sequence implementation (Phase 2).

**`db backup` / `db restore` exclusive-access enforcement ([ADR-0038](adr/0038-backup-execution-and-verification.md), [ADR-0042](adr/0042-process-supervision-and-single-instance-locking.md))**
WI-4 shipped the offline `healthspan db backup` (verify-then-publish + retention pruning) and `healthspan db restore` (verify-then-install) CLI commands and the verified-backup/verify pipelines they run. ADR-0038 requires both to *refuse while Core Service is up*, and `db restore` to hold the ADR-0042 advisory lock on `<database-path>.lock` for its duration. Neither guard ships in Phase 1: there is no Core Service to detect and no launcher lock yet (both ADR-0042 machinery), and in Phase 1 the CLI is the sole database opener, so exclusivity holds by construction. Trigger: single-instance locking / Core Service startup implementation (Phase 2) — the `.lock` acquisition and the service-up refusal land with ADR-0042's advisory lock. Also then: the in-service scheduled `backup.database` job (ADR-0038's scheduled producer), of which the WI-4 CLI is the offline counterpart.

**Rekey crash-durability barrier ([ADR-0028](adr/0028-key-derivation-and-rotation.md), [ADR-0047](adr/0047-crypto-surface-implementation-decisions.md))**
`_rekey` preserves the `.pending` sidecar once `db.rekey` has succeeded, so an interrupted rotation is *recoverable* — `unlock()` detects the stray `.pending` and guides the fix (ADR-0047 §7). Full crash-*consistency* — an `fsync` of the rekeyed database pages before, and of the directory after, the `os.replace(pending, sidecar)`, under `synchronous=NORMAL` WAL — is not yet enforced, so a power loss in the narrow window between the sidecar install and the pages reaching disk could still require the pending-file recovery rather than opening cleanly. Deferred: a correct barrier needs crash-injection testing and is platform-nuanced. Trigger: the process-supervision / durability hardening pass (Phase 2+, alongside the ADR-0042 lock). Surfaced by the 2026-07-11 end-of-Phase-1 holistic review.

---

## Testing

~~**Canary manifest derivation — interim script vs. fixture loader**~~ → Resolved (Phase 1 WI-3b) — see Resolved section.

---

## Resolved

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
