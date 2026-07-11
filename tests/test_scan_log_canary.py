"""Unit tests for the log canary gate scanner (scripts/scan_log_canary.py).

Scanner-only concerns: boundary anchoring, multi-hit ordering, and the CLI's
exit-code contract. Manifest *derivation* now lives with the fixture loader
(WI-3b) and is tested in test_fixture_loader.py; ``main`` consumes that
loader-owned manifest, which these tests exercise by pointing the loader at a
throwaway fixtures directory under tmp_path -- never tests/fixtures/, so they
cannot feed the real CI manifest.
"""

from pathlib import Path

import fixture_loader
import pytest
import scan_log_canary as slc


def _log(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "captured.log"
    path.write_text(text, encoding="utf-8")
    return path


# --- scan -----------------------------------------------------------------


# These scan()-only cases use 37.815402 -- a decimal that is NOT a committed
# fixture value -- so they stay inert to the real CI manifest even if a future
# refactor ever routed them through a printing path.
def test_scan_reports_hit_with_location(tmp_path: Path) -> None:
    log = _log(tmp_path, "INFO ok\nINFO glucose=37.815402 mg/dL\n")
    hits = slc.scan([log], {"37.815402"})
    assert hits == [(log, 2, "37.815402", "INFO glucose=37.815402 mg/dL")]


def test_decimal_does_not_match_inside_longer_number(tmp_path: Path) -> None:
    log = _log(tmp_path, "elapsed 137.815402 ms\noffset 37.8154020\n")
    assert slc.scan([log], {"37.815402"}) == []


def test_token_does_not_match_as_prefix_of_longer_token(tmp_path: Path) -> None:
    log = _log(tmp_path, "saw CANARY-ABCDEF here\n")
    assert slc.scan([log], {"CANARY-ABC"}) == []
    hit_log = _log(tmp_path, "saw CANARY-ABC here\n")
    assert len(slc.scan([hit_log], {"CANARY-ABC"})) == 1


def test_token_does_not_match_inside_longer_alnum_run(tmp_path: Path) -> None:
    log = _log(tmp_path, "saw xCANARY-ABC here\n")
    assert slc.scan([log], {"CANARY-ABC"}) == []


def test_multiple_hits_on_one_line_in_position_order(tmp_path: Path) -> None:
    log = _log(tmp_path, "a=37.815402 b=CANARY-xyz\n")
    values = [hit[2] for hit in slc.scan([log], {"37.815402", "CANARY-xyz"})]
    assert values == ["37.815402", "CANARY-xyz"]


def test_empty_manifest_never_hits(tmp_path: Path) -> None:
    log = _log(tmp_path, "anything 37.815402\n")
    assert slc.scan([log], set()) == []


# --- main -----------------------------------------------------------------


def test_main_requires_log_arguments() -> None:
    assert slc.main([]) == 2


def test_main_rejects_missing_log_file(tmp_path: Path) -> None:
    assert slc.main([str(tmp_path / "absent.log")]) == 2


def test_main_end_to_end_hit_and_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A throwaway value that is deliberately NOT a real fixture value: main()
    # prints the offending line on a hit, and --capture=tee-sys tees that into
    # the CI log the real gate scans -- a real manifest value here would make
    # the gate flag this test's own diagnostic output.
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "lab.json").write_text(
        '{"lab_results": [{"value_num": 55.512347}]}', encoding="utf-8"
    )
    monkeypatch.setattr(fixture_loader, "FIXTURES_DIR", fixtures)

    leak = _log(tmp_path, "INFO glucose=55.512347\n")
    assert slc.main([str(leak)]) == 1

    clean = tmp_path / "clean.log"
    clean.write_text("INFO nothing to see\n", encoding="utf-8")
    assert slc.main([str(clean)]) == 0


def test_main_fails_on_manifest_derivation_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "stray.bin").write_bytes(b"\xff")
    monkeypatch.setattr(fixture_loader, "FIXTURES_DIR", fixtures)
    log = _log(tmp_path, "INFO ok\n")
    assert slc.main([str(log)]) == 2
