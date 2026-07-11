"""The fixture loader and its canary-manifest derivation (Phase 1 WI-3b).

Two surfaces: (1) manifest derivation from parsed typed records -- the owner of
the log-canary manifest the scanner consumes -- including the grep-distinctness
rule it enforces on numeric health fields; (2) loading the committed synthetic
fixtures into a real ephemeral SQLCipher database through the full migration
path, proving they satisfy migration 0001's constraints and drive the FTS
triggers.

Edge-case manifests are built under tmp_path with values distinct from the real
fixtures, so a failure here never echoes a real canary value.
"""

import re
from collections.abc import Iterator
from pathlib import Path

import fixture_loader as fl
import pytest
import sqlcipher3

from healthspan import db
from healthspan.kdf import DbKey

KEY = DbKey(bytearray(range(1, 33)))  # non-zero (all-zero reads as zeroized)
_DECIMAL = re.compile(r"\d+\.\d+")


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --- parse_fixtures -------------------------------------------------------


def test_missing_fixtures_dir_yields_empty_mapping(tmp_path: Path) -> None:
    assert fl.parse_fixtures(tmp_path / "does-not-exist") == {}


def test_files_merge_by_table(tmp_path: Path) -> None:
    _write(tmp_path, "a.json", '{"biomarkers": [{"id": 1, "canonical_name": "A"}]}')
    _write(tmp_path, "b.json", '{"biomarkers": [{"id": 2, "canonical_name": "B"}]}')
    parsed = fl.parse_fixtures(tmp_path)
    assert [row["id"] for row in parsed["biomarkers"]] == [1, 2]


def test_unexpected_file_type_fails_loudly(tmp_path: Path) -> None:
    (tmp_path / "export.bin").write_bytes(b"\xff\xfe\x00")
    with pytest.raises(fl.FixtureError, match=r"export\.bin"):
        fl.parse_fixtures(tmp_path)


def test_unknown_table_fails_loudly(tmp_path: Path) -> None:
    _write(tmp_path, "bad.json", '{"not_a_table": [{"id": 1}]}')
    with pytest.raises(fl.FixtureError, match="unknown table"):
        fl.parse_fixtures(tmp_path)


def test_malformed_json_fails_loudly(tmp_path: Path) -> None:
    _write(tmp_path, "broken.json", "{not json")
    with pytest.raises(fl.FixtureError, match="invalid JSON"):
        fl.parse_fixtures(tmp_path)


def test_non_object_top_level_fails_loudly(tmp_path: Path) -> None:
    _write(tmp_path, "list.json", "[1, 2, 3]")
    with pytest.raises(fl.FixtureError, match="must be an object"):
        fl.parse_fixtures(tmp_path)


# --- build_manifest: derivation rules -------------------------------------


def test_text_field_contributes_canary_tokens(tmp_path: Path) -> None:
    _write(
        tmp_path, "n.json", '{"lab_results": [{"notes": "see CANARY-note-x today"}]}'
    )
    assert fl.build_manifest(tmp_path) == {"CANARY-note-x"}


def test_numeric_field_contributes_high_entropy_decimal(tmp_path: Path) -> None:
    _write(tmp_path, "c.json", '{"cgm_readings": [{"glucose_mg_dl": 77.315499}]}')
    assert fl.build_manifest(tmp_path) == {"77.315499"}


def test_dotted_token_survives_and_final_period_excluded(tmp_path: Path) -> None:
    _write(tmp_path, "n.json", '{"analyses": [{"body": "ref CANARY-v1.2-note here."}]}')
    assert fl.build_manifest(tmp_path) == {"CANARY-v1.2-note"}


def test_undeclared_columns_stay_out_of_manifest(tmp_path: Path) -> None:
    # unit + reference ranges are not health-owner values; only value_num is.
    _write(
        tmp_path,
        "r.json",
        '{"lab_results": [{"value_num": 66.204813, "unit": "mg/dL",'
        ' "reference_low": 70, "reference_high": 99}]}',
    )
    assert fl.build_manifest(tmp_path) == {"66.204813"}


def test_null_and_missing_fields_are_skipped(tmp_path: Path) -> None:
    _write(tmp_path, "r.json", '{"lab_results": [{"value_num": null, "notes": null}]}')
    assert fl.build_manifest(tmp_path) == set()


def test_low_entropy_numeric_health_value_is_rejected(tmp_path: Path) -> None:
    # Five significant digits: not grep-distinctive, so the fixture is a bug.
    _write(tmp_path, "c.json", '{"cgm_readings": [{"glucose_mg_dl": 93.184}]}')
    with pytest.raises(fl.FixtureError, match="grep-distinctive"):
        fl.build_manifest(tmp_path)


def test_integer_in_numeric_health_field_is_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "c.json", '{"cgm_readings": [{"glucose_mg_dl": 95}]}')
    with pytest.raises(fl.FixtureError, match="grep-distinctive"):
        fl.build_manifest(tmp_path)


# --- build_manifest: the committed fixtures -------------------------------


def test_real_manifest_covers_representative_values() -> None:
    manifest = fl.build_manifest()
    # numeric health values across domains
    assert {"104.73921", "93.184072", "82.617394", "12.573948"} <= manifest
    # canary tokens from text/narrative fields, including value_text and
    # a token nested inside the analyses result_data JSON string
    assert {
        "CANARY-flurgle",
        "CANARY-CompoundQ",
        "CANARY-qual-indeterminate",
        "CANARY-homair-estimate",
    } <= manifest


def test_real_manifest_excludes_reference_and_catalog_data() -> None:
    manifest = fl.build_manifest()
    # reference-range bounds and catalog names are not the owner's health data
    assert "70" not in manifest
    assert "99" not in manifest
    assert "Glucose" not in manifest


def test_every_manifest_value_is_grep_distinctive() -> None:
    # The gate is only as strong as this property (testing-strategy.md): each
    # value is either a CANARY- token or a high-entropy decimal.
    for value in fl.build_manifest():
        is_token = fl.CANARY_TOKEN.fullmatch(value) is not None
        is_decimal = (
            _DECIMAL.fullmatch(value) is not None
            and len(value.replace(".", "").strip("0")) >= 6
        )
        assert is_token or is_decimal, value


def test_canary_fields_are_all_known_tables() -> None:
    assert set(fl.CANARY_FIELDS) <= set(fl.TABLE_ORDER)


# --- create_loaded_database: real SQLCipher round-trip --------------------


@pytest.fixture
def loaded(tmp_path: Path) -> Iterator[sqlcipher3.Connection]:
    conn = fl.create_loaded_database(tmp_path / "healthspan.db", KEY)
    try:
        yield conn
    finally:
        db.close(conn)


def _count(conn: sqlcipher3.Connection, sql: str) -> int:
    row = conn.execute(sql).fetchone()
    assert row is not None
    return int(row[0])


def test_fixtures_load_through_the_migration_path(
    loaded: sqlcipher3.Connection,
) -> None:
    assert _count(loaded, "SELECT count(*) FROM lab_results") == 5
    assert _count(loaded, "SELECT count(*) FROM cgm_readings") == 3
    assert _count(loaded, "SELECT count(*) FROM body_composition") == 1
    assert _count(loaded, "SELECT count(*) FROM intervention_dose_history") == 1
    # a purely qualitative result exercises the ADR-0030 value-model CHECK
    assert (
        _count(
            loaded,
            "SELECT count(*) FROM lab_results "
            "WHERE value_num IS NULL AND value_text IS NOT NULL",
        )
        == 1
    )


def test_loaded_fixtures_satisfy_foreign_keys(loaded: sqlcipher3.Connection) -> None:
    # The runtime connection enforces foreign keys; a clean check proves the
    # FK-ordered load produced a referentially consistent database.
    assert db.foreign_key_ok(loaded) is True
    assert _count(loaded, "SELECT count(*) FROM document_lab_draws") == 1


def test_current_view_hides_superseded_rows(loaded: sqlcipher3.Connection) -> None:
    # With nothing superseded the view mirrors the base table; supersede one
    # row and the view must drop exactly it while the base count holds -- a
    # plain SELECT * passthrough would fail the second assertion (ADR-0027).
    base = _count(loaded, "SELECT count(*) FROM lab_results")
    assert _count(loaded, "SELECT count(*) FROM lab_results_current") == base
    loaded.execute("UPDATE lab_results SET superseded_by = 1 WHERE id = 4")
    loaded.commit()
    assert _count(loaded, "SELECT count(*) FROM lab_results") == base
    assert _count(loaded, "SELECT count(*) FROM lab_results_current") == base - 1


def test_load_rejects_illegal_column_identifier(loaded: sqlcipher3.Connection) -> None:
    # The identifier guard is what keeps the parameterized-insert builder's
    # interpolated column names safe; prove it fires before any SQL executes.
    with pytest.raises(fl.FixtureError, match="illegal column identifier"):
        fl.load_fixtures(loaded, {"biomarkers": [{"bad; DROP": 1}]})


def test_fts_triggers_index_loaded_document_bodies(
    loaded: sqlcipher3.Connection,
) -> None:
    # Distinctive tokens survive the porter tokenizer's hyphen split, so a
    # MATCH on the tail term finds the inserted body (ADR-0041 sync trigger).
    assert (
        _count(
            loaded,
            "SELECT count(*) FROM clinical_documents_fts "
            "WHERE clinical_documents_fts MATCH 'flurgle'",
        )
        == 1
    )
    assert (
        _count(
            loaded,
            "SELECT count(*) FROM subjective_observations_fts "
            "WHERE subjective_observations_fts MATCH 'zibbly'",
        )
        == 1
    )


def test_table_order_matches_the_migrated_schema(
    loaded: sqlcipher3.Connection,
) -> None:
    # Every table the loader knows how to fill must exist in migration 0001 --
    # a drift guard between TABLE_ORDER and the schema.
    rows = loaded.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    schema_tables = {row[0] for row in rows}
    assert set(fl.TABLE_ORDER) <= schema_tables
