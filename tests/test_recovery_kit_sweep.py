"""Recovery Kit orphan-plaintext startup sweep (ADR-0033, ADR-0049)."""

from pathlib import Path

from healthspan.recovery_kit import sweep_orphans


def test_sweep_disposes_orphan_kit(tmp_path: Path) -> None:
    orphan = tmp_path / "healthspan-recovery-kit-2026-07-11.txt"
    orphan.write_text("orphaned kit plaintext", encoding="utf-8")
    keep = tmp_path / "healthspan.db"
    keep.write_text("ciphertext", encoding="utf-8")

    disposed = sweep_orphans(tmp_path)

    assert disposed == [orphan]
    assert not orphan.exists()
    assert keep.exists()  # non-kit files are untouched


def test_sweep_returns_empty_for_clean_dir(tmp_path: Path) -> None:
    assert sweep_orphans(tmp_path) == []


def test_sweep_tolerates_missing_dir(tmp_path: Path) -> None:
    assert sweep_orphans(tmp_path / "does-not-exist") == []


def test_sweep_disposes_multiple_orphans(tmp_path: Path) -> None:
    first = tmp_path / "old-recovery-kit-1.txt"
    second = tmp_path / "healthspan-recovery-kit-2026.txt"
    first.write_text("x", encoding="utf-8")
    second.write_text("y", encoding="utf-8")

    disposed = sweep_orphans(tmp_path)

    assert set(disposed) == {first, second}
    assert not first.exists()
    assert not second.exists()
