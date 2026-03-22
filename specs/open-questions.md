# Open Questions

Architectural and technical decisions that need resolution before or during implementation. Personal action items (e.g. data collection tasks) are tracked separately in `specs/personal/`.

---

## Schema — Must Resolve Before Data Entry

~~**Timezone handling**~~ → Resolved — see Resolved section.

**Longitudinal data correction**
When a result was entered incorrectly and needs correction, overwriting silently loses history. Options: (a) `superseded_by` foreign key — the corrected row points to the replacement, original is kept; (b) a separate `corrections` table recording old value, new value, reason, and timestamp; (c) soft delete + re-entry with an audit trail entry. This affects the schema design for every data table and must be resolved before bulk data entry begins. See also: audit trail below.

**Audit trail**
A `audit_log` table recording every data mutation (insert, update, delete) with: table name, row ID, operation type, old values (JSON), new values (JSON), timestamp, and source (import batch ID, user action, plugin name). Not the same as application logs — this is a user-facing data integrity record. Must be in the schema from migration 0001; adding it later means historical changes have no record.

---

## Schema

**Biomarker alias table**
Add a `biomarker_aliases` table now, or handle lab-to-canonical name normalization at import time?
Adding it now keeps the schema self-contained and makes aliases a first-class concept. Handling it at import time defers complexity but risks inconsistency as data entry scales.

**Intervention dose history**
If a tracked intervention has dose changes over time, a `intervention_dose_history` child table is needed. Define the pattern now before any interventions are entered, or add it on first need?

**Biomarker category taxonomy**
The `biomarkers` table has a `category` column (lipids, metabolic, thyroid, hormones, inflammation, etc.). This taxonomy should be defined and documented before bulk data entry begins so categories are consistent across sources.

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

---

## Resolved

- **Timezone storage convention** → UTC as ground truth (ISO 8601). Every timestamp table carries four columns: `*_utc` (UTC, ground truth), `*_local_recorded` (original value from source, immutable), `*_local_tz` (IANA timezone name, best guess), `*_tz_inferred` (boolean — 1 if timezone was assumed not known). Correction workflow: update `local_tz`, recompute `*_utc` from `local_recorded`, clear `tz_inferred`. See [design-rationale.md](design-rationale.md) for the full convention.

- **Cloud backup strategy** → Cloud backup of the encrypted SQLite file is explicitly safe and recommended. The database file is AES-256 ciphertext (SQLCipher, ADR-0013); the cloud provider cannot read it. The provider is in the "do not trust" tier for storage — the encryption model handles this correctly. Recommended services: Dropbox, iCloud Drive, OneDrive, Backblaze, or any similar sync/backup service. Hot backups produced by `biocontext db backup` are also encrypted and safe to store in cloud. Cloud sync of the live file is safe for backup purposes but must respect SQLite's single-writer constraint — see ADR-0019 for the single-writer + cloud sync pattern.

- **Implementation language** → Python. Single language across all components; best ecosystem fit for data tooling, GUI (PySide6), and MCP server (fastmcp). See [ADR-0001](adr/0001-mcp-server-language.md).
- **MCP transport** → HTTP/SSE. Required for process isolation and AI client pluggability. See [ADR-0007](adr/0007-mcp-transport.md).
- **Application architecture** → Layered process-isolated. Core Service as stable REST API contract; all other processes are clients. See [ADR-0006](adr/0006-application-architecture.md).
- **Process lifecycle** → Launcher script default; Docker Compose supported. See [ADR-0008](adr/0008-process-lifecycle.md).
- **Database migration** → Custom runner as `biocontext db migrate` CLI subcommand. See [ADR-0009](adr/0009-database-migration.md).
- **Ingestion strategy** → Structured pipeline; all writes via Core REST API bulk import endpoint with validation and atomic transactions. See [ADR-0004](adr/0004-data-ingestion-strategy.md).
- **CLI extensibility** → Directory-scanning plugin model; users drop `.py` files into plugins directory. See [ADR-0010](adr/0010-cli-plugin-model.md).
