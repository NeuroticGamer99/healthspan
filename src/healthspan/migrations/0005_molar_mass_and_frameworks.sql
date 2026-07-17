-- Migration 0005 — biomarker molar mass + reference-range framework seed
-- (Phase 3 WI-3). Implements ADR-0058; resolves the molar-mass persistence
-- question ADR-0056 §3 deferred (open-questions.md). Runs inside the runner's
-- foreign_keys=OFF / BEGIN IMMEDIATE / foreign_key_check / COMMIT discipline
-- (ADR-0035); no IF NOT EXISTS.

-- 1. Molar mass (ADR-0058 §4, extends ADR-0056). Grams per mole, for the
-- mass-concentration <-> substance-concentration conversions (mg/dL <-> mmol/L)
-- that units.convert cannot perform from the unit strings alone. NULL means
-- "not applicable, or not curated" — the honest default: a molar conversion
-- against a NULL molar_mass fails loud in units.convert
-- (MissingMolarContextError, ADR-0056 §3), never falls back to a scalar factor.
--
-- A plain ADD COLUMN, deliberately NOT a table rebuild: the legacy_alter_table
-- hazard migration 0004 documents applies to ALTER TABLE ... RENAME, which
-- silently repoints other tables' REFERENCES clauses. ADD COLUMN carries no
-- such hazard, so `biomarkers` is left in place and lab_results /
-- framework_ranges keep pointing at it.
--
-- The CHECK is the database-level analog of units.convert's own positivity
-- guard — the ADR-0030 enforcement pattern: a malformed value cannot exist even
-- if written through a future code path that skips validation. SQLite accepts a
-- CHECK on ADD COLUMN and enforces it on subsequent writes; existing rows take
-- NULL, which the constraint permits.
ALTER TABLE biomarkers
    ADD COLUMN molar_mass REAL CHECK (molar_mass IS NULL OR molar_mass > 0);

-- 2. Molar-mass seed values (ADR-0058 §4). Grams per mole, sourced from
-- PubChem (NIH/NLM) `MolecularWeight` lookups
-- (https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/<name>/property/MolecularWeight/TXT),
-- each cross-checked against an independently-sourced conventional clinical
-- conversion factor (mostly a peer-reviewed SI-unit conversion table hosted
-- on PMC, PMC11132569). Only biomarkers with a real, defensible molar mass
-- are updated here; canonical_name identifies each row, matching migration
-- 0004's sub-select convention. Reviewed and approved seed values — do not
-- alter without re-sourcing.

UPDATE biomarkers SET molar_mass = 386.7 WHERE canonical_name = 'Total Cholesterol';
UPDATE biomarkers SET molar_mass = 386.7 WHERE canonical_name = 'HDL Cholesterol';
UPDATE biomarkers SET molar_mass = 386.7 WHERE canonical_name = 'LDL Cholesterol';

-- Triglycerides: 885.4 is triolein's (glyceryl trioleate, PubChem CID
-- 5497163) molar mass, the conventional single-molecule proxy used to
-- convert this biomarker at all -- circulating triglyceride is a
-- heterogeneous mixture of triacylglycerols, not one compound, so no true
-- "triglycerides molecule" exists. This is the standard clinical-conversion
-- convention (matches the sourced factor mg/dL x 0.0113 = mmol/L), not a
-- claim that all circulating triglyceride is triolein.
UPDATE biomarkers SET molar_mass = 885.4 WHERE canonical_name = 'Triglycerides';

UPDATE biomarkers SET molar_mass = 180.16 WHERE canonical_name = 'Glucose';
UPDATE biomarkers SET molar_mass = 168.11 WHERE canonical_name = 'Uric Acid';
UPDATE biomarkers SET molar_mass = 584.7 WHERE canonical_name = 'Total Bilirubin';
UPDATE biomarkers SET molar_mass = 113.12 WHERE canonical_name = 'Creatinine';

-- BUN: 28.014 is the UREA-NITROGEN equivalent weight (2 x 14.007 g/mol),
-- NOT urea's own molecular weight (60.06 g/mol). "Blood urea nitrogen"
-- measures the mass of nitrogen in the sample, and each urea molecule
-- carries exactly 2 nitrogen atoms, so urea's own molar mass cancels out of
-- the arithmetic entirely: mmol/L urea = (mg N/dL x 10 dL/L) / 28.014 mg
-- N/mmol = mg N/dL x 0.357 -- the textbook conventional BUN factor, which
-- only reconciles against 28.014, not 60.06. A lab that reports "urea"
-- directly (common outside the US) rather than "urea nitrogen" needs
-- urea's own 60.06 g/mol and a DIFFERENT factor, 0.1665. Do not "correct"
-- this to 60.06 -- that would silently produce a value off by ~2.14x.
UPDATE biomarkers SET molar_mass = 28.014 WHERE canonical_name = 'BUN';

UPDATE biomarkers SET molar_mass = 40.08 WHERE canonical_name = 'Calcium';
UPDATE biomarkers SET molar_mass = 24.305 WHERE canonical_name = 'Magnesium';

-- Homocysteine: canonical_unit is already umol/L (SI), so no mass<->molar
-- bridging is ever needed for this biomarker's own conversions. Stored for
-- completeness / future use only, not because any current path requires it.
UPDATE biomarkers SET molar_mass = 135.19 WHERE canonical_name = 'Homocysteine';

UPDATE biomarkers SET molar_mass = 55.84 WHERE canonical_name = 'Iron';
UPDATE biomarkers SET molar_mass = 362.5 WHERE canonical_name = 'Cortisol';
UPDATE biomarkers SET molar_mass = 288.4 WHERE canonical_name = 'Testosterone Total';
UPDATE biomarkers SET molar_mass = 272.4 WHERE canonical_name = 'Estradiol';
UPDATE biomarkers SET molar_mass = 400.6 WHERE canonical_name = 'Vitamin D 25-OH';
UPDATE biomarkers SET molar_mass = 1355.4 WHERE canonical_name = 'Vitamin B12';

-- Folate: 441.4 is folic acid's molar mass -- the synthetic/fortification
-- form, and the assay-calibration convention -- NOT 5-MTHF
-- (5-methyltetrahydrofolate, MW 459.5), the form that actually predominates
-- in circulation. The sourced conventional clinical conversion factor
-- (ng/mL x 2.266 = nmol/L) reconciles only against 441.4, mirroring the
-- triglycerides/triolein convention above: use the assay-calibration
-- molecule, not the physiologically dominant one.
UPDATE biomarkers SET molar_mass = 441.4 WHERE canonical_name = 'Folate';

-- Albumin deliberately gets NO molar mass: it is a ~66 kDa protein, and
-- routine clinical practice converts g/dL <-> g/L by a flat x10, never
-- through a molar concentration. No clinically-used mass<->molar
-- conversion exists for this heterogeneous glycoprotein -- inventing one
-- would fail the "clinically meaningful" bar this seed is held to.

-- 3. range_frameworks / framework_ranges seed (ADR-0058 §5), deferred here from
-- migration 0004 (ADR-0055 §6). Only defensibly-sourced ranges are seeded; an
-- uncovered biomarker flags `no_range` rather than carrying a guessed target.
-- All rows below are the dateless "always current" default (`effective_date`
-- left NULL, per ADR-0005's point-in-time lookup rule) -- a future dated
-- revision of any framework adds dated rows alongside these, not a second
-- framework. No `Lab Standard` framework is seeded (ADR-0058 §5: the lab's
-- own per-result range already covers that ground). No Attia framework is
-- seeded: only podcast show notes exist for his numeric targets, which is
-- explicitly not a citable source.

INSERT INTO range_frameworks (name, description, source_url) VALUES
    ('nih_medlineplus_lipid_targets',
     'Desirable/optimal blood lipid levels for adults age 20+, as published '
     || 'by NIH''s MedlinePlus consumer health service.',
     'https://medlineplus.gov/cholesterollevelswhatyouneedtoknow.html'),
    -- Name deliberately carries no year (unlike the source proposal's
    -- `..._2026_glucose_a1c`): ADR-0005 already versions a framework's
    -- targets via `effective_date`, so baking the edition year into the
    -- NAME would make a future ADA revision spawn a second framework
    -- instead of a dated row on this one, silently breaking longitudinal
    -- comparison against earlier results. The 2026 edition is recorded in
    -- `description`/`source_url` instead; a future revision adds dated
    -- rows to this same framework.
    ('ada_standards_of_care',
     'American Diabetes Association normal (non-diabetic, non-prediabetic) '
     || 'thresholds for fasting plasma glucose and A1C from the Standards '
     || 'of Care in Diabetes-2026 diagnostic-criteria table, plus the '
     || 'Level 1 hypoglycemia alert value from the same Standards'' '
     || '"Glycemic Goals and Hypoglycemia" chapter.',
     'https://pmc.ncbi.nlm.nih.gov/articles/PMC12690183/'),
    ('aha_cdc_hscrp_risk_strata',
     'AHA/CDC hs-CRP cardiovascular risk stratification (low/intermediate/'
     || 'high); this seed encodes only the lowest-risk (''optimal'') band.',
     'https://pmc.ncbi.nlm.nih.gov/articles/PMC4669860/');

-- NIH MedlinePlus lipid targets. HDL's sex-specific LOW cutoff (40 mg/dL
-- men / 50 mg/dL women, per the same source page) is deliberately left
-- unencoded: `framework_ranges` has no sex dimension, and silently picking
-- one sex's cutoff over the other is not acceptable. Only the sex-neutral
-- ">= 60 mg/dL is best" target, stated identically for both sexes in the
-- source, is encoded below.
INSERT INTO framework_ranges (framework_id, biomarker_id, range_low, range_high, unit) VALUES
    ((SELECT id FROM range_frameworks WHERE name = 'nih_medlineplus_lipid_targets'),
     (SELECT id FROM biomarkers WHERE canonical_name = 'Total Cholesterol'),
     NULL, 200, 'mg/dL'),
    ((SELECT id FROM range_frameworks WHERE name = 'nih_medlineplus_lipid_targets'),
     (SELECT id FROM biomarkers WHERE canonical_name = 'LDL Cholesterol'),
     NULL, 100, 'mg/dL'),
    ((SELECT id FROM range_frameworks WHERE name = 'nih_medlineplus_lipid_targets'),
     (SELECT id FROM biomarkers WHERE canonical_name = 'HDL Cholesterol'),
     60, NULL, 'mg/dL'),
    ((SELECT id FROM range_frameworks WHERE name = 'nih_medlineplus_lipid_targets'),
     (SELECT id FROM biomarkers WHERE canonical_name = 'Triglycerides'),
     NULL, 150, 'mg/dL');

-- ADA standards of care: Hemoglobin A1c. `framework_ranges` bounds are
-- INCLUSIVE (ADR-0005 §"Two integrity CHECKs guard the range itself" /
-- ADR-0058 §3). The ADA source states prediabetes as "A1C 5.7-6.4%" --
-- 5.7 is the prediabetes FLOOR, not the normal ceiling. Encoding an
-- inclusive range_high of 5.7 would flag a prediabetic 5.7% result
-- `in_range`, a clinical falsehood. range_high = 5.6 is the largest value
-- still normal -- exactly how labs print this range -- so it correctly
-- excludes 5.7 while including everything below it. Do not "fix" this back
-- to 5.7.
INSERT INTO framework_ranges (framework_id, biomarker_id, range_low, range_high, unit) VALUES
    ((SELECT id FROM range_frameworks WHERE name = 'ada_standards_of_care'),
     (SELECT id FROM biomarkers WHERE canonical_name = 'Hemoglobin A1c'),
     NULL, 5.6, '%');

-- ADA standards of care: Glucose. Two sources, two chapters:
--   - CEILING (99 mg/dL): the same inclusive-bound reasoning as A1c above.
--     The ADA source states prediabetes (IFG) as "100 mg/dL to 125 mg/dL";
--     100 is the prediabetes FLOOR, so the inclusive range_high must be 99
--     (the largest value still normal), not 100 -- encoding 100 would flag
--     a prediabetic fasting glucose of 100 `in_range`. Source: Standards of
--     Care in Diabetes-2026, "2. Diagnosis and Classification of Diabetes",
--     Table 2.2 (https://pmc.ncbi.nlm.nih.gov/articles/PMC12690183/).
--   - FLOOR (70 mg/dL): a SAFETY requirement, not a stylistic choice. A
--     NULL floor would flag a fasting glucose of 40 mg/dL -- severe
--     hypoglycemia, a medical emergency -- as `in_range`. Glucose is a
--     two-sided marker (unlike genuinely one-sided "lower is better"
--     markers such as LDL/ApoB/hs-CRP/triglycerides, which correctly get
--     range_low = NULL elsewhere in this seed). The floor is the ADA's own
--     Level 1 hypoglycemia / glucose alert value: 70 mg/dL (3.9 mmol/L),
--     quoted verbatim from Table 6.4 ("Classification of Hypoglycemia"),
--     "Standards of Care in Diabetes-2024", "6. Glycemic Goals and
--     Hypoglycemia" chapter (fetched directly;
--     https://pmc.ncbi.nlm.nih.gov/articles/PMC10725808/): "Glucose
--     <70 mg/dL (<3.9 mmol/L) and >=54 mg/dL (>=3.0 mmol/L)" defines Level
--     1, and the same chapter separately states clinicians should "counsel
--     individuals with diabetes to treat hypoglycemia with fast-acting
--     carbohydrates at the hypoglycemia alert value of 70 mg/dL
--     (3.9 mmol/L) or less". This value is stable across Standards of Care
--     editions; the 2024 copy is the one directly fetched and quoted here,
--     distinct from the 2026 diagnostic-criteria source above.
INSERT INTO framework_ranges (framework_id, biomarker_id, range_low, range_high, unit) VALUES
    ((SELECT id FROM range_frameworks WHERE name = 'ada_standards_of_care'),
     (SELECT id FROM biomarkers WHERE canonical_name = 'Glucose'),
     70, 99, 'mg/dL');

-- AHA/CDC hs-CRP: only the lowest-risk ("optimal") band, <1.0 mg/L, per the
-- 1/1-3/>3 mg/L low/intermediate/high stratification. Sourced at one
-- remove: the primary AHA/CDC statement (Pearson et al., Circulation 2003)
-- returned HTTP 403 on every fetch attempt, so the quote below is from a
-- 2015 PMC-hosted peer-reviewed narrative review that restates and
-- correctly attributes the original AHA/CDC classification -- a future
-- reader wanting the primary citation will need institutional access to
-- Circulation 2003;107(3):499-511.
INSERT INTO framework_ranges (framework_id, biomarker_id, range_low, range_high, unit) VALUES
    ((SELECT id FROM range_frameworks WHERE name = 'aha_cdc_hscrp_risk_strata'),
     (SELECT id FROM biomarkers WHERE canonical_name = 'hs-CRP'),
     NULL, 1.0, 'mg/L');
