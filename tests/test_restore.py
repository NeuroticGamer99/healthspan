"""Verified restore: verify-then-install, version policy, aside-rename."""

from pathlib import Path

import pytest

from healthspan import db, keychain, migrate
from healthspan.kdf import DbKey
from healthspan.keyparams import (
    KeyMode,
    KeyParams,
    sidecar_path,
    utc_now_iso,
    write_keyparams,
)
from healthspan.restore import RestoreError, derive_backup_key, restore_database

KEY = DbKey(bytearray(range(32)))


def _make_db(path: Path, marker: str, version: int | None) -> Path:
    """A keyed database carrying a marker row and, optionally, a schema
    version, with a two-factor sidecar alongside."""
    db.provision(path, KEY)
    conn = db.connect(path, KEY)
    try:
        conn.execute("CREATE TABLE t (v TEXT) STRICT")
        conn.execute("INSERT INTO t VALUES (?)", (marker,))
        if version is not None:
            conn.execute(
                "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, "
                "filename TEXT NOT NULL, applied_at TEXT NOT NULL) STRICT"
            )
            conn.execute(
                "INSERT INTO schema_version VALUES (?, ?, ?)",
                (version, f"{version:04d}_x.sql", utc_now_iso()),
            )
    finally:
        db.close(conn)
    write_keyparams(
        sidecar_path(path),
        KeyParams(mode=KeyMode.TWO_FACTOR, created_utc=utc_now_iso()),
    )
    return path


def _make_db_with_fk_violation(path: Path) -> Path:
    """A keyed database carrying a dangling foreign-key reference."""
    db.provision(path, KEY)
    conn = db.connect(path, KEY)
    try:
        conn.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY) STRICT")
        conn.execute(
            "CREATE TABLE child (parent_id INTEGER REFERENCES parent(id)) STRICT"
        )
        conn.execute("PRAGMA foreign_keys = OFF")  # slip past enforcement
        conn.execute("INSERT INTO child VALUES (999)")
    finally:
        db.close(conn)
    write_keyparams(
        sidecar_path(path),
        KeyParams(mode=KeyMode.TWO_FACTOR, created_utc=utc_now_iso()),
    )
    return path


def _read_marker(path: Path) -> str:
    conn = db.connect(path, KEY)
    try:
        row = conn.execute("SELECT v FROM t").fetchone()
        assert row is not None
        marker: str = row[0]
        return marker
    finally:
        db.close(conn)


def test_restore_round_trip_displaces_previous_live(tmp_path: Path) -> None:
    live = _make_db(tmp_path / "hs.db", "old-live", version=1)
    backup = _make_db(tmp_path / "b" / "backup.db", "from-backup", version=1)

    result = restore_database(backup, live, KEY, target_version=1)

    assert _read_marker(live) == "from-backup"
    assert result.restored_version == 1
    assert result.needs_migration is False
    assert result.displaced is not None
    assert result.displaced.exists()
    # The displaced copy still holds the previous live data (ciphertext kept).
    assert _read_marker(result.displaced) == "old-live"
    assert not list(tmp_path.glob("*.restoring*"))


def test_restore_onto_new_machine_has_nothing_to_displace(tmp_path: Path) -> None:
    backup = _make_db(tmp_path / "b" / "backup.db", "from-backup", version=1)
    live = tmp_path / "hs.db"  # does not exist yet

    result = restore_database(backup, live, KEY, target_version=1)

    assert result.displaced is None
    assert _read_marker(live) == "from-backup"


def test_restore_displaces_wal_and_shm_companions(tmp_path: Path) -> None:
    live = _make_db(tmp_path / "hs.db", "old-live", version=1)
    wal = live.with_name(live.name + "-wal")
    shm = live.with_name(live.name + "-shm")
    wal.write_bytes(b"stale-wal")
    shm.write_bytes(b"stale-shm")
    backup = _make_db(tmp_path / "b" / "backup.db", "from-backup", version=1)

    restore_database(backup, live, KEY, target_version=1)

    # A stale -wal/-shm must never survive next to the restored file.
    assert not wal.exists()
    assert not shm.exists()
    assert list(tmp_path.glob("hs.db-wal.pre-restore-*"))
    assert list(tmp_path.glob("hs.db-shm.pre-restore-*"))


def test_restore_refuses_a_newer_backup(tmp_path: Path) -> None:
    live = _make_db(tmp_path / "hs.db", "old-live", version=1)
    backup = _make_db(tmp_path / "b" / "backup.db", "newer", version=5)

    with pytest.raises(RestoreError, match="newer than this build"):
        restore_database(backup, live, KEY, target_version=1)

    assert _read_marker(live) == "old-live"  # live untouched
    assert not list(tmp_path.glob("*.restoring*"))


def test_restore_of_older_schema_flags_migration(tmp_path: Path) -> None:
    live = tmp_path / "hs.db"
    backup = _make_db(tmp_path / "b" / "backup.db", "from-backup", version=1)
    result = restore_database(backup, live, KEY, target_version=3)
    assert result.restored_version == 1
    assert result.needs_migration is True
    assert _read_marker(live) == "from-backup"  # data actually landed


def test_restore_of_schemaless_backup_flags_migration(tmp_path: Path) -> None:
    live = tmp_path / "hs.db"
    backup = _make_db(tmp_path / "b" / "backup.db", "pre-schema", version=None)
    result = restore_database(backup, live, KEY, target_version=1)
    assert result.restored_version is None
    assert result.needs_migration is True
    assert _read_marker(live) == "pre-schema"


def test_restore_then_migrate_brings_schema_current(tmp_path: Path) -> None:
    # ADR-0038: an older/absent-schema backup restores without implicit
    # migration; an explicit db migrate then brings it to the build's target.
    live = tmp_path / "hs.db"
    backup = _make_db(tmp_path / "b" / "backup.db", "pre-schema", version=None)
    restore_database(backup, live, KEY, target_version=migrate.target_version())

    run = migrate.migrate_database(live, KEY)
    assert run.final_version == migrate.target_version()
    assert _read_marker(live) == "pre-schema"  # data survived the migration


def test_restore_rejects_a_backup_with_fk_violations(tmp_path: Path) -> None:
    live = _make_db(tmp_path / "hs.db", "old-live", version=1)
    backup = _make_db_with_fk_violation(tmp_path / "b" / "backup.db")

    with pytest.raises(RestoreError, match="foreign_key_check"):
        restore_database(backup, live, KEY, target_version=1)

    assert _read_marker(live) == "old-live"  # live untouched
    assert not list(tmp_path.glob("*.restoring*"))


def test_restore_rejects_a_backup_failing_integrity_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A backup that opens with the right key but fails integrity_check (a
    # corrupted page). SQLCipher's per-page MAC makes real bit-flips surface
    # as a key-open failure, so the integrity_check branch is exercised by
    # forcing the check to report corruption.
    live = _make_db(tmp_path / "hs.db", "old-live", version=1)
    backup = _make_db(tmp_path / "b" / "backup.db", "from-backup", version=1)

    def _integrity_fails(conn: object) -> bool:
        return False

    monkeypatch.setattr(db, "integrity_ok", _integrity_fails)

    with pytest.raises(RestoreError, match="integrity_check"):
        restore_database(backup, live, KEY, target_version=1)

    assert _read_marker(live) == "old-live"
    assert not list(tmp_path.glob("*.restoring*"))


def test_restore_refuses_without_sidecar(tmp_path: Path) -> None:
    live = _make_db(tmp_path / "hs.db", "old-live", version=1)
    backup = _make_db(tmp_path / "b" / "backup.db", "from-backup", version=1)
    sidecar_path(backup).unlink()

    with pytest.raises(RestoreError, match=r"sidecar .* is missing"):
        restore_database(backup, live, KEY, target_version=1)

    assert _read_marker(live) == "old-live"


def test_restore_wrong_key_fails_and_leaves_live_untouched(tmp_path: Path) -> None:
    live = _make_db(tmp_path / "hs.db", "old-live", version=1)
    backup = _make_db(tmp_path / "b" / "backup.db", "from-backup", version=1)
    wrong = DbKey(bytearray([9] * 32))

    with pytest.raises(RestoreError, match="does not open"):
        restore_database(backup, live, wrong, target_version=1)

    assert _read_marker(live) == "old-live"
    assert not list(tmp_path.glob("*.restoring*"))


def test_restore_missing_backup_file(tmp_path: Path) -> None:
    missing = tmp_path / "nope.db"
    with pytest.raises(RestoreError, match="does not exist"):
        restore_database(missing, tmp_path / "hs.db", KEY, target_version=1)


def test_derive_backup_key_passphrase_only_opens_the_backup(tmp_path: Path) -> None:
    # Passphrase-only: the salt is in the sidecar, no keychain needed.
    from healthspan.kdf import derive_db_key, generate_secret_key

    backup = tmp_path / "b" / "backup.db"
    backup.parent.mkdir()
    salt = generate_secret_key()
    params = KeyParams(
        mode=KeyMode.PASSPHRASE_ONLY, salt=salt, created_utc=utc_now_iso()
    )
    passphrase = "a perfectly reasonable passphrase"
    key = derive_db_key(passphrase, salt, params)
    db.provision(backup, key)
    write_keyparams(sidecar_path(backup), params)

    derived = derive_backup_key(backup, passphrase)
    conn = db.connect(backup, derived)  # opens iff the key matches
    db.close(conn)


def test_derive_backup_key_two_factor_uses_keychain(tmp_path: Path) -> None:
    from healthspan.kdf import derive_db_key, generate_secret_key

    backup = tmp_path / "b" / "backup.db"
    backup.parent.mkdir()
    secret_key = generate_secret_key()
    params = KeyParams(mode=KeyMode.TWO_FACTOR, created_utc=utc_now_iso())
    passphrase = "a perfectly reasonable passphrase"
    key = derive_db_key(passphrase, secret_key, params)
    db.provision(backup, key)
    write_keyparams(sidecar_path(backup), params)
    keychain.store_secret_key(secret_key)  # as init/init --restore would

    derived = derive_backup_key(backup, passphrase)
    conn = db.connect(backup, derived)
    db.close(conn)


def test_derive_backup_key_two_factor_without_keychain_fails(tmp_path: Path) -> None:
    # Two-factor backup, empty keychain (autouse in-memory backend): the
    # secret key is unavailable, so key derivation cannot proceed.
    _make_db(tmp_path / "b" / "backup.db", "x", version=1)
    with pytest.raises(keychain.KeychainError):
        derive_backup_key(tmp_path / "b" / "backup.db", "whatever passphrase")
