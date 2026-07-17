"""Migration 0004 schema integrity: categories, the reserved not_assigned
row, biomarker_aliases, and the biomarkers category_id rebuild
(ADR-0054/0055, Phase 3 WI-2).
"""

from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
import sqlcipher3

from healthspan import backup, db, keyparams, migrate
from healthspan.kdf import DbKey
from healthspan.units import is_valid_unit

KEY = DbKey(bytearray(range(1, 33)))

SEEDED_CATEGORY_NAMES = {
    "autoimmunity",
    "allergy",
    "body_composition",
    "electrolytes",
    "environmental_toxins",
    "heart",
    "hematology",
    "hormones",
    "immune",
    "inflammation",
    "kidney",
    "liver",
    "lipoproteins",
    "metabolic",
    "nutrients",
    "pancreas",
    "screening",
    "thyroid",
    "urine",
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


def _names(conn: sqlcipher3.Connection, sql: str) -> set[str]:
    return {str(row[0]) for row in conn.execute(sql).fetchall()}


# --------------------------------------------------------------------------
# categories: reserved row + seed vocabulary (ADR-0055 §1/§2/§6)
# --------------------------------------------------------------------------


def test_reserved_category_row_is_seeded(conn: sqlcipher3.Connection) -> None:
    row = conn.execute("SELECT id, name FROM categories WHERE id = 0").fetchone()
    assert row == (0, "not_assigned")


def test_nineteen_system_categories_are_seeded(conn: sqlcipher3.Connection) -> None:
    names = _names(conn, "SELECT name FROM categories WHERE id != 0")
    assert names == SEEDED_CATEGORY_NAMES
    assert _scalar(conn, "SELECT count(*) FROM categories") == 20


def test_deleting_reserved_category_is_forbidden(
    conn: sqlcipher3.Connection,
) -> None:
    with pytest.raises(sqlcipher3.IntegrityError, match="reserved"):
        conn.execute("DELETE FROM categories WHERE id = 0")
    assert _scalar(conn, "SELECT count(*) FROM categories WHERE id = 0") == 1


def test_deleting_a_non_reserved_category_succeeds(
    conn: sqlcipher3.Connection,
) -> None:
    conn.execute("DELETE FROM categories WHERE name = 'urine'")
    assert _scalar(conn, "SELECT count(*) FROM categories WHERE name = 'urine'") == 0


def test_reserved_category_is_renamable(conn: sqlcipher3.Connection) -> None:
    # The reserved row is identified by id, not name (ADR-0055 §2) — renaming
    # its display text is allowed; only deletion is forbidden.
    conn.execute("UPDATE categories SET name = 'uncategorized' WHERE id = 0")
    assert _scalar(conn, "SELECT name FROM categories WHERE id = 0") == "uncategorized"


# --------------------------------------------------------------------------
# biomarkers rebuild: category_id FK, not free-text category (ADR-0055 §1)
# --------------------------------------------------------------------------


def test_biomarkers_has_category_id_not_category(
    conn: sqlcipher3.Connection,
) -> None:
    columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(biomarkers)").fetchall()
    }
    assert "category_id" in columns
    assert "category" not in columns


def test_seeded_biomarkers_all_point_at_a_real_category(
    conn: sqlcipher3.Connection,
) -> None:
    total = _scalar(conn, "SELECT count(*) FROM biomarkers")
    assert total >= 50  # brief: ~50-70 starter biomarkers
    orphans = _scalar(
        conn,
        "SELECT count(*) FROM biomarkers b "
        "LEFT JOIN categories c ON c.id = b.category_id "
        "WHERE c.id IS NULL",
    )
    assert orphans == 0
    # None of the seed rows were left at the reserved default — every starter
    # biomarker was deliberately categorized.
    unassigned = _scalar(conn, "SELECT count(*) FROM biomarkers WHERE category_id = 0")
    assert unassigned == 0


def test_seeded_biomarkers_have_valid_ucum_units(
    conn: sqlcipher3.Connection,
) -> None:
    units = [
        str(row[0])
        for row in conn.execute(
            "SELECT canonical_unit FROM biomarkers WHERE canonical_unit IS NOT NULL"
        ).fetchall()
    ]
    assert units  # the seed data actually populated units
    for unit in units:
        assert is_valid_unit(unit), f"{unit!r} is not a valid UCUM string"


def test_seeded_biomarkers_leave_loinc_null(conn: sqlcipher3.Connection) -> None:
    # Fail-safe: never guess a LOINC code (ADR-0032 owns the electronic lane).
    non_null = _scalar(
        conn, "SELECT count(*) FROM biomarkers WHERE loinc_code IS NOT NULL"
    )
    assert non_null == 0


def test_labs_seeded(conn: sqlcipher3.Connection) -> None:
    names = _names(conn, "SELECT name FROM labs")
    assert names == {
        "Quest",
        "LabCorp",
        "Function Health (Quest)",
        "Function Health (LabCorp)",
    }


def test_biomarker_category_index_exists(conn: sqlcipher3.Connection) -> None:
    names = _names(
        conn,
        "SELECT name FROM sqlite_master WHERE type = 'index' "
        "AND tbl_name = 'biomarkers'",
    )
    assert "ix_biomarkers_category" in names


def test_biomarkers_are_strict(conn: sqlcipher3.Connection) -> None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='biomarkers'"
    ).fetchone()
    assert row is not None
    assert "STRICT" in str(row[0])


def test_pre_existing_tables_still_reference_biomarkers_not_biomarkers_old(
    conn: sqlcipher3.Connection,
) -> None:
    # Regression guard: SQLite's default (legacy_alter_table=OFF) `ALTER
    # TABLE ... RENAME TO` rewrites REFERENCES clauses in *other*
    # already-existing tables to follow the rename. Without migration 0004's
    # `PRAGMA legacy_alter_table = ON` bracket around the rename,
    # lab_results/framework_ranges (both from migration 0001, both predating
    # this migration) would be silently left pointing at the doomed
    # `biomarkers_old`, breaking the moment it is dropped.
    for table in ("lab_results", "framework_ranges"):
        targets = {
            str(row[2])  # PRAGMA foreign_key_list: table referenced
            for row in conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
        }
        assert "biomarkers" in targets
        assert "biomarkers_old" not in targets


def test_lab_result_insert_against_the_rebuilt_biomarkers_table_works(
    conn: sqlcipher3.Connection,
) -> None:
    # An end-to-end proof (not just schema inspection) that the FK really
    # resolves: a lab_results row referencing a biomarker must insert cleanly.
    conn.execute("INSERT INTO labs (name) VALUES ('TestLab')")
    lab_id = _scalar(conn, "SELECT id FROM labs WHERE name = 'TestLab'")
    conn.execute(
        "INSERT INTO lab_draws (lab_id, draw_utc) VALUES (?, '2026-01-01T00:00:00Z')",
        (lab_id,),
    )
    draw_id = _scalar(conn, "SELECT id FROM lab_draws WHERE lab_id = ?", (lab_id,))
    bm_id = _scalar(conn, "SELECT id FROM biomarkers WHERE canonical_name = 'Glucose'")
    conn.execute(
        "INSERT INTO lab_results (lab_draw_id, biomarker_id, value_num) "
        "VALUES (?, ?, 90.0)",
        (draw_id, bm_id),
    )
    assert _scalar(conn, "SELECT count(*) FROM lab_results") == 1


# --------------------------------------------------------------------------
# biomarker_aliases (ADR-0054 §1)
# --------------------------------------------------------------------------


def test_biomarker_aliases_table_exists_and_is_empty(
    conn: sqlcipher3.Connection,
) -> None:
    assert _scalar(conn, "SELECT count(*) FROM biomarker_aliases") == 0
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(biomarker_aliases)").fetchall()
    }
    assert columns == {
        "id",
        "biomarker_id",
        "alias",
        "alias_normalized",
        "source",
        "created_utc",
    }


def test_biomarker_aliases_index_exists(conn: sqlcipher3.Connection) -> None:
    names = _names(
        conn,
        "SELECT name FROM sqlite_master WHERE type = 'index' "
        "AND tbl_name = 'biomarker_aliases'",
    )
    assert "ix_biomarker_aliases_biomarker" in names


def test_biomarker_aliases_alias_normalized_is_unique(
    conn: sqlcipher3.Connection,
) -> None:
    bm_id = _scalar(conn, "SELECT id FROM biomarkers WHERE canonical_name = 'Glucose'")
    conn.execute(
        "INSERT INTO biomarker_aliases "
        "(biomarker_id, alias, alias_normalized, created_utc) "
        "VALUES (?, 'Glc', 'glc', '2026-07-15T00:00:00Z')",
        (bm_id,),
    )
    with pytest.raises(sqlcipher3.IntegrityError, match="UNIQUE"):
        conn.execute(
            "INSERT INTO biomarker_aliases "
            "(biomarker_id, alias, alias_normalized, created_utc) "
            "VALUES (?, 'GLC', 'glc', '2026-07-15T00:00:00Z')",
            (bm_id,),
        )


# --------------------------------------------------------------------------
# whole-database integrity
# --------------------------------------------------------------------------


def test_foreign_key_check_is_clean(conn: sqlcipher3.Connection) -> None:
    assert db.foreign_key_ok(conn)


def test_migrating_through_0004_reaches_schema_version_4(tmp_path: Path) -> None:
    # Scoped to the migrations shipped through 0004 (not the shared `conn`
    # fixture, which now applies every shipped migration including 0005+):
    # this asserts migration 0004's own ledger entry, independent of what
    # ships after it.
    path = tmp_path / "healthspan.db"
    db.provision(path, KEY)
    through_0004 = [m for m in migrate.discover_migrations() if m.version <= 4]
    migrate.migrate_database(path, KEY, through_0004)
    connection = db.connect(path, KEY)
    try:
        assert db.schema_version(connection) == 4
        ledger = connection.execute(
            "SELECT version, filename FROM schema_version ORDER BY version"
        ).fetchall()
        assert ledger[-1] == (4, "0004_categories_and_aliases.sql")
    finally:
        db.close(connection)


def test_backup_restore_round_trip_stays_consistent(tmp_path: Path) -> None:
    # Cheap end-to-end proof that the rebuilt biomarkers table and the new
    # catalog tables survive the native backup/verify pipeline (ADR-0038). A
    # fresh provision+migrate (not the shared `conn` fixture) so the source
    # connection can be closed before the file is backed up.
    source_path = tmp_path / "healthspan.db"
    db.provision(source_path, KEY)
    migrate.migrate_database(source_path, KEY)
    # create_verified_backup requires the key-parameter sidecar (normally
    # written by `healthspan init`); this test provisions the database
    # directly, so write a minimal one.
    keyparams.write_keyparams(
        keyparams.sidecar_path(source_path),
        keyparams.KeyParams(
            mode=keyparams.KeyMode.TWO_FACTOR, created_utc=keyparams.utc_now_iso()
        ),
    )
    backup_dir = tmp_path / "backups"
    published = backup.create_verified_backup(source_path, KEY, backup_dir)
    restored = db.connect(published.database, KEY)
    try:
        assert db.foreign_key_ok(restored)
        assert _scalar(restored, "SELECT count(*) FROM categories WHERE id = 0") == 1
    finally:
        db.close(restored)


# --------------------------------------------------------------------------
# runner semantics: incremental apply + idempotent re-run (ADR-0035)
# --------------------------------------------------------------------------


def test_runner_applies_0004_incrementally_over_a_0003_database(
    tmp_path: Path,
) -> None:
    path = tmp_path / "healthspan.db"
    db.provision(path, KEY)
    all_migrations = migrate.discover_migrations()
    through_0003 = [m for m in all_migrations if m.version <= 3]
    first = migrate.migrate_database(path, KEY, through_0003)
    assert first.applied == (1, 2, 3)
    through_0004 = [m for m in all_migrations if m.version <= 4]
    second = migrate.migrate_database(path, KEY, through_0004)
    assert second.applied == (4,)
    assert second.final_version == 4


def test_rerunning_the_migrator_is_a_no_op(tmp_path: Path) -> None:
    path = tmp_path / "healthspan.db"
    db.provision(path, KEY)
    migrate.migrate_database(path, KEY)
    again = migrate.migrate_database(path, KEY)
    assert again.applied == ()
    assert again.final_version == 5
