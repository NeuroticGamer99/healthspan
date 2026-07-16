-- Migration 0004 — biomarker categories + name aliases (Phase 3 WI-2).
-- Implements ADR-0055 (first-class categories, reserved not_assigned row) and
-- ADR-0054 (biomarker_aliases). Rebuilds `biomarkers` to replace the free-text
-- `category` column (0001 sketch) with a `category_id` FK. Runs inside the
-- runner's foreign_keys=OFF / BEGIN IMMEDIATE / foreign_key_check / COMMIT
-- discipline (ADR-0035); no IF NOT EXISTS.

-- 1. Categories catalog (ADR-0055 §1).
CREATE TABLE categories (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT
) STRICT;

-- Reserved default (ADR-0055 §2): id 0, seeded by the migration itself (not
-- owner-editable catalog data). Delete-guarded below; renamable display text.
INSERT INTO categories (id, name, description)
VALUES (0, 'not_assigned', 'Reserved: biomarker has not been assigned a category.');

-- Delete-guard for the reserved row (ADR-0055 §2; mirrors the audit_log
-- append-only triggers in 0001). This trigger is the single enforcement point
-- (ADR-0057 §1) — no application-level guard is layered on top — and it makes
-- removal of id 0 structurally impossible on any write path.
CREATE TRIGGER categories_reserved_no_delete BEFORE DELETE ON categories
WHEN OLD.id = 0 BEGIN
    SELECT RAISE(ABORT, 'category id 0 (not_assigned) is reserved (ADR-0055)');
END;

-- 2. Seed the 19 owner-editable system-axis categories (ADR-0055 §6). Ids are
-- assigned by autoincrement (1..19); the order mirrors ADR-0055 §6's listed
-- vocabulary (which is near-alphabetical but not strictly so — `allergy` after
-- `autoimmunity`, `lipoproteins` after `liver`), so the seed reads against the
-- ADR one-for-one. Nothing references a category by id — the biomarker seed
-- below resolves category_id by name — so the order carries no meaning beyond
-- that correspondence.
INSERT INTO categories (name) VALUES
    ('autoimmunity'), ('allergy'), ('body_composition'), ('electrolytes'),
    ('environmental_toxins'), ('heart'), ('hematology'), ('hormones'),
    ('immune'), ('inflammation'), ('kidney'), ('liver'), ('lipoproteins'),
    ('metabolic'), ('nutrients'), ('pancreas'), ('screening'), ('thyroid'),
    ('urine');

-- 3. Rebuild `biomarkers`: replace free-text `category` with category_id FK
-- (ADR-0055 §1). SQLite cannot ALTER a column type, so use the documented
-- 12-step table-redefinition. Written correctly for the non-empty case
-- (map old free-text category name -> category id, default 0) even though on a
-- fresh DB the table is empty.
--
-- `legacy_alter_table` is set ON for the RENAME only: with it OFF (SQLite's
-- own default), `ALTER TABLE ... RENAME TO` rewrites the REFERENCES clause of
-- every *other already-existing* table that names `biomarkers` (lab_results,
-- framework_ranges, migration 0001) to point at `biomarkers_old` instead —
-- silently, in the schema text — so those tables would be left referencing a
-- name this migration then drops. Restored to OFF immediately after so any
-- later ALTER in a future migration keeps the safe (non-legacy) behavior.
PRAGMA legacy_alter_table = ON;
ALTER TABLE biomarkers RENAME TO biomarkers_old;
PRAGMA legacy_alter_table = OFF;

CREATE TABLE biomarkers (
    id             INTEGER PRIMARY KEY,
    canonical_name TEXT NOT NULL UNIQUE,
    loinc_code     TEXT UNIQUE,
    canonical_unit TEXT,
    category_id    INTEGER NOT NULL DEFAULT 0 REFERENCES categories(id),
    description    TEXT
) STRICT;

INSERT INTO biomarkers (id, canonical_name, loinc_code, canonical_unit, category_id, description)
SELECT b.id, b.canonical_name, b.loinc_code, b.canonical_unit,
       COALESCE((SELECT c.id FROM categories c WHERE c.name = b.category), 0),
       b.description
FROM biomarkers_old b;

DROP TABLE biomarkers_old;

CREATE INDEX ix_biomarkers_category ON biomarkers (category_id);

-- 4. biomarker_aliases (ADR-0054 §1). Catalog data: no supersession columns.
-- Created after the rebuild above so its FK resolves against the final
-- `biomarkers` table, not the transient `biomarkers_old`.
CREATE TABLE biomarker_aliases (
    id               INTEGER PRIMARY KEY,
    biomarker_id     INTEGER NOT NULL REFERENCES biomarkers(id),
    alias            TEXT NOT NULL,
    alias_normalized TEXT NOT NULL UNIQUE,
    source           TEXT,
    created_utc      TEXT NOT NULL
) STRICT;
CREATE INDEX ix_biomarker_aliases_biomarker ON biomarker_aliases (biomarker_id);

-- 5. Seed the starter biomarker catalog + common labs (ADR-0055 §6). Generic
-- reference data only — no personal health values. canonical_unit strings are
-- UCUM (ADR-0031), verified against healthspan.units.is_valid_unit. loinc_code
-- is left NULL throughout: guessing a code is worse than omitting one
-- (fail-safe; ADR-0032 owns the electronic-feed LOINC lane).

INSERT INTO labs (name) VALUES
    ('Quest'), ('LabCorp'), ('Function Health (Quest)'), ('Function Health (LabCorp)');

-- Lipoproteins
INSERT INTO biomarkers (canonical_name, canonical_unit, category_id) VALUES
    ('Total Cholesterol', 'mg/dL', (SELECT id FROM categories WHERE name = 'lipoproteins')),
    ('HDL Cholesterol',   'mg/dL', (SELECT id FROM categories WHERE name = 'lipoproteins')),
    ('LDL Cholesterol',   'mg/dL', (SELECT id FROM categories WHERE name = 'lipoproteins')),
    ('Triglycerides',     'mg/dL', (SELECT id FROM categories WHERE name = 'lipoproteins')),
    ('ApoB',              'mg/dL', (SELECT id FROM categories WHERE name = 'lipoproteins')),
    ('Lp(a)',             'nmol/L', (SELECT id FROM categories WHERE name = 'lipoproteins'));

-- Metabolic (HOMA-IR is derived from Glucose + Insulin, so it is skipped here
-- per ADR-0044 — derived data, not a catalog biomarker).
INSERT INTO biomarkers (canonical_name, canonical_unit, category_id) VALUES
    ('Glucose',          'mg/dL',    (SELECT id FROM categories WHERE name = 'metabolic')),
    ('Hemoglobin A1c',   '%',        (SELECT id FROM categories WHERE name = 'metabolic')),
    ('Fasting Insulin',  'u[iU]/mL', (SELECT id FROM categories WHERE name = 'metabolic')),
    ('Uric Acid',        'mg/dL',    (SELECT id FROM categories WHERE name = 'metabolic'));

-- Liver
INSERT INTO biomarkers (canonical_name, canonical_unit, category_id) VALUES
    ('ALT',              'U/L',   (SELECT id FROM categories WHERE name = 'liver')),
    ('AST',              'U/L',   (SELECT id FROM categories WHERE name = 'liver')),
    ('ALP',              'U/L',   (SELECT id FROM categories WHERE name = 'liver')),
    ('Total Bilirubin',  'mg/dL', (SELECT id FROM categories WHERE name = 'liver')),
    ('Albumin',          'g/dL',  (SELECT id FROM categories WHERE name = 'liver')),
    ('GGT',              'U/L',   (SELECT id FROM categories WHERE name = 'liver'));

-- Kidney
INSERT INTO biomarkers (canonical_name, canonical_unit, category_id) VALUES
    ('Creatinine',   'mg/dL',              (SELECT id FROM categories WHERE name = 'kidney')),
    ('BUN',          'mg/dL',              (SELECT id FROM categories WHERE name = 'kidney')),
    ('eGFR',         'mL/min/{1.73_m2}',   (SELECT id FROM categories WHERE name = 'kidney')),
    ('Cystatin C',   'mg/L',               (SELECT id FROM categories WHERE name = 'kidney'));

-- Electrolytes
INSERT INTO biomarkers (canonical_name, canonical_unit, category_id) VALUES
    ('Sodium',             'mmol/L', (SELECT id FROM categories WHERE name = 'electrolytes')),
    ('Potassium',          'mmol/L', (SELECT id FROM categories WHERE name = 'electrolytes')),
    ('Chloride',           'mmol/L', (SELECT id FROM categories WHERE name = 'electrolytes')),
    ('CO2 (Bicarbonate)',  'mmol/L', (SELECT id FROM categories WHERE name = 'electrolytes')),
    ('Calcium',            'mg/dL',  (SELECT id FROM categories WHERE name = 'electrolytes')),
    ('Magnesium',          'mg/dL',  (SELECT id FROM categories WHERE name = 'electrolytes'));

-- Thyroid (TPO Antibodies is an autoimmune marker, not a thyroid function
-- test, so it is seeded under autoimmunity per ADR-0055 §6 guidance).
INSERT INTO biomarkers (canonical_name, canonical_unit, category_id) VALUES
    ('TSH',              'm[iU]/L', (SELECT id FROM categories WHERE name = 'thyroid')),
    ('Free T4',          'ng/dL',   (SELECT id FROM categories WHERE name = 'thyroid')),
    ('Free T3',          'pg/mL',   (SELECT id FROM categories WHERE name = 'thyroid')),
    ('TPO Antibodies',   '[iU]/mL', (SELECT id FROM categories WHERE name = 'autoimmunity'));

-- Hematology (CBC)
INSERT INTO biomarkers (canonical_name, canonical_unit, category_id) VALUES
    ('WBC',           '10*3/uL', (SELECT id FROM categories WHERE name = 'hematology')),
    ('RBC',           '10*6/uL', (SELECT id FROM categories WHERE name = 'hematology')),
    ('Hemoglobin',    'g/dL',    (SELECT id FROM categories WHERE name = 'hematology')),
    ('Hematocrit',    '%',       (SELECT id FROM categories WHERE name = 'hematology')),
    ('MCV',           'fL',      (SELECT id FROM categories WHERE name = 'hematology')),
    ('MCH',           'pg',      (SELECT id FROM categories WHERE name = 'hematology')),
    ('MCHC',          'g/dL',    (SELECT id FROM categories WHERE name = 'hematology')),
    ('RDW',           '%',       (SELECT id FROM categories WHERE name = 'hematology')),
    ('Platelets',     '10*3/uL', (SELECT id FROM categories WHERE name = 'hematology')),
    ('Neutrophils',   '%',       (SELECT id FROM categories WHERE name = 'hematology')),
    ('Lymphocytes',   '%',       (SELECT id FROM categories WHERE name = 'hematology'));

-- Inflammation (Homocysteine -> inflammation and Ferritin -> nutrients are
-- the explicit ADR-0055 §6 seeding calls, not the more common lay grouping).
INSERT INTO biomarkers (canonical_name, canonical_unit, category_id) VALUES
    ('hs-CRP',         'mg/L',   (SELECT id FROM categories WHERE name = 'inflammation')),
    ('ESR',            'mm/h',   (SELECT id FROM categories WHERE name = 'inflammation')),
    ('Homocysteine',   'umol/L', (SELECT id FROM categories WHERE name = 'inflammation')),
    ('Fibrinogen',     'mg/dL',  (SELECT id FROM categories WHERE name = 'inflammation'));

-- Hormones
INSERT INTO biomarkers (canonical_name, canonical_unit, category_id) VALUES
    ('Testosterone Total',  'ng/dL',    (SELECT id FROM categories WHERE name = 'hormones')),
    ('Testosterone Free',   'pg/mL',    (SELECT id FROM categories WHERE name = 'hormones')),
    ('SHBG',                'nmol/L',   (SELECT id FROM categories WHERE name = 'hormones')),
    ('Estradiol',           'pg/mL',    (SELECT id FROM categories WHERE name = 'hormones')),
    ('DHEA-S',              'ug/dL',    (SELECT id FROM categories WHERE name = 'hormones')),
    ('Cortisol',            'ug/dL',    (SELECT id FROM categories WHERE name = 'hormones')),
    ('IGF-1',               'ng/mL',    (SELECT id FROM categories WHERE name = 'hormones')),
    ('FSH',                 'm[iU]/mL', (SELECT id FROM categories WHERE name = 'hormones')),
    ('LH',                  'm[iU]/mL', (SELECT id FROM categories WHERE name = 'hormones'));

-- Nutrients (Ferritin lives here per ADR-0055 §6, not under inflammation).
INSERT INTO biomarkers (canonical_name, canonical_unit, category_id) VALUES
    ('Vitamin D 25-OH',   'ng/mL', (SELECT id FROM categories WHERE name = 'nutrients')),
    ('Vitamin B12',       'pg/mL', (SELECT id FROM categories WHERE name = 'nutrients')),
    ('Folate',            'ng/mL', (SELECT id FROM categories WHERE name = 'nutrients')),
    ('Ferritin',          'ng/mL', (SELECT id FROM categories WHERE name = 'nutrients')),
    ('Iron',              'ug/dL', (SELECT id FROM categories WHERE name = 'nutrients')),
    ('TIBC',              'ug/dL', (SELECT id FROM categories WHERE name = 'nutrients')),
    ('Magnesium RBC',     'mg/dL', (SELECT id FROM categories WHERE name = 'nutrients'));

-- Pancreas
INSERT INTO biomarkers (canonical_name, canonical_unit, category_id) VALUES
    ('Lipase',   'U/L', (SELECT id FROM categories WHERE name = 'pancreas')),
    ('Amylase',  'U/L', (SELECT id FROM categories WHERE name = 'pancreas'));

-- Screening
INSERT INTO biomarkers (canonical_name, canonical_unit, category_id) VALUES
    ('PSA', 'ng/mL', (SELECT id FROM categories WHERE name = 'screening'));

-- range_frameworks / framework_ranges seeding is deferred to WI-3 (ADR-0055
-- §6); their read endpoints ship empty until then.
