# Data Model — Health Data Types

A living document cataloging the types of data this platform needs to represent, their sources, and schema considerations. This is a design surface, not a decision record — it evolves as new sources are onboarded and analytical needs become clearer.

Discrete architectural decisions that emerge from this document should be captured as ADRs.

---

## Data Type Inventory

### Lab Results
- **Sources:** Primary care (Corewell/Beaumont → Quest), Function Health (Quest)
- **Cadence:** Periodic (annual physical, targeted panels)
- **Volume:** Low-to-medium (tens of biomarkers per draw, multiple draws per year)
- **Schema considerations:** Lab source as first-class attribute; canonical biomarker names for display with LOINC as the standard identifier ([ADR-0030](adr/0030-biomarker-identity.md)); UCUM units with unit-normalized comparison ([ADR-0031](adr/0031-units-and-ucum.md)); result values as numeric + comparator + text (below-detection and qualitative results); reference ranges stored per result row; draw context (what ordered this panel)
- **Status:** Partially designed — see [design-rationale.md](design-rationale.md)

### Body Composition
- **Sources:** Any body composition scanner; currently InBody 120 and InBody 580 (via Enara Health)
- **Cadence:** Periodic (monthly or less)
- **Volume:** Low
- **Schema considerations:** Single table with `source` column; device-specific metrics (e.g. phase angle, ECW/TBW, intracellular/extracellular water) are NULL for devices that don't produce them
- **Status:** Partially designed — see [design-rationale.md](design-rationale.md)

### Continuous Glucose (CGM)
- **Sources:** Levels (primary); Dexcom API (potential direct hardware source — more resilient if Levels subscription lapses)
- **Cadence:** Continuous (reading every 5 minutes)
- **Volume:** High (potentially millions of rows over years)
- **Schema considerations:** Separate table from periodic labs; timestamp-based range indexing; most analysis joins to lab data at daily/weekly aggregate level
- **Timezone note:** Consumer CGM apps commonly export timestamps in local time without explicit timezone metadata. The first CGM import will likely result in all records having `tz_inferred = 1`. This is expected and handled — a single timezone correction pass recomputes UTC across the entire batch. Plan for this review step after the first import.
- **Export:** Levels exports glucose data as CSV. **Delivery is async — Levels emails a download link when the export is ready.** This makes fully automated export difficult; watch folder import (user saves file, importer picks it up) is the preferred approach for this data type. CSV column schema unknown until a real export is inspected.
- **Import adapter:** `levels.glucose`
- **Status:** Partially designed — see [design-rationale.md](design-rationale.md)

### Metabolic Context (Levels — Zone Data)
- **Sources:** Levels
- **Cadence:** Per-session / per-day computed scores
- **Volume:** Low
- **Description:** Levels' proprietary computed layer: Zone scores and Glucose Response analysis. Represents Levels' interpretation of CGM + context data. Stored as read-only imported values — cannot be independently recomputed. Long-term, analysis plugins can compute similar metrics from raw data.
- **Export:** JSON export available manually via Levels export page.
- **Import adapter:** `levels.zones`
- **Status:** Not yet designed — new data type

### Activity Logs (Levels)
- **Sources:** Levels
- **Cadence:** Event-based (per food entry, per exercise session, per note)
- **Volume:** Low-to-medium
- **Description:** Food events, exercise sessions, and free-form notes. Provides the contextual layer that makes CGM data clinically interpretable. The "how I'm feeling" subjective tracker may appear here as a note type — confirm when inspecting a real export.
- **Export:** CSV export available manually via Levels export page.
- **Import adapter:** `levels.activity`
- **Status:** Not yet designed — new data type; overlaps with the [Subjective Observations (Journal)](#subjective-observations-journal) type and activity event types — imported Levels notes would land there as patient-authored entries with import provenance

### Nutrition Logs (Levels)
- **Sources:** Levels
- **Cadence:** Per meal / per food item
- **Volume:** Low-to-medium
- **Description:** Food log entries with nutritional metadata (macros, micros). Distinct from activity logs — activity logs capture food timing and context; nutrition logs carry the detailed nutritional content. Both are needed for full metabolic analysis.
- **Export:** CSV export available manually via Levels export page.
- **Import adapter:** `levels.nutrition`
- **Status:** Not yet designed — new data type

### Wearable / Activity
- **Sources:** Fitbit
- **Cadence:** Daily aggregates (not raw intraday)
- **Volume:** Medium (one row per day per metric)
- **Metrics:** Steps, active minutes, resting HR, sleep duration, sleep score, HR zone minutes
- **Schema considerations:** Daily aggregate table; intraday tables deferred unless HRV or sleep stage analysis becomes a priority
- **Status:** Partially designed — see [design-rationale.md](design-rationale.md); export not yet attempted

### Clinical Events
- **Sources:** Manual entry
- **Cadence:** Point-in-time, infrequent
- **Examples:** Arterial stent placement, hospitalizations, diagnoses, significant lifestyle changes
- **Schema considerations:** Date, type, description, free-text notes; used as annotation points on all trend analysis
- **Status:** Table defined; no data entered yet

### Interventions
- **Sources:** Manual entry
- **Cadence:** Duration-based (start date, end date or ongoing)
- **Examples:** TRT, medications, supplements, therapies
- **Schema considerations:** Dose, route, frequency; **current dose is a computed read, not a stored column** — a view (or repository-layer query) over the latest `intervention_dose_history` row. Dose history is the source of truth; "current dose" is a query against it. A stored denormalized column would fit no [ADR-0027](adr/0027-audit-trail-and-corrections.md) mutation category (neither a value supersession nor a designated metadata repair) and would generate audit rows on every dose change for data that is not itself source data
- **Status:** Table defined; no data entered yet

### Intervention Dose History
- **Relationship:** Child table of `interventions` (many dose-history rows per intervention)
- **Purpose:** Records every dose change for an intervention, preserving the full titration history with who directed the change and why — critical for correlating lab trends against dose adjustments over time
- **Schema considerations:**
  - `intervention_id` — FK to `interventions`
  - `effective_date` — when this dose took effect; timestamp quadruple (UTC + local + tz + inferred flag)
  - `dose`, `unit` — e.g. `200`, `mg/wk` (UCUM string, [ADR-0031](adr/0031-units-and-ucum.md); `wk`, not `week`)
  - `change_type` enum: `initiation`, `increase`, `decrease`, `hold`, `resumption`, `discontinuation`
  - `authority_type` enum: `prescribing_physician`, `supervising_clinician`, `self`, `protocol` — the primary axis for distinguishing medically directed changes from self-adjustment
  - `ordered_by` — free text; name/role of the directing party (NULL when `authority_type = 'self'`)
  - `reason` enum: `scheduled_titration`, `lab_result`, `symptom_response`, `side_effect`, `cost_or_availability`, `physician_directed`, `protocol_change`, `other` — why the change was made; orthogonal to who made it
  - `notes` — free text for additional context (e.g. "testosterone trough was 420, targeting 600-800")
  - Standard audit columns
- **Key design note:** `authority_type` and `reason` are intentionally orthogonal axes. The same `reason` can occur under different authorities, and the combination carries meaning that neither field expresses alone:

  | `reason`           | `authority_type`         | Meaning |
  |--------------------|--------------------------|---------|
  | `lab_result`       | `prescribing_physician`  | Doctor reviewed your testosterone trough and called in a new dose |
  | `lab_result`       | `self`                   | You reviewed your own labs and adjusted without physician involvement |
  | `symptom_response` | `supervising_clinician`  | NP adjusted based on reported symptoms at a follow-up visit |
  | `symptom_response` | `self`                   | You adjusted based on how you were feeling |
  | `scheduled_titration` | `protocol`            | Dose increase following a published TRT protocol, not a specific physician directive |
  | `side_effect`      | `self`                   | You reduced dose due to elevated hematocrit or other adverse sign |
  | `side_effect`      | `prescribing_physician`  | Physician directed reduction after reviewing labs showing adverse effect |

  This lets an AI client answer questions that require both dimensions: *"show me all self-directed dose changes"*, *"what dose was I on when my hematocrit spiked, and who ordered it?"*, or *"have any of my self-adjustments been later validated by a physician titration in the same direction?"*
- **Status:** Designed — ready to implement

### Clinical Documents & Visit Notes
- **Sources:** Manual entry; future: patient portal export (FHIR DocumentReference, CCDA), PDF import
- **Cadence:** Event-based (per encounter)
- **Examples:** Doctor's notes, specialist referral letters, discharge summaries, clinician interpretations of labs, care plan summaries, second-opinion write-ups
- **Schema considerations:**
  - `encounter_date` (timestamped to the visit, not the import)
  - `provider_name`, `provider_role` (PCP, cardiologist, endocrinologist, etc.), `practice_name`
  - `document_type` enum: `visit_note`, `lab_interpretation`, `referral`, `discharge_summary`, `care_plan`, `imaging_report`, `other`
  - `body` — full free-text content; the primary queryable surface. Indexed for search by an FTS5 external-content virtual table shipped in migration 0001 with this table; the index inherits the SQLCipher encryption boundary and stays in sync via triggers, filtering to current rows at query time ([ADR-0041](adr/0041-clinical-document-fts.md))
  - `source_format` — how it arrived: `manual_entry`, `pdf_extracted`, `fhir_document`, `ccda`
  - `source_file_hash` — SHA-256 of original file if imported from a document; enables deduplication and keys the stored original ([ADR-0034](adr/0034-clinical-document-storage.md))
  - Original files (PDFs, CCDA, FHIR document payloads) are retained as content-addressed BLOBs inside the encrypted database — never in a plaintext directory. Size guardrail and future cold-store escape hatch in [ADR-0034](adr/0034-clinical-document-storage.md).
  - `author_type` enum: `clinician` (formal note from provider), `patient` (your own notes taken during/after the visit) — allows AI clients to weight or filter by source perspective
  - Links to related data: junction tables — `document_lab_draws`, `document_events`, `document_interventions` — each a two-column link (`document_id` + the target row's FK) rather than an in-row array (SQLite has no array type and cannot enforce a foreign key inside JSON). As real foreign keys they participate in `foreign_key_check` ([ADR-0035](adr/0035-migration-execution-semantics.md)) and the audit model ([ADR-0027](adr/0027-audit-trail-and-corrections.md)); link rows are audited as `insert`/`delete` and are not supersession-chained — a link exists or it does not, so correcting one is a delete plus an insert, not a value supersession
  - Timestamp quadruple on `encounter_date` (same UTC + local + tz convention as all other tables)
- **AI/MCP value:** This is one of the highest-value data types for AI client interactions. Clinician narrative captures reasoning, differential diagnoses, and interpretation context that structured lab values cannot express. MCP tools can surface relevant visit notes alongside lab trends, enabling an AI client to answer questions like "what did my cardiologist say about my LDL trajectory?" or "summarize all provider guidance on my insulin resistance" by FTS5 full-text search across the `body` column ([ADR-0041](adr/0041-clinical-document-fts.md)).
- **Status:** Not yet designed — prioritized; original-file storage boundary decided ([ADR-0034](adr/0034-clinical-document-storage.md))

### Subjective Observations (Journal)
- **Sources:** Manual entry (GUI/CLI); possibly Levels activity-log notes on import (see the Levels export open question)
- **Cadence:** Event-based, freeform — whenever the owner has something to record
- **Volume:** Low
- **Description:** First-person, contemporaneous narrative: how you feel today, what you think of the current training block, a new ache and a suspicion about its cause. Content class: **source** — the fact that the owner felt or suspected something on a given date is itself a datum, and its narrative form cannot be confused with a measurement ([provenance-and-derived-data.md](provenance-and-derived-data.md)). Distinct from Analyses & Interpretations below: a journal entry records in the moment; an analysis synthesizes in retrospect.
- **Schema considerations:**
  - `observed_at` — timestamp quadruple (UTC + local + tz + inferred flag)
  - `body` — free text, the primary surface; FTS5-indexed via the [ADR-0041](adr/0041-clinical-document-fts.md) pattern (own external-content virtual table and triggers)
  - Junction links to what the entry is about — `observation_interventions`, `observation_events` (same two-column link pattern as clinical documents) — so "new ache since starting X" is a real foreign key an AI client can traverse, not just prose
  - Structured vocabulary (tags, 1–10 scales for energy/mood/pain) deliberately deferred — freeform first, structure when real entries show what they carry (see [open-questions.md](open-questions.md))
  - Standard audit columns and supersession ([ADR-0027](adr/0027-audit-trail-and-corrections.md))
- **Note:** Subsumes the former "Subjective health logs" candidate from the not-yet-evaluated list. The original caveat stands: confirm whether the Levels activity-log export carries the "how I'm feeling" tracker before designing the import mapping — imported Levels notes would land here as patient-authored entries with import provenance.
- **Status:** Sketched — schema considerations above; vocabulary and Levels mapping open

### Analyses & Interpretations
- **Sources:** Manual entry (owner-authored); AI clients via the MCP write path ([ADR-0043](adr/0043-ai-authored-analyses-and-annotate-scope.md))
- **Cadence:** Event-based (per analysis performed)
- **Volume:** Low
- **Description:** Retrospective synthesis over stored data — the owner's conclusions and AI-authored analysis, in one table, distinguishable per row. Content class: **interpretation** — never written into source-data tables (INV-6, [ADR-0044](adr/0044-derived-data-points.md)). Longitudinal self-review ("what did I conclude last quarter, and what did the model conclude?") is a query over prior rows, not a folder of external documents.
- **Schema considerations:**
  - `analysis_date` — timestamp quadruple
  - `author_type` enum: `self` | `ai` — **stamped by the Core Service from the writing token's `authorship` attribute, never caller-supplied** ([ADR-0043](adr/0043-ai-authored-analyses-and-annotate-scope.md)); `author_token` records the stamped token name; `tool_info` is optional caller-supplied text (model name/version) stored as a claim, distinct from the stamped identity
  - `title`, `body` — narrative; `body` FTS5-indexed via the [ADR-0041](adr/0041-clinical-document-fts.md) pattern
  - Junction links to the data analyzed — `analysis_lab_draws`, `analysis_documents`, `analysis_interventions`, `analysis_observations` (two-column link pattern)
  - `result_data` — optional structured JSON attachment for small computed result sets; the interim home for derived data points per [ADR-0044](adr/0044-derived-data-points.md) (reviewable and searchable, not yet plottable as first-class series)
  - Standard audit columns and supersession; supersede/delete restricted by the author guard ([ADR-0043](adr/0043-ai-authored-analyses-and-annotate-scope.md)): a token manages its own rows, owner-held tokens manage all
- **AI/MCP value:** High in both directions — AI clients write attributed analysis here (`read annotate` token), and reads return `author_type`/`tool_info` so a model can distinguish prior interpretation (including its own) from measurement ([provenance-and-derived-data.md](provenance-and-derived-data.md), Presentation rule)
- **Status:** Sketched — direction set by [provenance-and-derived-data.md](provenance-and-derived-data.md), [ADR-0043](adr/0043-ai-authored-analyses-and-annotate-scope.md), [ADR-0044](adr/0044-derived-data-points.md)

---

## CGM and Mobile Health Platform APIs

### Direct CGM APIs
- **Dexcom Developer API** — official OAuth-based REST API, web-accessible without a mobile app. Most authoritative source for Dexcom CGM data. Should be the primary CGM API target. Provides real-time and historical readings.
- **Abbott LibreView** — Abbott's cloud platform for Libre sensors has limited API access; less open than Dexcom. Research needed.
- **Nightscout** — open source CGM data bridge with a REST API. If the user already runs Nightscout, it aggregates CGM data from multiple sensor types and is cleanly accessible from a desktop. An import adapter would be straightforward.

### Mobile Health Platform APIs
**Apple HealthKit** (iOS) and **Google Health Connect** (Android) are valuable because they aggregate data from many health apps in one place — Levels, Dexcom, Fitbit, wearables, and more — providing a single integration point per platform. Both are **sandboxed to native mobile apps** and cannot be queried directly from a Python desktop application.

**Apple HealthKit / Apple Health**
- **XML bulk export** — the iOS Health app can export all HealthKit data as a bulk XML file. Desktop-accessible without a native app. Comprehensive single-source import for iOS users; covers data from any HealthKit-connected app. Import adapter is viable near-term.
- **Live HealthKit API** — sandboxed; requires a native iOS app. Future direction if a mobile companion app is built.

**Google Health Connect** (Android)
- **Live API** — sandboxed; requires a native Android app. No bulk desktop export mechanism equivalent to Apple Health XML.
- **Samsung Health** — on Samsung Galaxy devices, Samsung Health acts as an aggregator alongside Health Connect. Has a manual CSV export and a developer API worth researching. Relevant for testing and development since the project developer uses a Samsung S23 Ultra.
- **Third-party bridge** — services like Terra API expose Health Connect data via REST without a native app. Tradeoff: health data transits a third-party service, which conflicts with the local-first privacy model.

**Third-party aggregators (Terra API, etc.)** — cover both platforms via a single REST API. Convenient but introduce an external dependency and a privacy concern. Worth noting as a path for users who cannot use platform-native options.

### Recommended approach
- **Near term:** Dexcom API for authoritative CGM data; Apple Health XML export adapter for iOS users; Samsung Health manual export for Android/Galaxy users during development
- **Medium term:** Investigate Samsung Health and Google Health Connect web APIs; Nightscout adapter
- **Future:** Native iOS and Android companion apps unlock live HealthKit and Health Connect APIs without third-party dependency

---

## Data Types Not Yet Evaluated

The following are candidate data types that may be worth modeling. Each needs research before schema design begins.

- **Genomic / genetic data** (e.g. 23andMe raw data, MTHFR and other SNPs)
- **Blood pressure / home vitals** (manual or connected device)
- **Medication adherence** (distinct from the intervention record itself)
- **Imaging reports** (radiology reads, echo reports — structurally similar to visit notes but distinct document type; covered by `document_type = imaging_report` in the Clinical Documents table)
- **Additional wearable sources** (Apple Watch, Oura, Whoop)
- **Dietary logs / nutrition** (partially covered by Levels nutrition logs export; general nutrition tracking beyond Levels is a separate concern)

---

## Cross-Cutting Schema Concerns

Topics that affect multiple data types and need consistent treatment:

- **Units and unit normalization** — decided ([ADR-0031](adr/0031-units-and-ucum.md)): units stored as UCUM strings, a canonical unit per biomarker, all comparisons normalized to it; the conversion engine (ucumvert vs. a curated table) is an open sub-decision recorded in that ADR
- **Biomarker identity** — decided ([ADR-0030](adr/0030-biomarker-identity.md)): internal surrogate `biomarker_id` key, canonical name as display, `loinc_code` as the standard interoperability attribute; result values as numeric + comparator + text
- **Biomarker alias resolution** — where does lab-name → canonical-name mapping live? LOINC ([ADR-0030](adr/0030-biomarker-identity.md)) resolves electronic feeds directly and reduces this, but a name-based fallback is still needed for PDF/manual data; one concept can carry several LOINC codes ([ADR-0032](adr/0032-biomarker-loinc-cardinality.md)). (see [open-questions.md](open-questions.md))
- **Timezone handling** — resolved: UTC ground truth + `local_recorded` + `local_tz` (IANA) + `tz_inferred` flag on every timestamp. See [design-rationale.md](design-rationale.md) for the full convention.
- **Source provenance** — every row should carry an import batch reference; the `audit_log` table (see below) provides the full trail. Bulk-import inserts are audited at batch level ([ADR-0027](adr/0027-audit-trail-and-corrections.md)): one `import` audit row per (batch, table), with row-level "where did this come from" answered by each row's `import_batch_id`
- **Content class and authorship** — decided in principle ([provenance-and-derived-data.md](provenance-and-derived-data.md)): every datum is classed source / interpretation / derivation structurally (by table), authorship on interpretive rows is stamped from token identity ([ADR-0043](adr/0043-ai-authored-analyses-and-annotate-scope.md)), and interpretation or derivation is never written into source-data tables ([ADR-0044](adr/0044-derived-data-points.md), INV-6)
- **Longitudinal data correction** — decided ([ADR-0027](adr/0027-audit-trail-and-corrections.md)): `superseded_by` self-FK on every data table; value corrections supersede (never mutate in place), designated metadata repairs (timezone workflow) update in place with full audit; current state via per-table `*_current` views
- **Audit trail** — decided ([ADR-0027](adr/0027-audit-trail-and-corrections.md)): platform-wide append-only `audit_log`, written in the same transaction as every mutation, in the schema from migration 0001; per-row image audit for mutations of existing data, batch-level audit for bulk-import inserts; event sourcing was considered and rejected
- **Multiple reference range frameworks** — results need to be comparable against more than one set of ranges: the lab's own range (already stored per result row), longevity-optimized ranges (e.g. Function Health), and practitioner-specific optimal ranges (e.g. Attia, Brewer, Hyman). Requires a named framework table separate from per-result lab ranges. See [ADR-0005](adr/0005-reference-range-frameworks.md).

---

## Migration 0001 — Realized Schema

The data types above are realized as concrete tables by **migration 0001**, the initial schema, in [`src/healthspan/migrations/0001_initial_schema.sql`](../src/healthspan/migrations/0001_initial_schema.sql) (Phase 1 WI-3). Per [design-rationale.md](design-rationale.md), the migration scripts are the authoritative source of truth for database structure; this section records the design decisions that shaped 0001 and are not obvious from the DDL, not a second copy of the column list.

### Table inventory

- **Provenance** (FK targets, minimal in Phase 1; extended additively later): `import_batches` ([ADR-0004](adr/0004-data-ingestion-strategy.md) owns the full shape), `jobs` ([ADR-0012](adr/0012-job-abstraction.md) owns it).
- **Audit**: `audit_log` with its append-only `BEFORE UPDATE`/`BEFORE DELETE` immutability triggers ([ADR-0027](adr/0027-audit-trail-and-corrections.md)).
- **Catalog** (reference data): `biomarkers` ([ADR-0030](adr/0030-biomarker-identity.md)), `labs`, `range_frameworks` + `framework_ranges` ([ADR-0005](adr/0005-reference-range-frameworks.md)).
- **Lab results**: `lab_draws` (the draw-level container) + `lab_results` (the value rows, with the [ADR-0030](adr/0030-biomarker-identity.md) value-model CHECKs).
- **Measurements**: `body_composition`, `cgm_readings`, `wearable_daily`.
- **Clinical timeline**: `events`, `interventions`, `intervention_dose_history`.
- **Narrative** (FTS5-indexed `body`, per [ADR-0041](adr/0041-clinical-document-fts.md)): `clinical_documents`, `subjective_observations`, `analyses` — each with its external-content FTS virtual table and three sync triggers, plus the two-column link tables (`document_*`, `observation_*`, `analysis_*`).

### Conventions realized uniformly

- **STRICT** on every table ([ADR-0035](adr/0035-migration-execution-semantics.md)); dates/timestamps are `TEXT` (ISO-8601), booleans `INTEGER` 0/1 guarded by `CHECK (col IN (0,1))`.
- **Timestamp quadruple** (`*_utc` / `*_local_recorded` / `*_local_tz` / `*_tz_inferred`) on every clinically meaningful time (design-rationale.md).
- **Correction model**: the *content* tables (lab draws/results, measurements, events, interventions, dose history, documents, observations, analyses) each carry `superseded_by`, a companion `<table>_current` view, and a partial index `WHERE superseded_by IS NULL` ([ADR-0027](adr/0027-audit-trail-and-corrections.md)). Catalog, provenance, audit, and junction tables deliberately **do not** — corrections there are catalog edits or insert/delete link changes, not value supersession (link rows are audited insert/delete and never supersession-chained).
- **Provenance**: every content row carries a nullable `import_batch_id` (NULL for manual entry).

### Decisions the DDL leaves implicit

- **Lab identity lives on the draw.** `lab_results` references `lab_draws`, and the `lab_id` sits on `lab_draws`. This satisfies design-rationale's "lab is not optional on a result" transitively (a draw is one blood-draw event at one lab) without denormalizing `lab_id` onto every result row. `lab_draws` was introduced by [ADR-0027](adr/0027-audit-trail-and-corrections.md)'s lab-panel example.
- **Device-metric units are fixed in column names** (`weight_kg`, `glucose_mg_dl`, `resting_heart_rate` bpm, …) for `body_composition`/`cgm_readings`/`wearable_daily`. These are single-device fixed-unit metrics, not biomarker results, so the UCUM per-value + canonical-unit normalization model ([ADR-0031](adr/0031-units-and-ucum.md)) — which exists for cross-lab result comparison against framework ranges — does not apply to them.
- **Migration files ship as package data** under `src/healthspan/migrations/`, discovered at runtime via `importlib.resources` so an installed distribution can locate them — the decision to relocate from [ADR-0009](adr/0009-database-migration.md)'s repo-root `sql/migrations/` (while preserving its numbering/plain-SQL/`schema_version` convention) is recorded in [ADR-0048](adr/0048-migration-file-packaging.md), which extends ADR-0009.
- **Catalog display names are `UNIQUE`.** `biomarkers.canonical_name` and `labs.name` carry `UNIQUE` (as [ADR-0005](adr/0005-reference-range-frameworks.md) already specifies for `range_frameworks.name`). [ADR-0030](adr/0030-biomarker-identity.md) declares `canonical_name` a human display label and keeps the surrogate `id` as identity — the `UNIQUE` here is a data-integrity guard against two catalog rows claiming the same display name, not a second identity key (`loinc_code` remains the interoperability attribute, `id` the join target). `loinc_code` is separately `UNIQUE` per ADR-0030.
- **Audit is schema-only in Phase 1.** 0001 ships the `audit_log` table, its immutability triggers, the `superseded_by` columns, and every integrity constraint. The application-layer audit *capture* (writing `audit_log` rows inside each mutation transaction) and the mutation-matrix test are the Core Service write path's job and arrive in Phase 2 — the only Phase-1 database writers are `db migrate` and (WI-4) `db backup`.

### Deferred, with triggers

- `biomarker_aliases` → **Phase 3**, landing with the taxonomy + name-alias-fallback gate ([open-questions.md](open-questions.md)); additive migration.
- Levels **zones / activity / nutrition** tables → **Phase 7**; they are "Not yet designed" pending inspection of a real Levels export, so 0001 does not invent their shape.
- Clinical-document **original-binary storage** (content-addressed BLOBs) → **Phase 5** ([ADR-0034](adr/0034-clinical-document-storage.md)); 0001 records only the `source_file_hash` dedup key on `clinical_documents`. The FTS-indexed `body` text ships now because [ADR-0041](adr/0041-clinical-document-fts.md) requires the index in 0001.
- The lab's **reported LOINC per result** → deferred to [ADR-0032](adr/0032-biomarker-loinc-cardinality.md) (one concept, many codes); `lab_results` carries no `reported_loinc` column yet.
