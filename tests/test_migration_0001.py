"""Migration 0001 schema integrity and its database-level guarantees.

STRICT typing, the ADR-0030 value-model CHECKs, the ADR-0005 framework
uniqueness/partial-index and range CHECKs, the ADR-0027 append-only
audit_log triggers, and the ADR-0041 FTS sync/rebuild/current-filter
behavior — the constraints migration 0001 exists to make unbypassable.
"""

from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
import sqlcipher3

from healthspan import db, migrate
from healthspan.kdf import DbKey

KEY = DbKey(bytearray(range(1, 33)))

EXPECTED_TABLES = {
    "import_batches",
    "jobs",
    "audit_log",
    "biomarkers",
    "labs",
    "range_frameworks",
    "framework_ranges",
    "lab_draws",
    "lab_results",
    "body_composition",
    "cgm_readings",
    "wearable_daily",
    "events",
    "interventions",
    "intervention_dose_history",
    "clinical_documents",
    "subjective_observations",
    "analyses",
    "document_lab_draws",
    "document_events",
    "document_interventions",
    "observation_interventions",
    "observation_events",
    "analysis_lab_draws",
    "analysis_documents",
    "analysis_interventions",
    "analysis_observations",
}

EXPECTED_VIEWS = {
    "lab_draws_current",
    "lab_results_current",
    "body_composition_current",
    "cgm_readings_current",
    "wearable_daily_current",
    "events_current",
    "interventions_current",
    "intervention_dose_history_current",
    "clinical_documents_current",
    "subjective_observations_current",
    "analyses_current",
}


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlcipher3.Connection]:
    path = tmp_path / "healthspan.db"
    db.provision(path, KEY)
    migrate.migrate_database(path, KEY)
    connection = db.connect(path, KEY)
    try:
        yield connection
    finally:
        db.close(connection)


def _scalar(conn: sqlcipher3.Connection, sql: str, params: Sequence[Any] = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    assert row is not None
    return row[0]


def _seed_lab_context(conn: sqlcipher3.Connection) -> tuple[Any, Any]:
    """A lab, a biomarker, and a draw — the FK context lab_results needs."""
    conn.execute("INSERT INTO labs (name) VALUES ('TestLab')")
    lab_id = _scalar(conn, "SELECT id FROM labs WHERE name = 'TestLab'")
    conn.execute("INSERT INTO biomarkers (canonical_name) VALUES ('TestMarker')")
    bm_id = _scalar(
        conn, "SELECT id FROM biomarkers WHERE canonical_name = 'TestMarker'"
    )
    conn.execute(
        "INSERT INTO lab_draws (lab_id, draw_utc) VALUES (?, '2026-01-01T00:00:00Z')",
        (lab_id,),
    )
    draw_id = _scalar(conn, "SELECT id FROM lab_draws WHERE lab_id = ?", (lab_id,))
    return draw_id, bm_id


# --- driver capabilities (ADR-0037/0041) ----------------------------------


def test_driver_has_fts5_and_cipher(conn: sqlcipher3.Connection) -> None:
    options = [r[0] for r in conn.execute("PRAGMA compile_options").fetchall()]
    assert any("FTS5" in o for o in options)  # the FTS index (ADR-0041) needs it
    assert conn.execute("PRAGMA cipher_version").fetchone() is not None  # not vanilla


# --- schema integrity -----------------------------------------------------


def test_all_expected_tables_exist(conn: sqlcipher3.Connection) -> None:
    names = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    # Superset, not equality: the live schema also carries schema_version, the
    # FTS5 shadow tables, and PRAGMA optimize's sqlite_stat* tables — none of
    # them content tables enumerated here.
    assert names >= EXPECTED_TABLES


def test_all_current_views_exist(conn: sqlcipher3.Connection) -> None:
    names = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'view'"
        ).fetchall()
    }
    assert names == EXPECTED_VIEWS


def test_audit_immutability_triggers_installed(conn: sqlcipher3.Connection) -> None:
    names = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        ).fetchall()
    }
    assert {"audit_log_no_update", "audit_log_no_delete"} <= names


# --- STRICT typing (ADR-0035) ---------------------------------------------


def test_strict_rejects_wrong_typed_value(conn: sqlcipher3.Connection) -> None:
    with pytest.raises(sqlcipher3.IntegrityError, match="cannot store"):
        conn.execute(
            "INSERT INTO cgm_readings (source, reading_utc, glucose_mg_dl) "
            "VALUES ('levels.glucose', '2026-01-01T00:00:00Z', 'not a number')"
        )


# --- value model (ADR-0030) -----------------------------------------------


def test_lab_result_requires_a_numeric_or_text_value(
    conn: sqlcipher3.Connection,
) -> None:
    draw_id, bm_id = _seed_lab_context(conn)
    with pytest.raises(sqlcipher3.IntegrityError, match="CHECK"):
        conn.execute(
            "INSERT INTO lab_results (lab_draw_id, biomarker_id) VALUES (?, ?)",
            (draw_id, bm_id),
        )


def test_comparator_requires_a_magnitude(conn: sqlcipher3.Connection) -> None:
    draw_id, bm_id = _seed_lab_context(conn)
    with pytest.raises(sqlcipher3.IntegrityError, match="CHECK"):
        conn.execute(
            "INSERT INTO lab_results "
            "(lab_draw_id, biomarker_id, comparator, value_text) "
            "VALUES (?, ?, '<', 'Reactive')",
            (draw_id, bm_id),
        )


def test_comparator_domain_is_closed(conn: sqlcipher3.Connection) -> None:
    draw_id, bm_id = _seed_lab_context(conn)
    with pytest.raises(sqlcipher3.IntegrityError, match="CHECK"):
        conn.execute(
            "INSERT INTO lab_results "
            "(lab_draw_id, biomarker_id, value_num, comparator) "
            "VALUES (?, ?, 1.0, '!=')",
            (draw_id, bm_id),
        )


def test_value_model_accepts_exact_censored_and_qualitative(
    conn: sqlcipher3.Connection,
) -> None:
    draw_id, bm_id = _seed_lab_context(conn)
    # Distinct biomarkers per row: the value-model CHECK is per-row, and the
    # (lab_draw_id, biomarker_id) natural-key index (migration 0003) forbids
    # two current results for one biomarker in a draw.
    conn.execute("INSERT INTO biomarkers (canonical_name) VALUES ('TestMarker2')")
    conn.execute("INSERT INTO biomarkers (canonical_name) VALUES ('TestMarker3')")
    bm_id2 = _scalar(
        conn, "SELECT id FROM biomarkers WHERE canonical_name = 'TestMarker2'"
    )
    bm_id3 = _scalar(
        conn, "SELECT id FROM biomarkers WHERE canonical_name = 'TestMarker3'"
    )
    conn.execute(
        "INSERT INTO lab_results (lab_draw_id, biomarker_id, value_num) "
        "VALUES (?, ?, 92.0)",
        (draw_id, bm_id),
    )  # exact
    conn.execute(
        "INSERT INTO lab_results (lab_draw_id, biomarker_id, value_num, comparator) "
        "VALUES (?, ?, 0.1, '<')",
        (draw_id, bm_id2),
    )  # below-detection: magnitude is the threshold, comparator marks it censored
    conn.execute(
        "INSERT INTO lab_results (lab_draw_id, biomarker_id, value_text) "
        "VALUES (?, ?, 'Not Detected')",
        (draw_id, bm_id3),
    )  # qualitative
    assert conn.execute("SELECT count(*) FROM lab_results").fetchone() == (3,)


# --- enum CHECKs authored for migration 0001 ------------------------------


def test_dose_history_enum_checks(conn: sqlcipher3.Connection) -> None:
    conn.execute("INSERT INTO interventions (name) VALUES ('Compound-Q (fictional)')")
    iv_id = _scalar(
        conn, "SELECT id FROM interventions WHERE name = 'Compound-Q (fictional)'"
    )
    conn.execute(
        "INSERT INTO intervention_dose_history "
        "(intervention_id, effective_utc, dose, unit, change_type, "
        "authority_type, reason) "
        "VALUES (?, '2026-01-01T00:00:00Z', 12.5, 'mg/d', 'initiation', "
        "'protocol', 'protocol_change')",
        (iv_id,),
    )  # a valid, fully-specified dose row (synthetic values)
    with pytest.raises(sqlcipher3.IntegrityError, match="CHECK"):
        conn.execute(
            "INSERT INTO intervention_dose_history "
            "(intervention_id, effective_utc, change_type, authority_type) "
            "VALUES (?, '2026-01-02T00:00:00Z', 'teleport', 'self')",
            (iv_id,),
        )


def test_clinical_document_type_domain(conn: sqlcipher3.Connection) -> None:
    with pytest.raises(sqlcipher3.IntegrityError, match="CHECK"):
        conn.execute(
            "INSERT INTO clinical_documents (encounter_utc, document_type) "
            "VALUES ('2026-01-01T00:00:00Z', 'fan_fiction')"
        )


# --- framework ranges (ADR-0005) ------------------------------------------


def _seed_framework(conn: sqlcipher3.Connection) -> tuple[Any, Any]:
    conn.execute("INSERT INTO range_frameworks (name) VALUES ('Attia')")
    fw_id = _scalar(conn, "SELECT id FROM range_frameworks WHERE name = 'Attia'")
    # A synthetic name distinct from migration 0004's starter catalog seed
    # (which already includes 'ApoB'), so this stays independent of that data.
    conn.execute("INSERT INTO biomarkers (canonical_name) VALUES ('TestApoBMarker')")
    bm_id = _scalar(
        conn, "SELECT id FROM biomarkers WHERE canonical_name = 'TestApoBMarker'"
    )
    return fw_id, bm_id


def test_framework_default_range_is_singular(conn: sqlcipher3.Connection) -> None:
    fw_id, bm_id = _seed_framework(conn)
    conn.execute(
        "INSERT INTO framework_ranges (framework_id, biomarker_id, range_high, unit) "
        "VALUES (?, ?, 60, 'mg/dL')",
        (fw_id, bm_id),
    )
    # A second dateless default for the same pair is rejected by the partial
    # unique index (the base UNIQUE would allow it — NULLs are distinct).
    with pytest.raises(sqlcipher3.IntegrityError, match="UNIQUE"):
        conn.execute(
            "INSERT INTO framework_ranges "
            "(framework_id, biomarker_id, range_high, unit) "
            "VALUES (?, ?, 70, 'mg/dL')",
            (fw_id, bm_id),
        )


def test_framework_dated_range_is_unique_per_date(
    conn: sqlcipher3.Connection,
) -> None:
    """The base UNIQUE(framework_id, biomarker_id, effective_date) — distinct
    from the partial default index — forbids two rows on the same date for a
    pair, while allowing a different date (the point-in-time series)."""
    fw_id, bm_id = _seed_framework(conn)
    conn.execute(
        "INSERT INTO framework_ranges "
        "(framework_id, biomarker_id, range_high, unit, effective_date) "
        "VALUES (?, ?, 60, 'mg/dL', '2024-01-01')",
        (fw_id, bm_id),
    )
    # Same non-null date for the same pair: the base UNIQUE fires (no NULLs
    # involved, so the partial default index is not what rejects this).
    with pytest.raises(sqlcipher3.IntegrityError, match="UNIQUE"):
        conn.execute(
            "INSERT INTO framework_ranges "
            "(framework_id, biomarker_id, range_high, unit, effective_date) "
            "VALUES (?, ?, 70, 'mg/dL', '2024-01-01')",
            (fw_id, bm_id),
        )
    # A different date for the same pair is allowed.
    conn.execute(
        "INSERT INTO framework_ranges "
        "(framework_id, biomarker_id, range_high, unit, effective_date) "
        "VALUES (?, ?, 65, 'mg/dL', '2025-01-01')",
        (fw_id, bm_id),
    )
    # Scoped to this test's own framework: migration 0005 seeds its own
    # framework_ranges rows into the same table, and this assertion is about
    # the two dated rows this test inserted, not the table's global count.
    assert conn.execute(
        "SELECT count(*) FROM framework_ranges WHERE framework_id = ?", (fw_id,)
    ).fetchone() == (2,)


def test_framework_rejects_inverted_range(conn: sqlcipher3.Connection) -> None:
    fw_id, bm_id = _seed_framework(conn)
    with pytest.raises(sqlcipher3.IntegrityError, match="CHECK"):
        conn.execute(
            "INSERT INTO framework_ranges "
            "(framework_id, biomarker_id, range_low, range_high, unit) "
            "VALUES (?, ?, 100, 10, 'mg/dL')",
            (fw_id, bm_id),
        )


def test_framework_rejects_contentless_row(conn: sqlcipher3.Connection) -> None:
    fw_id, bm_id = _seed_framework(conn)
    with pytest.raises(sqlcipher3.IntegrityError, match="CHECK"):
        conn.execute(
            "INSERT INTO framework_ranges (framework_id, biomarker_id, unit) "
            "VALUES (?, ?, 'mg/dL')",  # no low/high/text: a contentless target
            (fw_id, bm_id),
        )


# --- audit_log immutability (ADR-0027) ------------------------------------


def test_audit_log_is_append_only(conn: sqlcipher3.Connection) -> None:
    conn.execute(
        "INSERT INTO audit_log (table_name, operation, occurred_at_utc) "
        "VALUES ('lab_results', 'insert', '2026-01-01T00:00:00Z')"
    )
    with pytest.raises(sqlcipher3.IntegrityError, match="append-only"):
        conn.execute("UPDATE audit_log SET reason = 'tamper'")
    with pytest.raises(sqlcipher3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM audit_log")
    assert conn.execute("SELECT count(*) FROM audit_log").fetchone() == (1,)


# --- clinical-document FTS (ADR-0041) -------------------------------------


def test_fts_syncs_on_insert_update_delete(conn: sqlcipher3.Connection) -> None:
    conn.execute(
        "INSERT INTO clinical_documents (encounter_utc, body) "
        "VALUES ('2026-01-01T00:00:00Z', 'cardiology note about LDL trajectory')"
    )
    doc_id = _scalar(conn, "SELECT id FROM clinical_documents")
    assert conn.execute(
        "SELECT rowid FROM clinical_documents_fts "
        "WHERE clinical_documents_fts MATCH 'trajectory'"
    ).fetchall() == [(doc_id,)]
    # porter stemming favors recall: a query variant still matches.
    assert conn.execute(
        "SELECT count(*) FROM clinical_documents_fts "
        "WHERE clinical_documents_fts MATCH 'trajectories'"
    ).fetchone() == (1,)
    # A real body change re-indexes (AFTER UPDATE OF body).
    conn.execute(
        "UPDATE clinical_documents SET body = 'renal panel note' WHERE id = ?",
        (doc_id,),
    )
    assert conn.execute(
        "SELECT count(*) FROM clinical_documents_fts "
        "WHERE clinical_documents_fts MATCH 'trajectory'"
    ).fetchone() == (0,)
    assert conn.execute(
        "SELECT count(*) FROM clinical_documents_fts "
        "WHERE clinical_documents_fts MATCH 'renal'"
    ).fetchone() == (1,)
    conn.execute("DELETE FROM clinical_documents WHERE id = ?", (doc_id,))
    assert conn.execute(
        "SELECT count(*) FROM clinical_documents_fts "
        "WHERE clinical_documents_fts MATCH 'renal'"
    ).fetchone() == (0,)


def test_fts_supersession_indexes_both_but_current_filter_returns_one(
    conn: sqlcipher3.Connection,
) -> None:
    conn.execute(
        "INSERT INTO clinical_documents (encounter_utc, body) "
        "VALUES ('2026-01-01T00:00:00Z', 'note one alpha')"
    )
    first = _scalar(
        conn, "SELECT id FROM clinical_documents WHERE body = 'note one alpha'"
    )
    conn.execute(
        "INSERT INTO clinical_documents (encounter_utc, body) "
        "VALUES ('2026-01-02T00:00:00Z', 'note two beta')"
    )
    second = _scalar(
        conn, "SELECT id FROM clinical_documents WHERE body = 'note two beta'"
    )
    # A metadata-only correction (setting superseded_by; body unchanged) leaves
    # BOTH bodies in the index — the superseded row stays searchable. (The
    # AFTER UPDATE OF body trigger scoping additionally spares this update any
    # re-index churn, a performance property not observable through MATCH here.)
    conn.execute(
        "UPDATE clinical_documents SET superseded_by = ? WHERE id = ?", (second, first)
    )
    assert conn.execute(
        "SELECT count(*) FROM clinical_documents_fts "
        "WHERE clinical_documents_fts MATCH 'alpha'"
    ).fetchone() == (1,)
    # The current-filtered query (the ADR-0041 idiom) returns only the
    # superseding row.
    rows = conn.execute(
        "SELECT c.id FROM clinical_documents_fts f "
        "JOIN clinical_documents c ON c.id = f.rowid "
        "WHERE clinical_documents_fts MATCH 'note' AND c.superseded_by IS NULL"
    ).fetchall()
    assert rows == [(second,)]


def test_fts_rebuild_reproduces_the_index(conn: sqlcipher3.Connection) -> None:
    conn.execute(
        "INSERT INTO clinical_documents (encounter_utc, body) "
        "VALUES ('2026-01-01T00:00:00Z', 'rebuild me gamma')"
    )
    before = conn.execute(
        "SELECT count(*) FROM clinical_documents_fts "
        "WHERE clinical_documents_fts MATCH 'gamma'"
    ).fetchone()
    conn.execute(
        "INSERT INTO clinical_documents_fts(clinical_documents_fts) VALUES('rebuild')"
    )
    after = conn.execute(
        "SELECT count(*) FROM clinical_documents_fts "
        "WHERE clinical_documents_fts MATCH 'gamma'"
    ).fetchone()
    assert before == after == (1,)


def test_observation_fts_syncs(conn: sqlcipher3.Connection) -> None:
    conn.execute(
        "INSERT INTO subjective_observations (observed_utc, body) "
        "VALUES ('2026-01-01T00:00:00Z', 'felt strong today zeta')"
    )
    obs_id = _scalar(conn, "SELECT id FROM subjective_observations")
    assert conn.execute(
        "SELECT count(*) FROM subjective_observations_fts "
        "WHERE subjective_observations_fts MATCH 'zeta'"
    ).fetchone() == (1,)
    conn.execute("DELETE FROM subjective_observations WHERE id = ?", (obs_id,))
    assert conn.execute(
        "SELECT count(*) FROM subjective_observations_fts "
        "WHERE subjective_observations_fts MATCH 'zeta'"
    ).fetchone() == (0,)


def test_analyses_fts_syncs(conn: sqlcipher3.Connection) -> None:
    conn.execute(
        "INSERT INTO analyses (analysis_utc, author_type, body) "
        "VALUES ('2026-01-01T00:00:00Z', 'self', 'quarterly review omega')"
    )
    ana_id = _scalar(conn, "SELECT id FROM analyses")
    assert conn.execute(
        "SELECT count(*) FROM analyses_fts WHERE analyses_fts MATCH 'omega'"
    ).fetchone() == (1,)
    conn.execute("DELETE FROM analyses WHERE id = ?", (ana_id,))
    assert conn.execute(
        "SELECT count(*) FROM analyses_fts WHERE analyses_fts MATCH 'omega'"
    ).fetchone() == (0,)
