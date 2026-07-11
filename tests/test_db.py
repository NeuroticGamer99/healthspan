"""Connection factory: provisioning, keying, pragma discipline, rekey."""

from pathlib import Path

import pytest

from healthspan import db
from healthspan.kdf import DbKey

KEY_A = DbKey(bytearray(range(32)))


def _fresh_key(fill: int) -> DbKey:
    return DbKey(bytearray([fill] * 32))


def test_provision_creates_encrypted_file_with_persistent_pragmas(
    tmp_path: Path,
) -> None:
    path = tmp_path / "healthspan.db"
    db.provision(path, KEY_A)
    header = path.read_bytes()[:16]
    assert header != b"SQLite format 3\x00"  # plaintext SQLite magic absent
    conn = db.connect(path, KEY_A)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        row = conn.execute("PRAGMA application_id").fetchone()
        assert row is not None
        assert row[0] == db.APPLICATION_ID
    finally:
        db.close(conn)


def test_connect_applies_runtime_pragma_set(tmp_path: Path) -> None:
    path = tmp_path / "healthspan.db"
    db.provision(path, KEY_A)
    conn = db.connect(path, KEY_A)
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone() == (1,)
        assert conn.execute("PRAGMA synchronous").fetchone() == (1,)  # NORMAL
        assert conn.execute("PRAGMA busy_timeout").fetchone() == (5000,)
    finally:
        db.close(conn)


def test_wrong_key_is_rejected_with_guidance(tmp_path: Path) -> None:
    path = tmp_path / "healthspan.db"
    db.provision(path, KEY_A)
    with pytest.raises(db.DatabaseError, match="wrong passphrase/secret key"):
        db.connect(path, _fresh_key(0x42))


def test_connect_requires_existing_file(tmp_path: Path) -> None:
    with pytest.raises(db.DatabaseError, match="does not exist"):
        db.connect(tmp_path / "absent.db", KEY_A)


def test_provision_refuses_to_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "healthspan.db"
    db.provision(path, KEY_A)
    with pytest.raises(db.DatabaseError, match="refusing to overwrite"):
        db.provision(path, KEY_A)


def test_rekey_round_trip_preserves_data(tmp_path: Path) -> None:
    path = tmp_path / "healthspan.db"
    db.provision(path, KEY_A)
    conn = db.connect(path, KEY_A)
    conn.execute("CREATE TABLE t (v TEXT) STRICT")
    conn.execute("INSERT INTO t VALUES (?)", ("payload",))
    new_key = _fresh_key(0x7F)
    db.rekey(conn, new_key)
    db.close(conn)

    with pytest.raises(db.DatabaseError):
        db.connect(path, KEY_A)  # old key no longer opens
    conn = db.connect(path, new_key)
    try:
        assert conn.execute("SELECT v FROM t").fetchone() == ("payload",)
    finally:
        db.close(conn)


def test_key_pragma_validates_hex_format() -> None:
    zeroized = _fresh_key(0)
    # An all-zero buffer reads as zeroized key material.
    with pytest.raises(RuntimeError, match="zeroized"):
        db.key_pragma("key", zeroized)
    statement = db.key_pragma("key", KEY_A)
    assert statement.startswith("PRAGMA key = \"x'")
    assert KEY_A.hex() in statement


def test_schema_version_is_none_before_migrations(tmp_path: Path) -> None:
    path = tmp_path / "healthspan.db"
    db.provision(path, KEY_A)
    conn = db.connect(path, KEY_A)
    try:
        assert db.schema_version(conn) is None
        assert db.integrity_ok(conn)
    finally:
        db.close(conn)


def test_foreign_key_ok_passes_clean_and_flags_violations(tmp_path: Path) -> None:
    path = tmp_path / "healthspan.db"
    db.provision(path, KEY_A)
    conn = db.connect(path, KEY_A)
    try:
        conn.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY) STRICT")
        conn.execute(
            "CREATE TABLE child (parent_id INTEGER REFERENCES parent(id)) STRICT"
        )
        assert db.foreign_key_ok(conn)  # no rows yet
        # Insert an orphan with enforcement off, as the migration runner does;
        # foreign_key_check must still catch it independent of that pragma.
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("INSERT INTO child VALUES (999)")
        assert not db.foreign_key_ok(conn)
    finally:
        db.close(conn)
