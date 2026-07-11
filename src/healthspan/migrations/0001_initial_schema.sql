-- Migration 0001 — initial schema (Phase 1 WI-3).
--
-- The full designed schema from specs/data-model.md and its owning ADRs.
-- Runs inside the migration runner's transaction discipline (ADR-0035):
-- foreign_keys OFF, BEGIN IMMEDIATE, this file's statements, the
-- schema_version row, PRAGMA foreign_key_check, COMMIT. The runner owns the
-- schema_version bootstrap; this file assumes it exists and never guards its
-- own DDL with IF NOT EXISTS (ADR-0035: drift must fail loudly).
--
-- Conventions applied uniformly:
--   * Every table is declared STRICT (ADR-0035) — real per-column typing.
--     Dates/timestamps are TEXT (ISO-8601), booleans INTEGER 0/1.
--   * Clinically meaningful times carry the four-column quadruple
--     (design-rationale.md): *_utc (ground truth) + *_local_recorded
--     (immutable source value) + *_local_tz (IANA) + *_tz_inferred flag.
--   * Content tables (observations, events, documents, interventions,
--     analyses, journal) carry superseded_by (ADR-0027 correction model),
--     a *_current view, and a nullable import_batch_id provenance FK.
--     Catalog tables (biomarkers, labs, frameworks), provenance tables
--     (import_batches, jobs), and junction/link tables do not — corrections
--     to those are catalog edits or insert/delete link changes, not value
--     supersession.
--   * Audit *capture* (writing audit_log rows in the mutation transaction)
--     is the Core Service's write-path job and arrives in Phase 2. This
--     migration ships the audit_log table, its append-only immutability
--     triggers, the supersession columns/views, and every integrity
--     constraint — the schema the write path will honor.

--------------------------------------------------------------------------
-- Provenance tables (FK targets referenced throughout) — minimal in
-- Phase 1; ADR-0004 (import_batches) and ADR-0012 (jobs) own their full
-- shapes in later phases and extend these additively.
--------------------------------------------------------------------------

CREATE TABLE import_batches (
    id              INTEGER PRIMARY KEY,
    source          TEXT NOT NULL,          -- 'levels.glucose', 'manual', ...
    adapter_id      TEXT,                   -- import adapter identifier
    adapter_version TEXT,
    created_utc     TEXT NOT NULL,          -- when the batch was ingested
    note            TEXT
) STRICT;

CREATE TABLE jobs (
    id            INTEGER PRIMARY KEY,
    job_type      TEXT NOT NULL,
    status        TEXT NOT NULL,            -- enum owned by ADR-0012 (Phase 4)
    submitted_utc TEXT NOT NULL,
    updated_utc   TEXT
) STRICT;

--------------------------------------------------------------------------
-- Audit trail (ADR-0027) — one platform-wide append-only integrity log,
-- present from migration 0001 before any data table receives a row.
--------------------------------------------------------------------------

CREATE TABLE audit_log (
    id              INTEGER PRIMARY KEY,
    table_name      TEXT NOT NULL,
    row_id          INTEGER,                -- NULL for batch-level 'import' rows
    operation       TEXT NOT NULL,
    old_values      TEXT,                   -- JSON row image; NULL on insert/import
    new_values      TEXT,                   -- JSON row image; NULL on delete
    occurred_at_utc TEXT NOT NULL,          -- system event time, UTC only
    actor           TEXT,                   -- token name (ADR-0026); NULL pre-Phase-2
    import_batch_id INTEGER REFERENCES import_batches(id),
    job_id          INTEGER REFERENCES jobs(id),
    reason          TEXT,
    CHECK (operation IN ('insert', 'update', 'correct', 'delete', 'import'))
) STRICT;

CREATE INDEX ix_audit_log_row ON audit_log (table_name, row_id);
CREATE INDEX ix_audit_log_time ON audit_log (occurred_at_utc);

-- Append-only, enforced in the schema: even a bug in first-party code
-- cannot rewrite history (ADR-0027).
CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only (ADR-0027)');
END;
CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only (ADR-0027)');
END;

--------------------------------------------------------------------------
-- Biomarker identity and reference frameworks (catalog data — no
-- supersession; ADR-0030, ADR-0005).
--------------------------------------------------------------------------

CREATE TABLE biomarkers (
    id             INTEGER PRIMARY KEY,
    canonical_name TEXT NOT NULL UNIQUE,    -- human display label
    loinc_code     TEXT UNIQUE,             -- canonical LOINC; NULL when none exists
    canonical_unit TEXT,                    -- UCUM string (ADR-0031); NULL if unitless
    category       TEXT,                    -- lipids, metabolic, thyroid, ...
    description    TEXT
) STRICT;

CREATE TABLE labs (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,       -- 'Quest', 'LabCorp', 'Function Health (Quest)'
    description TEXT
) STRICT;

CREATE TABLE range_frameworks (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,       -- 'Lab Standard', 'Function Health', 'Attia', ...
    description TEXT,
    source_url  TEXT
) STRICT;

CREATE TABLE framework_ranges (
    id             INTEGER PRIMARY KEY,
    framework_id   INTEGER NOT NULL REFERENCES range_frameworks(id),
    biomarker_id   INTEGER NOT NULL REFERENCES biomarkers(id),
    range_low      REAL,
    range_high     REAL,
    unit           TEXT NOT NULL,           -- UCUM string; mandatory (ADR-0005/0031)
    range_text     TEXT,                    -- non-numeric targets or notes
    effective_date TEXT,                    -- ISO-8601 date; NULL = always current
    notes          TEXT,
    UNIQUE (framework_id, biomarker_id, effective_date),
    CHECK (range_low IS NULL OR range_high IS NULL OR range_low <= range_high),
    CHECK (range_low IS NOT NULL OR range_high IS NOT NULL OR range_text IS NOT NULL)
) STRICT;

-- The UNIQUE above treats NULLs as distinct, so a partial index makes the
-- "always current" (dateless) default provably singular per pair (ADR-0005).
CREATE UNIQUE INDEX ux_framework_ranges_default
    ON framework_ranges (framework_id, biomarker_id)
    WHERE effective_date IS NULL;

--------------------------------------------------------------------------
-- Lab results — draw-level container + per-result values. Lab identity
-- lives on the draw (a draw is one blood-draw event at one lab), which
-- satisfies design-rationale's "lab is not optional on a result"
-- transitively; the lab_draws table was introduced by ADR-0027's
-- lab-panel example. Reference ranges are stored per result row.
--------------------------------------------------------------------------

CREATE TABLE lab_draws (
    id                  INTEGER PRIMARY KEY,
    lab_id              INTEGER NOT NULL REFERENCES labs(id),
    draw_utc            TEXT NOT NULL,
    draw_local_recorded TEXT,
    draw_local_tz       TEXT,
    draw_tz_inferred    INTEGER NOT NULL DEFAULT 0,
    draw_context        TEXT,               -- 'annual physical', 'comprehensive panel', ...
    fasting             INTEGER,            -- 0/1, nullable (unknown)
    notes               TEXT,
    import_batch_id     INTEGER REFERENCES import_batches(id),
    superseded_by       INTEGER REFERENCES lab_draws(id),
    CHECK (draw_tz_inferred IN (0, 1)),
    CHECK (fasting IN (0, 1))
) STRICT;

CREATE TABLE lab_results (
    id              INTEGER PRIMARY KEY,
    lab_draw_id     INTEGER NOT NULL REFERENCES lab_draws(id),
    biomarker_id    INTEGER NOT NULL REFERENCES biomarkers(id),
    value_num       REAL,                   -- numeric magnitude; NULL if purely qualitative
    comparator      TEXT,                   -- '<','<=','>=','>'; NULL = exact value
    value_text      TEXT,                   -- qualitative result; NULL when numeric
    unit            TEXT,                   -- UCUM string as reported (ADR-0031)
    reference_low   REAL,                   -- the lab's own range (historical fact, per row)
    reference_high  REAL,
    reference_text  TEXT,                   -- non-numeric lab range / flag text
    notes           TEXT,
    import_batch_id INTEGER REFERENCES import_batches(id),
    superseded_by   INTEGER REFERENCES lab_results(id),
    -- Value model (ADR-0030) — the database-level analog of validation:
    CHECK (value_num IS NOT NULL OR value_text IS NOT NULL),
    CHECK (comparator IS NULL OR value_num IS NOT NULL),
    CHECK (comparator IN ('<', '<=', '>=', '>'))
) STRICT;

--------------------------------------------------------------------------
-- Body composition — one table, device-specific metrics NULL for devices
-- that do not produce them (design-rationale.md). Fixed metric units are
-- encoded in the column names (not biomarker results, so no framework
-- comparison / UCUM normalization applies).
--------------------------------------------------------------------------

CREATE TABLE body_composition (
    id                      INTEGER PRIMARY KEY,
    source                  TEXT NOT NULL,  -- 'InBody 120', 'InBody 580', ...
    measured_utc            TEXT NOT NULL,
    measured_local_recorded TEXT,
    measured_local_tz       TEXT,
    measured_tz_inferred    INTEGER NOT NULL DEFAULT 0,
    weight_kg               REAL,
    body_fat_pct            REAL,
    skeletal_muscle_mass_kg REAL,
    total_body_water_kg     REAL,
    -- device-specific (NULL for simpler devices):
    phase_angle_deg         REAL,
    ecw_tbw_ratio           REAL,
    intracellular_water_kg  REAL,
    extracellular_water_kg  REAL,
    visceral_fat_area_cm2   REAL,
    notes                   TEXT,
    import_batch_id         INTEGER REFERENCES import_batches(id),
    superseded_by           INTEGER REFERENCES body_composition(id),
    CHECK (measured_tz_inferred IN (0, 1))
) STRICT;

--------------------------------------------------------------------------
-- Continuous glucose (CGM) — high volume, separate from periodic labs,
-- indexed for timestamp range queries (design-rationale.md). A future
-- supersession exemption for re-imports is a deferred per-table decision
-- (ADR-0027 seam); the column ships now for uniformity.
--------------------------------------------------------------------------

CREATE TABLE cgm_readings (
    id                     INTEGER PRIMARY KEY,
    source                 TEXT NOT NULL,   -- 'levels.glucose', 'dexcom', ...
    reading_utc            TEXT NOT NULL,
    reading_local_recorded TEXT,
    reading_local_tz       TEXT,
    reading_tz_inferred    INTEGER NOT NULL DEFAULT 0,
    glucose_mg_dl          REAL NOT NULL,
    import_batch_id        INTEGER REFERENCES import_batches(id),
    superseded_by          INTEGER REFERENCES cgm_readings(id),
    CHECK (reading_tz_inferred IN (0, 1))
) STRICT;

--------------------------------------------------------------------------
-- Wearable daily aggregates — one wide row per (source, day); intraday
-- tables deferred (design-rationale.md).
--------------------------------------------------------------------------

CREATE TABLE wearable_daily (
    id                 INTEGER PRIMARY KEY,
    source             TEXT NOT NULL,       -- 'fitbit', ...
    day_utc            TEXT NOT NULL,       -- aggregate date, midnight UTC
    day_local_recorded TEXT,
    day_local_tz       TEXT,
    day_tz_inferred    INTEGER NOT NULL DEFAULT 0,
    steps              INTEGER,
    active_minutes     INTEGER,
    resting_heart_rate INTEGER,             -- bpm
    sleep_minutes      INTEGER,
    sleep_score        INTEGER,
    hr_zone_minutes    INTEGER,
    notes              TEXT,
    import_batch_id    INTEGER REFERENCES import_batches(id),
    superseded_by      INTEGER REFERENCES wearable_daily(id),
    CHECK (day_tz_inferred IN (0, 1))
) STRICT;

--------------------------------------------------------------------------
-- Clinical events and interventions (design-rationale.md, data-model.md).
-- Interventions store no "current dose" column — current dose is a
-- computed read over intervention_dose_history (ADR-0027, data-model.md).
--------------------------------------------------------------------------

CREATE TABLE events (
    id                   INTEGER PRIMARY KEY,
    event_utc            TEXT NOT NULL,
    event_local_recorded TEXT,
    event_local_tz       TEXT,
    event_tz_inferred    INTEGER NOT NULL DEFAULT 0,
    event_type           TEXT,              -- surgery, hospitalization, diagnosis, ...
    title                TEXT NOT NULL,
    description          TEXT,
    notes                TEXT,
    import_batch_id      INTEGER REFERENCES import_batches(id),
    superseded_by        INTEGER REFERENCES events(id),
    CHECK (event_tz_inferred IN (0, 1))
) STRICT;

CREATE TABLE interventions (
    id                   INTEGER PRIMARY KEY,
    name                 TEXT NOT NULL,     -- 'Testosterone cypionate', 'Atorvastatin', ...
    intervention_type    TEXT,              -- medication, supplement, therapy, ...
    route                TEXT,              -- IM, oral, topical, ...
    start_utc            TEXT,
    start_local_recorded TEXT,
    start_local_tz       TEXT,
    start_tz_inferred    INTEGER NOT NULL DEFAULT 0,
    end_utc              TEXT,              -- NULL = ongoing
    end_local_recorded   TEXT,
    end_local_tz         TEXT,
    end_tz_inferred      INTEGER NOT NULL DEFAULT 0,
    notes                TEXT,
    import_batch_id      INTEGER REFERENCES import_batches(id),
    superseded_by        INTEGER REFERENCES interventions(id),
    CHECK (start_tz_inferred IN (0, 1)),
    CHECK (end_tz_inferred IN (0, 1))
) STRICT;

CREATE TABLE intervention_dose_history (
    id                       INTEGER PRIMARY KEY,
    intervention_id          INTEGER NOT NULL REFERENCES interventions(id),
    effective_utc            TEXT NOT NULL,
    effective_local_recorded TEXT,
    effective_local_tz       TEXT,
    effective_tz_inferred    INTEGER NOT NULL DEFAULT 0,
    dose                     REAL,          -- e.g. 200
    unit                     TEXT,          -- UCUM string, e.g. 'mg/wk'
    change_type              TEXT NOT NULL, -- data-model.md enum
    authority_type           TEXT NOT NULL, -- data-model.md enum
    ordered_by               TEXT,          -- NULL when authority_type = 'self'
    reason                   TEXT,          -- data-model.md enum (nullable)
    notes                    TEXT,
    import_batch_id          INTEGER REFERENCES import_batches(id),
    superseded_by            INTEGER REFERENCES intervention_dose_history(id),
    CHECK (effective_tz_inferred IN (0, 1)),
    CHECK (change_type IN (
        'initiation', 'increase', 'decrease', 'hold', 'resumption', 'discontinuation'
    )),
    CHECK (authority_type IN (
        'prescribing_physician', 'supervising_clinician', 'self', 'protocol'
    )),
    CHECK (reason IS NULL OR reason IN (
        'scheduled_titration', 'lab_result', 'symptom_response', 'side_effect',
        'cost_or_availability', 'physician_directed', 'protocol_change', 'other'
    ))
) STRICT;

--------------------------------------------------------------------------
-- Clinical documents (ADR-0041 FTS, ADR-0034 originals). The body column
-- is the queryable surface; the FTS5 external-content index and its sync
-- triggers ship here so the index is never a retrofit. Original-binary
-- storage (content-addressed BLOBs) is deferred to Phase 5 (ADR-0034);
-- only its dedup key, source_file_hash, is recorded now.
--------------------------------------------------------------------------

CREATE TABLE clinical_documents (
    id                       INTEGER PRIMARY KEY,
    encounter_utc            TEXT NOT NULL,
    encounter_local_recorded TEXT,
    encounter_local_tz       TEXT,
    encounter_tz_inferred    INTEGER NOT NULL DEFAULT 0,
    provider_name            TEXT,
    provider_role            TEXT,          -- PCP, cardiologist, endocrinologist, ...
    practice_name            TEXT,
    document_type            TEXT,          -- data-model.md enum
    body                     TEXT,          -- full free text; FTS-indexed
    source_format            TEXT,          -- data-model.md enum
    source_file_hash         TEXT,          -- SHA-256 of original (ADR-0034 dedup key)
    author_type              TEXT,          -- clinician | patient
    notes                    TEXT,
    import_batch_id          INTEGER REFERENCES import_batches(id),
    superseded_by            INTEGER REFERENCES clinical_documents(id),
    CHECK (encounter_tz_inferred IN (0, 1)),
    CHECK (document_type IS NULL OR document_type IN (
        'visit_note', 'lab_interpretation', 'referral', 'discharge_summary',
        'care_plan', 'imaging_report', 'other'
    )),
    CHECK (source_format IS NULL OR source_format IN (
        'manual_entry', 'pdf_extracted', 'fhir_document', 'ccda'
    )),
    CHECK (author_type IS NULL OR author_type IN ('clinician', 'patient'))
) STRICT;

CREATE VIRTUAL TABLE clinical_documents_fts USING fts5(
    body,
    content='clinical_documents',
    content_rowid='id',
    tokenize='porter unicode61 remove_diacritics 2'
);

CREATE TRIGGER clinical_documents_fts_ai AFTER INSERT ON clinical_documents BEGIN
    INSERT INTO clinical_documents_fts(rowid, body) VALUES (new.id, new.body);
END;
CREATE TRIGGER clinical_documents_fts_ad AFTER DELETE ON clinical_documents BEGIN
    INSERT INTO clinical_documents_fts(clinical_documents_fts, rowid, body)
        VALUES ('delete', old.id, old.body);
END;
CREATE TRIGGER clinical_documents_fts_au AFTER UPDATE OF body ON clinical_documents BEGIN
    INSERT INTO clinical_documents_fts(clinical_documents_fts, rowid, body)
        VALUES ('delete', old.id, old.body);
    INSERT INTO clinical_documents_fts(rowid, body) VALUES (new.id, new.body);
END;

-- Two-column link tables (data-model.md): real FKs, audited insert/delete,
-- never supersession-chained.
CREATE TABLE document_lab_draws (
    document_id INTEGER NOT NULL REFERENCES clinical_documents(id),
    lab_draw_id INTEGER NOT NULL REFERENCES lab_draws(id),
    PRIMARY KEY (document_id, lab_draw_id)
) STRICT;
CREATE TABLE document_events (
    document_id INTEGER NOT NULL REFERENCES clinical_documents(id),
    event_id    INTEGER NOT NULL REFERENCES events(id),
    PRIMARY KEY (document_id, event_id)
) STRICT;
CREATE TABLE document_interventions (
    document_id     INTEGER NOT NULL REFERENCES clinical_documents(id),
    intervention_id INTEGER NOT NULL REFERENCES interventions(id),
    PRIMARY KEY (document_id, intervention_id)
) STRICT;

--------------------------------------------------------------------------
-- Subjective observations (journal) — source-class free text, FTS-indexed
-- via the ADR-0041 pattern; structured vocabulary deferred (data-model.md).
--------------------------------------------------------------------------

CREATE TABLE subjective_observations (
    id                      INTEGER PRIMARY KEY,
    observed_utc            TEXT NOT NULL,
    observed_local_recorded TEXT,
    observed_local_tz       TEXT,
    observed_tz_inferred    INTEGER NOT NULL DEFAULT 0,
    body                    TEXT,           -- free text; FTS-indexed
    notes                   TEXT,
    import_batch_id         INTEGER REFERENCES import_batches(id),
    superseded_by           INTEGER REFERENCES subjective_observations(id),
    CHECK (observed_tz_inferred IN (0, 1))
) STRICT;

CREATE VIRTUAL TABLE subjective_observations_fts USING fts5(
    body,
    content='subjective_observations',
    content_rowid='id',
    tokenize='porter unicode61 remove_diacritics 2'
);

CREATE TRIGGER subjective_observations_fts_ai
AFTER INSERT ON subjective_observations BEGIN
    INSERT INTO subjective_observations_fts(rowid, body) VALUES (new.id, new.body);
END;
CREATE TRIGGER subjective_observations_fts_ad
AFTER DELETE ON subjective_observations BEGIN
    INSERT INTO subjective_observations_fts(subjective_observations_fts, rowid, body)
        VALUES ('delete', old.id, old.body);
END;
CREATE TRIGGER subjective_observations_fts_au
AFTER UPDATE OF body ON subjective_observations BEGIN
    INSERT INTO subjective_observations_fts(subjective_observations_fts, rowid, body)
        VALUES ('delete', old.id, old.body);
    INSERT INTO subjective_observations_fts(rowid, body) VALUES (new.id, new.body);
END;

CREATE TABLE observation_interventions (
    observation_id  INTEGER NOT NULL REFERENCES subjective_observations(id),
    intervention_id INTEGER NOT NULL REFERENCES interventions(id),
    PRIMARY KEY (observation_id, intervention_id)
) STRICT;
CREATE TABLE observation_events (
    observation_id INTEGER NOT NULL REFERENCES subjective_observations(id),
    event_id       INTEGER NOT NULL REFERENCES events(id),
    PRIMARY KEY (observation_id, event_id)
) STRICT;

--------------------------------------------------------------------------
-- Analyses & interpretations (ADR-0043, ADR-0044) — interpretation-class,
-- self- or AI-authored in one table, FTS-indexed body. author_type is
-- stamped by the Core Service from token identity (Phase 2+); the author
-- guard on supersede/delete is application logic, not schema.
--------------------------------------------------------------------------

CREATE TABLE analyses (
    id                      INTEGER PRIMARY KEY,
    analysis_utc            TEXT NOT NULL,
    analysis_local_recorded TEXT,
    analysis_local_tz       TEXT,
    analysis_tz_inferred    INTEGER NOT NULL DEFAULT 0,
    author_type             TEXT NOT NULL,  -- 'self' | 'ai' (stamped by Core, ADR-0043)
    author_token            TEXT,           -- stamped token name
    tool_info               TEXT,           -- optional caller claim (model name/version)
    title                   TEXT,
    body                    TEXT,           -- narrative; FTS-indexed
    result_data             TEXT,           -- optional JSON attachment (ADR-0044 interim)
    import_batch_id         INTEGER REFERENCES import_batches(id),
    superseded_by           INTEGER REFERENCES analyses(id),
    CHECK (analysis_tz_inferred IN (0, 1)),
    CHECK (author_type IN ('self', 'ai'))
) STRICT;

CREATE VIRTUAL TABLE analyses_fts USING fts5(
    body,
    content='analyses',
    content_rowid='id',
    tokenize='porter unicode61 remove_diacritics 2'
);

CREATE TRIGGER analyses_fts_ai AFTER INSERT ON analyses BEGIN
    INSERT INTO analyses_fts(rowid, body) VALUES (new.id, new.body);
END;
CREATE TRIGGER analyses_fts_ad AFTER DELETE ON analyses BEGIN
    INSERT INTO analyses_fts(analyses_fts, rowid, body)
        VALUES ('delete', old.id, old.body);
END;
CREATE TRIGGER analyses_fts_au AFTER UPDATE OF body ON analyses BEGIN
    INSERT INTO analyses_fts(analyses_fts, rowid, body)
        VALUES ('delete', old.id, old.body);
    INSERT INTO analyses_fts(rowid, body) VALUES (new.id, new.body);
END;

CREATE TABLE analysis_lab_draws (
    analysis_id INTEGER NOT NULL REFERENCES analyses(id),
    lab_draw_id INTEGER NOT NULL REFERENCES lab_draws(id),
    PRIMARY KEY (analysis_id, lab_draw_id)
) STRICT;
CREATE TABLE analysis_documents (
    analysis_id INTEGER NOT NULL REFERENCES analyses(id),
    document_id INTEGER NOT NULL REFERENCES clinical_documents(id),
    PRIMARY KEY (analysis_id, document_id)
) STRICT;
CREATE TABLE analysis_interventions (
    analysis_id     INTEGER NOT NULL REFERENCES analyses(id),
    intervention_id INTEGER NOT NULL REFERENCES interventions(id),
    PRIMARY KEY (analysis_id, intervention_id)
) STRICT;
CREATE TABLE analysis_observations (
    analysis_id    INTEGER NOT NULL REFERENCES analyses(id),
    observation_id INTEGER NOT NULL REFERENCES subjective_observations(id),
    PRIMARY KEY (analysis_id, observation_id)
) STRICT;

--------------------------------------------------------------------------
-- Current-state views (ADR-0027): readers consume "WHERE superseded_by IS
-- NULL" by name. One per content table.
--------------------------------------------------------------------------

CREATE VIEW lab_draws_current AS
    SELECT * FROM lab_draws WHERE superseded_by IS NULL;
CREATE VIEW lab_results_current AS
    SELECT * FROM lab_results WHERE superseded_by IS NULL;
CREATE VIEW body_composition_current AS
    SELECT * FROM body_composition WHERE superseded_by IS NULL;
CREATE VIEW cgm_readings_current AS
    SELECT * FROM cgm_readings WHERE superseded_by IS NULL;
CREATE VIEW wearable_daily_current AS
    SELECT * FROM wearable_daily WHERE superseded_by IS NULL;
CREATE VIEW events_current AS
    SELECT * FROM events WHERE superseded_by IS NULL;
CREATE VIEW interventions_current AS
    SELECT * FROM interventions WHERE superseded_by IS NULL;
CREATE VIEW intervention_dose_history_current AS
    SELECT * FROM intervention_dose_history WHERE superseded_by IS NULL;
CREATE VIEW clinical_documents_current AS
    SELECT * FROM clinical_documents WHERE superseded_by IS NULL;
CREATE VIEW subjective_observations_current AS
    SELECT * FROM subjective_observations WHERE superseded_by IS NULL;
CREATE VIEW analyses_current AS
    SELECT * FROM analyses WHERE superseded_by IS NULL;

--------------------------------------------------------------------------
-- Partial indexes on current rows (ADR-0027): keep current-state queries
-- flat as supersession chains accumulate.
--------------------------------------------------------------------------

CREATE INDEX ix_lab_draws_current_lab ON lab_draws (lab_id, draw_utc)
    WHERE superseded_by IS NULL;
CREATE INDEX ix_lab_results_current_draw ON lab_results (lab_draw_id)
    WHERE superseded_by IS NULL;
CREATE INDEX ix_lab_results_current_biomarker ON lab_results (biomarker_id)
    WHERE superseded_by IS NULL;
CREATE INDEX ix_body_composition_current ON body_composition (source, measured_utc)
    WHERE superseded_by IS NULL;
CREATE INDEX ix_cgm_readings_current_time ON cgm_readings (reading_utc)
    WHERE superseded_by IS NULL;
CREATE INDEX ix_wearable_daily_current ON wearable_daily (source, day_utc)
    WHERE superseded_by IS NULL;
CREATE INDEX ix_events_current_time ON events (event_utc)
    WHERE superseded_by IS NULL;
CREATE INDEX ix_interventions_current_name ON interventions (name)
    WHERE superseded_by IS NULL;
CREATE INDEX ix_dose_history_current_intervention
    ON intervention_dose_history (intervention_id, effective_utc)
    WHERE superseded_by IS NULL;
CREATE INDEX ix_clinical_documents_current_time ON clinical_documents (encounter_utc)
    WHERE superseded_by IS NULL;
CREATE INDEX ix_observations_current_time ON subjective_observations (observed_utc)
    WHERE superseded_by IS NULL;
CREATE INDEX ix_analyses_current_time ON analyses (analysis_utc)
    WHERE superseded_by IS NULL;

-- Point-in-time framework range lookup by (framework, biomarker, date) is
-- already served by the index the UNIQUE(framework_id, biomarker_id,
-- effective_date) constraint creates (ADR-0005) — no separate index needed.
