"""Verified backup primitive: verify-then-publish, no partial artifacts."""

from pathlib import Path

import pytest

from healthspan import db
from healthspan.backup import (
    BackupError,
    create_verified_backup,
    latest_backup,
    list_backups,
    prune_backups,
)
from healthspan.kdf import DbKey
from healthspan.keyparams import (
    KeyMode,
    KeyParams,
    sidecar_path,
    utc_now_iso,
    write_keyparams,
)

KEY = DbKey(bytearray(range(32)))


@pytest.fixture
def live_db(tmp_path: Path) -> Path:
    path = tmp_path / "healthspan.db"
    db.provision(path, KEY)
    conn = db.connect(path, KEY)
    conn.execute("CREATE TABLE t (v TEXT) STRICT")
    conn.execute("INSERT INTO t VALUES (?)", ("payload",))
    db.close(conn)
    write_keyparams(
        sidecar_path(path),
        KeyParams(mode=KeyMode.TWO_FACTOR, created_utc=utc_now_iso()),
    )
    return path


def test_backup_publishes_verified_pair(live_db: Path, tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    result = create_verified_backup(live_db, KEY, backup_dir)
    assert result.database.exists()
    assert result.sidecar.exists()
    assert result.sidecar.read_bytes() == sidecar_path(live_db).read_bytes()
    assert not list(backup_dir.glob("*.partial"))
    conn = db.connect(result.database, KEY)
    try:
        assert conn.execute("SELECT v FROM t").fetchone() == ("payload",)
    finally:
        db.close(conn)


def test_backup_refuses_without_sidecar(live_db: Path, tmp_path: Path) -> None:
    sidecar_path(live_db).unlink()
    with pytest.raises(BackupError, match=r"sidecar .* is missing"):
        create_verified_backup(live_db, KEY, tmp_path / "backups")


def test_failed_backup_leaves_no_partial_files(live_db: Path, tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    wrong = DbKey(bytearray([9] * 32))
    with pytest.raises(BackupError):
        create_verified_backup(live_db, wrong, backup_dir)
    assert not any(backup_dir.iterdir())


def test_backup_rejects_source_with_fk_violations(tmp_path: Path) -> None:
    # A source carrying a dangling FK reference fails the verification gate
    # (foreign_key_check), so nothing is published (ADR-0038 definition).
    path = tmp_path / "healthspan.db"
    db.provision(path, KEY)
    conn = db.connect(path, KEY)
    conn.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY) STRICT")
    conn.execute("CREATE TABLE child (parent_id INTEGER REFERENCES parent(id)) STRICT")
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("INSERT INTO child VALUES (999)")
    db.close(conn)
    write_keyparams(
        sidecar_path(path),
        KeyParams(mode=KeyMode.TWO_FACTOR, created_utc=utc_now_iso()),
    )
    backup_dir = tmp_path / "backups"
    with pytest.raises(BackupError, match="foreign_key_check"):
        create_verified_backup(path, KEY, backup_dir)
    assert not any(backup_dir.iterdir())


def test_same_second_backups_get_distinct_names(live_db: Path, tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    first = create_verified_backup(live_db, KEY, backup_dir)
    second = create_verified_backup(live_db, KEY, backup_dir)
    assert first.database != second.database
    assert first.sidecar != second.sidecar


def _make_backups(live_db: Path, backup_dir: Path, n: int) -> list[Path]:
    return [create_verified_backup(live_db, KEY, backup_dir).database for _ in range(n)]


def test_list_backups_excludes_sidecars_and_partials(
    live_db: Path, tmp_path: Path
) -> None:
    backup_dir = tmp_path / "backups"
    made = _make_backups(live_db, backup_dir, 2)
    (backup_dir / "healthspan-20990101T000000Z.db.partial").write_bytes(b"x")
    listed = list_backups(backup_dir)
    assert listed == sorted(made)  # only published .db files, no sidecar/partial


def test_list_backups_missing_dir_is_empty(tmp_path: Path) -> None:
    assert list_backups(tmp_path / "nope") == []


def test_latest_backup_returns_newest(live_db: Path, tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    made = _make_backups(live_db, backup_dir, 3)
    assert latest_backup(backup_dir) == max(made)


def test_latest_backup_without_any_raises(tmp_path: Path) -> None:
    with pytest.raises(BackupError, match="no published backups"):
        latest_backup(tmp_path / "backups")


def test_prune_keeps_newest_and_drops_pairs(live_db: Path, tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    made = _make_backups(live_db, backup_dir, 4)
    pruned = prune_backups(backup_dir, retention_count=2)
    assert pruned == sorted(made)[:2]  # the two oldest
    for gone in pruned:
        assert not gone.exists()
        assert not (backup_dir / f"{gone.name}.keyparams").exists()
    assert list_backups(backup_dir) == sorted(made)[2:]


def test_prune_within_retention_is_a_noop(live_db: Path, tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    _make_backups(live_db, backup_dir, 2)
    assert prune_backups(backup_dir, retention_count=5) == []
