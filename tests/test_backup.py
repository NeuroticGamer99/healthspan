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
    # Creation order IS chronological order; same-second backups get a "-N"
    # collision suffix that sorts lexically before the un-suffixed name, so a
    # plain sort would invert them. list_backups must return creation order.
    assert listed == made  # only published .db files, no sidecar/partial


def test_list_backups_missing_dir_is_empty(tmp_path: Path) -> None:
    assert list_backups(tmp_path / "nope") == []


def test_latest_backup_returns_newest(live_db: Path, tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    made = _make_backups(live_db, backup_dir, 3)
    # The newest is the last created, NOT the lexical max (a same-second
    # collision suffix "-N" sorts before the plain ".db" name).
    assert latest_backup(backup_dir) == made[-1]


def test_latest_backup_without_any_raises(tmp_path: Path) -> None:
    with pytest.raises(BackupError, match="no published backups"):
        latest_backup(tmp_path / "backups")


def test_prune_keeps_newest_and_drops_pairs(live_db: Path, tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    made = _make_backups(live_db, backup_dir, 4)
    pruned = prune_backups(backup_dir, retention_count=2)
    assert pruned == made[:2]  # the two OLDEST (first created), not lexical
    for gone in pruned:
        assert not gone.exists()
        assert not (backup_dir / f"{gone.name}.keyparams").exists()
    assert list_backups(backup_dir) == made[2:]


def test_prune_within_retention_is_a_noop(live_db: Path, tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    _make_backups(live_db, backup_dir, 2)
    assert prune_backups(backup_dir, retention_count=5) == []


def test_same_second_collision_orders_chronologically(tmp_path: Path) -> None:
    # Regression for the lexical-sort inversion: '-' (0x2D) < '.' (0x2E), so a
    # same-second collision backup ("-2") sorts before the un-suffixed first
    # backup of that second. Built by hand so the collision is deterministic.
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    stamp = "20240314T133000Z"
    first = backup_dir / f"healthspan-{stamp}.db"  # created first (older)
    second = backup_dir / f"healthspan-{stamp}-2.db"  # created second (newer)
    for db_file in (first, second):
        db_file.write_bytes(b"ciphertext")
        (backup_dir / f"{db_file.name}.keyparams").write_bytes(b"sidecar")
    assert max([first, second]) == first  # a plain sort WOULD invert them
    assert list_backups(backup_dir) == [first, second]  # chronological order
    assert latest_backup(backup_dir) == second  # newest, not the lexical max


def test_crash_between_publish_renames_leaves_no_visible_backup(
    live_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Publish renames the sidecar first, then the db (which list_backups keys
    # on). A crash between them must never surface a db without its sidecar:
    # here the db rename fails, so the sidecar lands but the db does not, and
    # list_backups (keyed on the db) reports nothing restorable.
    backup_dir = tmp_path / "backups"
    real_rename = Path.rename

    def _flaky_rename(self: Path, target: Path) -> Path:
        if str(target).endswith(".db"):  # the final db publish
            raise OSError("simulated crash publishing the database")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", _flaky_rename)
    with pytest.raises(OSError, match="simulated crash"):
        create_verified_backup(live_db, KEY, backup_dir)
    monkeypatch.undo()

    assert list_backups(backup_dir) == []  # no half-published db is selectable
    with pytest.raises(BackupError, match="no published backups"):
        latest_backup(backup_dir)


def test_list_backups_excludes_db_without_sidecar(tmp_path: Path) -> None:
    # A crash between the two publish renames (or a half-pruned pair) can leave
    # a .db with no sidecar; it is not restorable, so it must not be listed,
    # selected by --latest, or counted toward retention.
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    orphan = backup_dir / "healthspan-20240314T133000Z.db"
    orphan.write_bytes(b"ciphertext")  # no .keyparams sidecar
    assert list_backups(backup_dir) == []
