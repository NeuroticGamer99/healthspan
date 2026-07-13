-- Migration 0003 — import conflict-key indexes (Phase 2 WI-3).
--
-- The bulk import endpoint (ADR-0004) matches incoming rows against existing
-- ones by a defined natural key per importable table, not by primary key
-- (ADR-0052): the payload's `id`s are batch-local handles that wire intra-batch
-- foreign keys, never persistent identity. These partial UNIQUE indexes make
-- that natural key a real schema invariant over *current* rows, so the
-- conflict-resolution engine cannot ever create a duplicate current row and a
-- direct writer or a bug is caught by the constraint rather than corrupting the
-- store silently (ADR-0035 "drift must fail loudly").
--
-- Scoped `WHERE superseded_by IS NULL` so supersession chains coexist with
-- uniqueness: a superseded row drops out of the index, leaving its replacement
-- the single current occupant of the key. Only the two lab tables WI-3
-- registers as importable are constrained here; the remaining content tables
-- gain their keys with the Phase-7 adapters that feed them (ADR-0052).
--
-- Runs inside the runner's transaction discipline (ADR-0035): foreign_keys
-- OFF, BEGIN IMMEDIATE, these statements, the schema_version row,
-- PRAGMA foreign_key_check, COMMIT. No IF NOT EXISTS — drift fails loudly.

-- A lab draw is one blood-draw event at one lab: unique per (lab, instant).
CREATE UNIQUE INDEX ux_lab_draws_natural_key
    ON lab_draws (lab_id, draw_utc)
    WHERE superseded_by IS NULL;

-- One current result per biomarker within a draw.
CREATE UNIQUE INDEX ux_lab_results_natural_key
    ON lab_results (lab_draw_id, biomarker_id)
    WHERE superseded_by IS NULL;
