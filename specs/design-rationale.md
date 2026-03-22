# Health Data Platform — Design Rationale

This document explains the architectural decisions behind the platform. It is intended for contributors and anyone adapting this project for their own use. Discrete decisions with evaluated alternatives are captured as ADRs in `specs/adr/`; this document covers the broader principles that shape everything.

---

## Project Goal

A personal longitudinal health data platform that:
- Normalizes lab results, body composition, and clinical data spanning many years into a single database
- Exposes that data to an AI client via an MCP server for analytical conversations
- Handles the real-world messiness of multi-source health data — different labs, different assay platforms, changing reference ranges, and one-time clinical events that contextualize everything else

---

## Architectural Philosophy: Layered, Process-Isolated (Unix Principle)

The platform is built as a set of independent processes with well-defined interfaces, not as a monolith. Each process has one job. No process has privileged status. The AI client interface, GUI, import pipeline, and command-line interface (CLI) are all clients of the core service — none of them is the foundation.

This design has two practical goals:

**Power and approachability coexist.** A command-line interface, a REST API, and scripting hooks give power users bare-metal access. A GUI and a launcher script give non-technical users quality-of-life without hiding the underlying capability. Each layer is independently usable.

**Replaceability.** Any component can be replaced, forked, or reimplemented without changing the others. A contributor who dislikes the default GUI can build their own. A user who wants to connect a different AI client can do so. The core service remains stable regardless.

See [ADR-0006](adr/0006-application-architecture.md) for the full process architecture.

---

## Design Influences

**Home Assistant** is the closest architectural analog in open source. It is a Python, local-first, privacy-focused platform with a plugin-driven architecture and an event bus at its core. The comparison is instructive both for what to do and what to avoid:

- HA's event bus is central to its extensibility — this platform treats it the same way
- HA's plugin (integration) system is the reason it became the dominant home automation platform; the same philosophy applies here to health data
- HA's most persistent criticism is the absence of encryption at rest — this platform treats encryption as a day-one requirement, not a future enhancement
- HA's add-on system (Docker containers for complex plugins) is a future growth path here

**The goal is not to build a health automation platform.** The goal is to build a personal health data platform that is as extensible and community-friendly as Home Assistant is for home automation.

---

## Versioning Surfaces

Versioning is applied consistently across all interfaces where a breaking change could affect users or integrations without warning. These surfaces are:

| Surface | Mechanism | Notes |
|---|---|---|
| REST API | URL prefix (`/v1/`) | Applied from the first endpoint |
| Database schema | `schema_version` table + numbered migration files | See ADR-0009 |
| TOML config file | `config_version` field | Allows automatic migration of old configs |
| Plugin interface | `PLUGIN_API_VERSION` integer in each plugin | CLI skips incompatible plugins with a warning |
| Import file formats | Version field in import envelope | Each source importer declares its expected format version |
| MCP tool interface | Additive changes only; breaking changes get a new tool name | MCP has no native versioning mechanism |

The principle: callers must never encounter a silent breaking change. Either the interface is backwards-compatible, or the version number changes and the old version is maintained until deprecated.

---

## Why SQLite (Default)

The local-first default is the right choice for **simplicity and zero infrastructure dependency** — not primarily for data protection. ADR-0013 establishes AES-256 encryption at rest (SQLCipher), making the database file opaque ciphertext that is safe for cloud backup and sync regardless of where the file is stored. Data protection is solved at the file layer; local-first is about not requiring a server to be running somewhere.

SQLite gives you:
- A single portable file — trivial to copy, backup, and move between machines
- Full SQL query capability with no connection layer
- Hot backups via the SQLite Online Backup API that preserve encryption (the backup file is also encrypted ciphertext)
- Direct integration with the MCP server and Core Service without a network hop

The MCP server queries SQLite and returns focused result sets to the AI client, keeping context window usage efficient regardless of how large the database grows.

**The local-first constraint that remains real** is SQLite's single-writer model: only one Core Service instance may write at a time. Concurrent writes from multiple machines corrupt the database. This is a concurrency constraint, not a privacy constraint. It is addressed by the single-writer + cloud sync pattern (ADR-0019) for multi-device use, or by switching to the PostgreSQL backend (ADR-0003) for true multi-master write access.

---

## The Core Design Challenge: Multi-Source Lab Data

The most important design decision in this schema is treating lab source as a **first-class attribute** on every result row, not an afterthought.

### Why this matters

Fasting insulin is a canonical example of the broader problem. Unlike glucose or HbA1c — which are standardized across platforms — insulin is measured by immunoassay, and there is no universal calibration standard. Different labs use different antibody clones, different assay platforms (Roche Elecsys, Abbott Architect, Siemens, etc.), and different reference ranges. The same patient, properly fasted, drawn on the same day, can receive results that differ by 2-5x depending on which lab processes the sample.

This means:
- Trend analysis must be done **within a single lab's series** where possible
- Cross-lab comparisons of absolute values are unreliable for immunoassay-based markers
- A rising trend in one lab's series and a flat trend in another lab's series for the same biomarker is diagnostic of an assay issue, not a genuine metabolic change
- When a lab switches platforms or is acquired by another lab network, the longitudinal series is effectively broken and should be annotated as such

### Implications for schema design

- `results` rows have a `lab_id` foreign key — not optional
- `labs` table is a proper entity, not just a text field
- Reference ranges are stored **per result row**, not per biomarker — they vary by lab, by patient demographics, and change over time
- `biomarkers` table uses a **canonical name** to normalize naming inconsistencies across labs (e.g. "Glucose, Serum" and "Fasting Glucose" map to the same canonical biomarker)
- A `draw_context` field captures the ordering context (annual physical, comprehensive panel, etc.) which helps interpret why certain biomarkers appear together

---

## Timestamp and Timezone Convention

**UTC is the ground truth. ISO 8601 is the wire and storage format. IANA timezone names are the local context.**

Every table with a timestamp carries four columns rather than one:

```sql
draw_timestamp_utc     TEXT NOT NULL,     -- ground truth: '2026-01-15T13:30:00Z'
draw_local_recorded    TEXT,              -- original value from source: '2026-01-15T08:30:00'
draw_local_tz          TEXT,              -- IANA name: 'America/Detroit'
draw_tz_inferred       INTEGER DEFAULT 0  -- 1 = timezone was assumed, not known from source
```

The `*_utc` column is the authoritative timestamp used in all queries and calculations. The `*_local_recorded` column is immutable — it preserves exactly what the source provided, forever. The `*_local_tz` column is the best available knowledge of what timezone the event occurred in. The `*_tz_inferred` flag distinguishes confirmed timezone knowledge from an educated guess.

### Ingestion rules

| Source provides | Action |
|---|---|
| UTC timestamp | Store directly as `*_utc`; set `local_tz` to user's configured home timezone; `tz_inferred = 1` |
| Local time + explicit timezone | Convert to UTC; store both; `tz_inferred = 0` |
| Local time, timezone unknown | Store local time in `*_local_recorded`; convert using home timezone as best guess; `tz_inferred = 1` |
| Date only, no time | Store as midnight UTC; `tz_inferred = 1`; flag for review |

### Correction workflow

When `tz_inferred = 1` records are reviewed and corrected:
1. Update `*_local_tz` to the correct IANA timezone name
2. Recompute `*_utc` from `*_local_recorded` + corrected timezone
3. Set `tz_inferred = 0`
4. The `audit_log` records the correction

The `*_local_recorded` value is never modified — it is the permanent record of what the source said.

### Why this matters for health data specifically

CGM glucose patterns are only clinically interpretable relative to local time — a spike at 08:30 UTC means nothing without knowing whether that was breakfast time or the middle of the night. Lab draws have fasting context that depends on local time. The separation of UTC (analytical ground truth) from local timezone (clinical context) makes both concerns correct independently.

---

## Canonical Biomarker Names

Different labs name the same biomarker differently. Normalizing at the schema level — rather than at query time — keeps analysis clean.

The `biomarkers` table holds one canonical name per biomarker. A `biomarker_aliases` table can be added as data entry reveals naming inconsistencies. The canonical name is the human-readable standard you choose; it doesn't need to match any lab's specific naming.

Categories (lipids, metabolic, thyroid, hormones, inflammation, etc.) on the `biomarkers` table enable panel-level queries without hardcoding biomarker lists.

---

## Clinical Events and Interventions as First-Class Entities

Raw biomarker trends are only partially interpretable without knowing what was happening clinically. Two separate tables handle this:

**`events`** — point-in-time occurrences: surgeries, hospitalizations, diagnoses, significant lifestyle changes. These become annotation points on any trend chart or analytical query.

**`interventions`** — duration-based: medications, supplements, therapies with start dates, end dates, doses, and routes. These are essential for before/after analysis. A dose history child table should be added if dose changes are clinically significant for the intervention being tracked.

The design intent is that any biomarker query can be overlaid with the event and intervention timeline to ask: *what changed after X started, or before and after Y occurred?*

---

## Body Composition

All body composition measurements share a single `body_composition` table with a `source` column, regardless of device. Current sources are InBody 120 and InBody 580 (via Enara Health). More capable devices produce additional metrics (e.g. phase angle, ECW/TBW ratio, intracellular/extracellular water) that are NULL for records from devices that don't measure them. This keeps body composition queries simple — one table, filter by source if device-specific metrics are needed.

---

## High-Volume Continuous Data

CGM (continuous glucose monitor) data and raw wearable data are kept in separate tables from periodic lab results for several reasons:
- CGM produces readings every 5 minutes — potentially millions of rows over years of use
- Indexing strategy differs: timestamp-based range queries rather than date + biomarker lookups
- Most analysis joins CGM to periodic lab data (HbA1c, fasting glucose) at the daily or weekly aggregate level, not at the individual reading level

Wearable data (Fitbit, Apple Watch, etc.) is stored as **daily aggregates** rather than raw intraday data. Most analytical questions operate at the daily level, and intraday data adds significant volume without proportional analytical value for most use cases. Intraday tables can be added if sleep stage analysis or heart rate variability becomes a priority.

---

## Schema Versioning

A `schema_version` table tracks applied migrations. All schema changes should be written as migration scripts in `sql/migrations/` rather than modifying `schema.sql` directly after initial deployment. This allows:
- Reproducible database reconstruction from scratch
- A test database with synthetic data that can be migrated in parallel
- Clear history of when the schema changed and why

---

## MCP Server Design

The MCP server is a thin layer over SQLite. It exposes focused tools rather than raw SQL access:

- `get_biomarker_history` — results for a named biomarker, with optional lab and date range filters
- `get_panel_by_date` — all results from a specific draw date
- `get_abnormal_results` — flagged values across any time range
- `get_labs` — reference list of lab sources
- `search_biomarkers` — fuzzy name match for discovery
- `get_events_and_interventions` — clinical timeline for context overlay on any analysis

The rationale for tools over raw SQL: natural language queries from an AI client map better to semantically named tools than to ad hoc SQL generation. It also keeps the AI client from accidentally scanning large tables without appropriate filters.

---

## What Does Not Belong in Git

- The SQLite database file (`*.db`, `*.db-shm`, `*.db-wal`)
- Any personal health data, even in document form
- Personal context documents used to orient Claude during development sessions

The schema DDL (`schema.sql`) and migration scripts are the source of truth for database structure. The database itself should be backed up via appropriate means (e.g. a cloud backup service) independently of the Git repository.

See `.gitignore` for the complete exclusion list.

---

## Adapting This Project

This schema is designed around one person's data but is intentionally generic. To adapt it:

1. Review the `biomarker-catalog.md` for the canonical name and category conventions used
2. Add lab sources to the `labs` table that match your providers
3. The `interventions` table is flexible — any medication, supplement, or lifestyle intervention can be tracked
4. CGM and wearable tables are optional — the core value is in the `results`, `events`, and `interventions` tables
5. The MCP server tools can be extended with additional query patterns as analytical needs emerge
