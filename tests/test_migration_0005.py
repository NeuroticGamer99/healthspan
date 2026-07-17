"""Migration 0005 schema integrity: biomarkers.molar_mass, and the
molar-mass / range-framework seed data (ADR-0058 §4/§5, extends ADR-0056,
Phase 3 WI-3).
"""

from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
import sqlcipher3

from healthspan import db, migrate
from healthspan.kdf import DbKey
from healthspan.units import UnitError, convert, is_valid_unit

KEY = DbKey(bytearray(range(1, 33)))

# The approved molar-mass seed (research proposal, reviewed and approved
# as-is — see the migration's own section-2 comments for sourcing/subtlety
# notes on BUN, Triglycerides, Folate, and Homocysteine).
SEEDED_MOLAR_MASSES = {
    "Total Cholesterol": 386.7,
    "HDL Cholesterol": 386.7,
    "LDL Cholesterol": 386.7,
    "Triglycerides": 885.4,
    "Glucose": 180.16,
    "Uric Acid": 168.11,
    "Total Bilirubin": 584.7,
    "Creatinine": 113.12,
    "BUN": 28.014,
    "Calcium": 40.08,
    "Magnesium": 24.305,
    "Homocysteine": 135.19,
    "Iron": 55.84,
    "Cortisol": 362.5,
    "Testosterone Total": 288.4,
    "Estradiol": 272.4,
    "Vitamin D 25-OH": 400.6,
    "Vitamin B12": 1355.4,
    "Folate": 441.4,
}

# The approved framework seed, post-fix (ADR-0058 §5): dropped-year ADA
# name, inclusive-bound-corrected A1c/Glucose ceilings, and the sourced
# Level 1 hypoglycemia floor on Glucose.
SEEDED_FRAMEWORKS = {
    "nih_medlineplus_lipid_targets": {
        "Total Cholesterol": (None, 200, "mg/dL"),
        "LDL Cholesterol": (None, 100, "mg/dL"),
        "HDL Cholesterol": (60, None, "mg/dL"),
        "Triglycerides": (None, 150, "mg/dL"),
    },
    "ada_standards_of_care": {
        "Glucose": (70, 99, "mg/dL"),
        "Hemoglobin A1c": (None, 5.6, "%"),
    },
    "aha_cdc_hscrp_risk_strata": {
        "hs-CRP": (None, 1.0, "mg/L"),
    },
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


# --------------------------------------------------------------------------
# schema version
# --------------------------------------------------------------------------


def test_fresh_database_reaches_schema_version_5(conn: sqlcipher3.Connection) -> None:
    assert db.schema_version(conn) == 5
    ledger = conn.execute(
        "SELECT version, filename FROM schema_version ORDER BY version"
    ).fetchall()
    assert ledger[-1] == (5, "0005_molar_mass_and_frameworks.sql")


# --------------------------------------------------------------------------
# biomarkers.molar_mass: column shape (ADR-0058 §4)
# --------------------------------------------------------------------------


def test_molar_mass_column_is_real_and_nullable(conn: sqlcipher3.Connection) -> None:
    columns = {
        str(row[1]): row
        for row in conn.execute("PRAGMA table_info(biomarkers)").fetchall()
    }
    assert "molar_mass" in columns
    _cid, _name, col_type, notnull, default, _pk = columns["molar_mass"]
    assert str(col_type) == "REAL"
    assert notnull == 0
    assert default is None


def test_molar_mass_accepts_null(conn: sqlcipher3.Connection) -> None:
    conn.execute(
        "INSERT INTO biomarkers (canonical_name, category_id, molar_mass) "
        "VALUES ('Test Marker (null molar mass)', 0, NULL)"
    )
    assert (
        _scalar(
            conn,
            "SELECT molar_mass FROM biomarkers WHERE canonical_name = ?",
            ("Test Marker (null molar mass)",),
        )
        is None
    )


# --------------------------------------------------------------------------
# biomarkers.molar_mass: CHECK (molar_mass IS NULL OR molar_mass > 0)
# --------------------------------------------------------------------------


def test_molar_mass_check_rejects_negative(conn: sqlcipher3.Connection) -> None:
    with pytest.raises(sqlcipher3.IntegrityError, match="CHECK"):
        conn.execute(
            "INSERT INTO biomarkers (canonical_name, category_id, molar_mass) "
            "VALUES ('Test Marker (negative molar mass)', 0, -1)"
        )


def test_molar_mass_check_rejects_zero(conn: sqlcipher3.Connection) -> None:
    with pytest.raises(sqlcipher3.IntegrityError, match="CHECK"):
        conn.execute(
            "INSERT INTO biomarkers (canonical_name, category_id, molar_mass) "
            "VALUES ('Test Marker (zero molar mass)', 0, 0)"
        )


def test_molar_mass_check_accepts_positive(conn: sqlcipher3.Connection) -> None:
    conn.execute(
        "INSERT INTO biomarkers (canonical_name, category_id, molar_mass) "
        "VALUES ('Test Marker (positive molar mass)', 0, 180.16)"
    )
    assert (
        _scalar(
            conn,
            "SELECT molar_mass FROM biomarkers WHERE canonical_name = ?",
            ("Test Marker (positive molar mass)",),
        )
        == 180.16
    )


# --------------------------------------------------------------------------
# ADD COLUMN, not a rebuild: pre-existing columns, WI-2 seed data, and the
# lab_results/framework_ranges FKs all survive the ALTER (migration comment,
# ADR-0058 §4).
# --------------------------------------------------------------------------


def test_biomarkers_pre_existing_columns_survive_the_alter(
    conn: sqlcipher3.Connection,
) -> None:
    columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(biomarkers)").fetchall()
    }
    assert columns == {
        "id",
        "canonical_name",
        "loinc_code",
        "canonical_unit",
        "category_id",
        "description",
        "molar_mass",
    }


def test_wi2_seed_biomarkers_survive_the_alter(conn: sqlcipher3.Connection) -> None:
    total = _scalar(conn, "SELECT count(*) FROM biomarkers")
    assert total >= 50  # the WI-2 starter catalog (migration 0004) is ~64 rows
    glucose = _scalar(
        conn, "SELECT count(*) FROM biomarkers WHERE canonical_name = 'Glucose'"
    )
    assert glucose == 1


def test_foreign_key_check_is_clean(conn: sqlcipher3.Connection) -> None:
    # The strongest proof this was ADD COLUMN, not a table rebuild: a rebuild
    # gone wrong (e.g. the migration 0004 legacy_alter_table hazard) leaves
    # lab_results/framework_ranges pointing at a stale table identity.
    assert db.foreign_key_ok(conn)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_lab_result_insert_against_the_altered_biomarkers_table_works(
    conn: sqlcipher3.Connection,
) -> None:
    # End-to-end proof (not just schema inspection) that lab_results' FK into
    # biomarkers still resolves after the ADD COLUMN.
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
# runner semantics: incremental apply + idempotent re-run (ADR-0035)
# --------------------------------------------------------------------------


def test_runner_applies_0005_incrementally_over_a_0004_database(
    tmp_path: Path,
) -> None:
    path = tmp_path / "healthspan.db"
    db.provision(path, KEY)
    all_migrations = migrate.discover_migrations()
    through_0004 = [m for m in all_migrations if m.version <= 4]
    first = migrate.migrate_database(path, KEY, through_0004)
    assert first.applied == (1, 2, 3, 4)
    # Scoped to <= 5, not the full corpus: this test is about 0005 applying
    # incrementally over a 0004 database, so it must keep asserting exactly
    # that once 0006 ships (an unscoped corpus would silently drift to (5, 6)).
    through_0005 = [m for m in all_migrations if m.version <= 5]
    second = migrate.migrate_database(path, KEY, through_0005)
    assert second.applied == (5,)
    assert second.final_version == 5


def test_rerunning_the_migrator_through_0005_is_a_no_op(tmp_path: Path) -> None:
    path = tmp_path / "healthspan.db"
    db.provision(path, KEY)
    migrate.migrate_database(path, KEY)
    again = migrate.migrate_database(path, KEY)
    assert again.applied == ()
    assert again.final_version == 5


# --------------------------------------------------------------------------
# molar-mass seed (ADR-0058 §4, approved research proposal)
# --------------------------------------------------------------------------


def test_every_seeded_molar_mass_landed(conn: sqlcipher3.Connection) -> None:
    for name, expected in SEEDED_MOLAR_MASSES.items():
        actual = _scalar(
            conn,
            "SELECT molar_mass FROM biomarkers WHERE canonical_name = ?",
            (name,),
        )
        assert actual == expected, f"{name}: expected {expected}, got {actual}"


def test_bun_molar_mass_is_the_urea_nitrogen_equivalent_not_ureas_own_weight(
    conn: sqlcipher3.Connection,
) -> None:
    # 28.014 (2 x 14.007, urea-nitrogen) is the value that reconciles with
    # the conventional 0.357 mg/dL -> mmol/L factor; urea's own molecular
    # weight (60.06) is a different, wrong number for this purpose.
    mm = _scalar(conn, "SELECT molar_mass FROM biomarkers WHERE canonical_name = 'BUN'")
    assert mm == 28.014
    assert mm != 60.06


def test_triglycerides_molar_mass_is_triolein(conn: sqlcipher3.Connection) -> None:
    mm = _scalar(
        conn,
        "SELECT molar_mass FROM biomarkers WHERE canonical_name = 'Triglycerides'",
    )
    assert mm == 885.4


def test_folate_molar_mass_is_folic_acid_not_5_mthf(
    conn: sqlcipher3.Connection,
) -> None:
    # 441.4 (folic acid, the assay-calibration convention) is correct; 459.5
    # (5-MTHF, the physiologically dominant circulating form) is not what
    # the sourced conventional factor (2.266) reconciles with.
    mm = _scalar(
        conn, "SELECT molar_mass FROM biomarkers WHERE canonical_name = 'Folate'"
    )
    assert mm == 441.4
    assert mm != 459.5


def test_albumin_has_no_molar_mass(conn: sqlcipher3.Connection) -> None:
    # Deliberate omission: albumin is a ~66 kDa protein with no
    # clinically-used molar conversion (g/dL <-> g/L is a flat x10, not a
    # molar-mass fact).
    mm = _scalar(
        conn, "SELECT molar_mass FROM biomarkers WHERE canonical_name = 'Albumin'"
    )
    assert mm is None


# --------------------------------------------------------------------------
# range_frameworks / framework_ranges seed (ADR-0058 §5)
# --------------------------------------------------------------------------


def test_three_frameworks_are_seeded_with_description_and_source_url(
    conn: sqlcipher3.Connection,
) -> None:
    rows = conn.execute(
        "SELECT name, description, source_url FROM range_frameworks ORDER BY name"
    ).fetchall()
    names = {str(row[0]) for row in rows}
    assert names == set(SEEDED_FRAMEWORKS)
    for name, description, source_url in rows:
        assert description, f"{name}: description must not be blank/NULL"
        assert source_url, f"{name}: source_url must not be blank/NULL"


def test_no_ada_framework_row_bakes_the_edition_year_into_the_name(
    conn: sqlcipher3.Connection,
) -> None:
    # FIX 1 regression: ADR-0005 versions a framework via effective_date, not
    # via its name — a year-suffixed name would fork a new framework on every
    # ADA revision instead of adding a dated row to this one.
    count = _scalar(
        conn,
        "SELECT count(*) FROM range_frameworks WHERE name LIKE '%2026%'",
    )
    assert count == 0
    ada_count = _scalar(
        conn,
        "SELECT count(*) FROM range_frameworks WHERE name = 'ada_standards_of_care'",
    )
    assert ada_count == 1


def test_seeded_framework_ranges_land_with_expected_bounds(
    conn: sqlcipher3.Connection,
) -> None:
    for framework_name, biomarkers in SEEDED_FRAMEWORKS.items():
        for biomarker_name, (low, high, unit) in biomarkers.items():
            row = conn.execute(
                "SELECT fr.range_low, fr.range_high, fr.unit, fr.effective_date "
                "FROM framework_ranges fr "
                "JOIN range_frameworks f ON f.id = fr.framework_id "
                "JOIN biomarkers b ON b.id = fr.biomarker_id "
                "WHERE f.name = ? AND b.canonical_name = ?",
                (framework_name, biomarker_name),
            ).fetchone()
            pair = f"{framework_name}/{biomarker_name}"
            assert row is not None, f"missing range row: {pair}"
            assert row[0] == low, f"{framework_name}/{biomarker_name} range_low"
            assert row[1] == high, f"{framework_name}/{biomarker_name} range_high"
            assert row[2] == unit, f"{framework_name}/{biomarker_name} unit"
            assert row[3] is None, (
                f"{framework_name}/{biomarker_name} effective_date must be the "
                "dateless default"
            )


def test_ada_a1c_ceiling_is_the_inclusive_bound_corrected_value(
    conn: sqlcipher3.Connection,
) -> None:
    # FIX 2 regression: the ADA source's prediabetes floor is 5.7%; the
    # seeded inclusive range_high must be 5.6 (largest still-normal value),
    # never 5.7 (which would flag a prediabetic result in_range).
    high = _scalar(
        conn,
        "SELECT fr.range_high FROM framework_ranges fr "
        "JOIN range_frameworks f ON f.id = fr.framework_id "
        "JOIN biomarkers b ON b.id = fr.biomarker_id "
        "WHERE f.name = 'ada_standards_of_care' "
        "AND b.canonical_name = 'Hemoglobin A1c'",
    )
    assert high == 5.6
    assert high != 5.7


def test_ada_glucose_ceiling_is_the_inclusive_bound_corrected_value(
    conn: sqlcipher3.Connection,
) -> None:
    high = _scalar(
        conn,
        "SELECT fr.range_high FROM framework_ranges fr "
        "JOIN range_frameworks f ON f.id = fr.framework_id "
        "JOIN biomarkers b ON b.id = fr.biomarker_id "
        "WHERE f.name = 'ada_standards_of_care' AND b.canonical_name = 'Glucose'",
    )
    assert high == 99
    assert high != 100


def test_ada_glucose_range_low_is_not_null_if_glucose_row_exists(
    conn: sqlcipher3.Connection,
) -> None:
    # FIX 3 regression net: a floorless glucose target is a safety bug (a
    # severe-hypoglycemia value would flag in_range). If a Glucose row exists
    # in ada_standards_of_care at all, it must carry a non-NULL range_low —
    # and per the sourced ADA Level 1 hypoglycemia alert value, that floor is
    # 70 mg/dL.
    row = conn.execute(
        "SELECT fr.range_low FROM framework_ranges fr "
        "JOIN range_frameworks f ON f.id = fr.framework_id "
        "JOIN biomarkers b ON b.id = fr.biomarker_id "
        "WHERE f.name = 'ada_standards_of_care' AND b.canonical_name = 'Glucose'"
    ).fetchone()
    if row is not None:
        assert row[0] is not None, (
            "Glucose seeded in ada_standards_of_care with a NULL range_low is a "
            "safety bug: it would flag severe hypoglycemia in_range"
        )
        assert row[0] == 70


def test_no_seeded_framework_range_has_both_bounds_null(
    conn: sqlcipher3.Connection,
) -> None:
    # Proves the ADR-0005 CHECK (range_low IS NOT NULL OR range_high IS NOT
    # NULL OR range_text IS NOT NULL) actually holds for the real seed, not
    # just that the CHECK exists.
    both_null = _scalar(
        conn,
        "SELECT count(*) FROM framework_ranges "
        "WHERE range_low IS NULL AND range_high IS NULL AND range_text IS NULL",
    )
    assert both_null == 0


def test_framework_ranges_check_rejects_a_contentless_row(
    conn: sqlcipher3.Connection,
) -> None:
    # Companion to the count-based test above: prove the DB itself refuses
    # to accept a bounds-and-text-free row, not just that the seed avoided
    # writing one.
    framework_id = _scalar(
        conn,
        "SELECT id FROM range_frameworks WHERE name = 'nih_medlineplus_lipid_targets'",
    )
    biomarker_id = _scalar(
        conn, "SELECT id FROM biomarkers WHERE canonical_name = 'Glucose'"
    )
    with pytest.raises(sqlcipher3.IntegrityError, match="CHECK"):
        conn.execute(
            "INSERT INTO framework_ranges "
            "(framework_id, biomarker_id, range_low, range_high, unit) "
            "VALUES (?, ?, NULL, NULL, 'mg/dL')",
            (framework_id, biomarker_id),
        )


# --------------------------------------------------------------------------
# critical regression: every seeded framework_ranges.unit is valid UCUM AND
# actually convertible to its biomarker's canonical_unit (a row that can
# only ever produce an `error` flag is a seeding bug).
# --------------------------------------------------------------------------


def _all_seeded_framework_ranges(
    conn: sqlcipher3.Connection,
) -> list[tuple[str, str, str, str | None, float | None]]:
    """(framework_name, biomarker_name, range_unit, canonical_unit, molar_mass)
    for every framework_ranges row actually present in the seeded database —
    driven off the real seed, not a hardcoded list, so future seed rows
    (including ones added by later migrations) are covered automatically.
    """
    rows = conn.execute(
        "SELECT f.name, b.canonical_name, fr.unit, b.canonical_unit, b.molar_mass "
        "FROM framework_ranges fr "
        "JOIN range_frameworks f ON f.id = fr.framework_id "
        "JOIN biomarkers b ON b.id = fr.biomarker_id"
    ).fetchall()
    assert rows, "expected at least one seeded framework_ranges row"
    return [(str(fw), str(bm), str(u), cu, mm) for fw, bm, u, cu, mm in rows]


def test_every_seeded_framework_range_unit_is_valid_ucum(
    conn: sqlcipher3.Connection,
) -> None:
    for (
        framework,
        biomarker,
        unit,
        _canonical_unit,
        _molar_mass,
    ) in _all_seeded_framework_ranges(conn):
        assert is_valid_unit(unit), (
            f"{framework}/{biomarker}: unit {unit!r} is not valid UCUM"
        )


def test_every_seeded_framework_range_unit_converts_to_canonical_unit(
    conn: sqlcipher3.Connection,
) -> None:
    for (
        framework,
        biomarker,
        unit,
        canonical_unit,
        molar_mass,
    ) in _all_seeded_framework_ranges(conn):
        assert canonical_unit is not None, (
            f"{framework}/{biomarker}: biomarker has no canonical_unit — a "
            "seeded range against it can only ever produce `error`"
        )
        try:
            convert(1.0, unit, canonical_unit, molar_mass=molar_mass)
        except UnitError as exc:
            pytest.fail(
                f"{framework}/{biomarker}: seeded unit {unit!r} does not "
                f"convert to canonical_unit {canonical_unit!r} "
                f"(molar_mass={molar_mass!r}): {exc}"
            )
