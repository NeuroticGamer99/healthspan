"""Verified backup primitive: verify-then-publish, no partial artifacts."""

from pathlib import Path

import pytest

from healthspan import db
from healthspan.backup import BackupError, create_verified_backup
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


def test_same_second_backups_get_distinct_names(live_db: Path, tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    first = create_verified_backup(live_db, KEY, backup_dir)
    second = create_verified_backup(live_db, KEY, backup_dir)
    assert first.database != second.database
    assert first.sidecar != second.sidecar
