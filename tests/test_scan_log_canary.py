"""Unit tests for the log canary gate scanner (scripts/scan_log_canary.py).

Case list sourced from the 2026-07-10 review of PR #6: boundary anchoring,
timestamp/version exclusion, significant-digit counting, dotted tokens, and
non-fixture-file handling. These tests build fixtures under tmp_path only --
never under tests/fixtures/ -- so they cannot feed the real CI manifest.
"""

from pathlib import Path

import pytest
import scan_log_canary as slc


def _fixture(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --- build_manifest -------------------------------------------------------


def test_missing_fixtures_dir_yields_empty_manifest(tmp_path: Path) -> None:
    assert slc.build_manifest(tmp_path / "does-not-exist") == set()


def test_tokens_and_high_entropy_decimals_enter_manifest(tmp_path: Path) -> None:
    _fixture(
        tmp_path,
        "lab.json",
        '{"note": "CANARY-glucose-a", "value": 104.73921}',
    )
    assert slc.build_manifest(tmp_path) == {"CANARY-glucose-a", "104.73921"}


def test_nested_fixture_directories_are_scanned(tmp_path: Path) -> None:
    _fixture(tmp_path, "cgm/day1.sql", "INSERT INTO x VALUES (93.184072);")
    assert slc.build_manifest(tmp_path) == {"93.184072"}


def test_dotted_token_survives_intact(tmp_path: Path) -> None:
    _fixture(tmp_path, "notes.json", '{"note": "see CANARY-v1.2-note today"}')
    assert slc.build_manifest(tmp_path) == {"CANARY-v1.2-note"}


def test_sentence_final_period_not_captured(tmp_path: Path) -> None:
    _fixture(tmp_path, "notes.json", '{"note": "ends with CANARY-abc."}')
    assert slc.build_manifest(tmp_path) == {"CANARY-abc"}


def test_low_entropy_decimals_excluded(tmp_path: Path) -> None:
    # 12.5: too few digits. 100.000 / 100000.0: trailing zeros carry no
    # entropy -- round numbers must not enter the manifest.
    _fixture(tmp_path, "vals.json", '{"a": 12.5, "b": 100.000, "c": 100000.0}')
    assert slc.build_manifest(tmp_path) == set()


def test_six_significant_digit_threshold_boundary(tmp_path: Path) -> None:
    # 84.7392 -> 6 significant digits (in); 84.739 -> 5 (out).
    _fixture(tmp_path, "edge.json", '{"in": 84.7392, "out": 84.739}')
    assert slc.build_manifest(tmp_path) == {"84.7392"}


def test_timestamps_and_version_strings_excluded(tmp_path: Path) -> None:
    _fixture(
        tmp_path,
        "meta.json",
        '{"at": "2026-01-15T08:30:00.123456", "schema": "1.2.344444"}',
    )
    assert slc.build_manifest(tmp_path) == set()


def test_unexpected_file_type_fails_loudly(tmp_path: Path) -> None:
    (tmp_path / "export.bin").write_bytes(b"\xff\xfe\x00")
    with pytest.raises(ValueError, match=r"export\.bin"):
        slc.build_manifest(tmp_path)


# --- scan -----------------------------------------------------------------


def _log(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "captured.log"
    path.write_text(text, encoding="utf-8")
    return path


def test_scan_reports_hit_with_location(tmp_path: Path) -> None:
    log = _log(tmp_path, "INFO ok\nINFO glucose=104.73921 mg/dL\n")
    hits = slc.scan([log], {"104.73921"})
    assert hits == [(log, 2, "104.73921", "INFO glucose=104.73921 mg/dL")]


def test_decimal_does_not_match_inside_longer_number(tmp_path: Path) -> None:
    log = _log(tmp_path, "elapsed 1104.739215 ms\noffset 104.739215\n")
    assert slc.scan([log], {"104.73921"}) == []


def test_token_does_not_match_as_prefix_of_longer_token(tmp_path: Path) -> None:
    log = _log(tmp_path, "saw CANARY-ABCDEF here\n")
    assert slc.scan([log], {"CANARY-ABC"}) == []
    hit_log = _log(tmp_path, "saw CANARY-ABC here\n")
    assert len(slc.scan([hit_log], {"CANARY-ABC"})) == 1


def test_token_does_not_match_inside_longer_alnum_run(tmp_path: Path) -> None:
    log = _log(tmp_path, "saw xCANARY-ABC here\n")
    assert slc.scan([log], {"CANARY-ABC"}) == []


def test_multiple_hits_on_one_line_in_position_order(tmp_path: Path) -> None:
    log = _log(tmp_path, "a=104.73921 b=CANARY-xyz\n")
    values = [hit[2] for hit in slc.scan([log], {"104.73921", "CANARY-xyz"})]
    assert values == ["104.73921", "CANARY-xyz"]


def test_empty_manifest_never_hits(tmp_path: Path) -> None:
    log = _log(tmp_path, "anything 104.73921\n")
    assert slc.scan([log], set()) == []


# --- main -----------------------------------------------------------------


def test_main_requires_log_arguments() -> None:
    assert slc.main([]) == 2


def test_main_rejects_missing_log_file(tmp_path: Path) -> None:
    assert slc.main([str(tmp_path / "absent.log")]) == 2


def test_main_end_to_end_hit_and_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixtures = tmp_path / "fixtures"
    _fixture(fixtures, "lab.json", '{"value": 104.73921}')
    monkeypatch.setattr(slc, "FIXTURES_DIR", fixtures)

    leak = _log(tmp_path, "INFO glucose=104.73921\n")
    assert slc.main([str(leak)]) == 1

    clean = tmp_path / "clean.log"
    clean.write_text("INFO nothing to see\n", encoding="utf-8")
    assert slc.main([str(clean)]) == 0


def test_main_fails_on_unexpected_fixture_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "stray.bin").write_bytes(b"\xff")
    monkeypatch.setattr(slc, "FIXTURES_DIR", fixtures)
    log = _log(tmp_path, "INFO ok\n")
    assert slc.main([str(log)]) == 2
