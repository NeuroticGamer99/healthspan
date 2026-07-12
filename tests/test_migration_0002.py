"""Migration 0002 schema integrity: tokens, auth_audit, and the first
multi-migration runner pass (ADR-0026/0035).

0002 is the first post-0001 migration, so it doubles as the proof that the
runner applies a *new* migration to an *existing* database incrementally —
the semantics ADR-0035 specifies but 0001 alone could not exercise.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlcipher3

from healthspan import db, migrate
from healthspan.kdf import DbKey

KEY = DbKey(bytearray(range(1, 33)))


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


# --------------------------------------------------------------------------
# Runner semantics: the first genuine version increment (ADR-0035)
# --------------------------------------------------------------------------


def test_fresh_database_reaches_schema_version_2(
    conn: sqlcipher3.Connection,
) -> None:
    assert db.schema_version(conn) == 2
    ledger = conn.execute(
        "SELECT version, filename FROM schema_version ORDER BY version"
    ).fetchall()
    assert [row[0] for row in ledger] == [1, 2]
    assert ledger[1][1] == "0002_tokens_and_auth_audit.sql"


def test_runner_applies_0002_incrementally_over_an_0001_database(
    tmp_path: Path,
) -> None:
    # A database migrated when 0001 was the newest migration, later migrated
    # by a build shipping 0002: only 0002 runs (ADR-0035 incremental apply).
    path = tmp_path / "healthspan.db"
    db.provision(path, KEY)
    all_migrations = migrate.discover_migrations()
    first = migrate.migrate_database(
        path, KEY, [m for m in all_migrations if m.version == 1]
    )
    assert first.applied == (1,)
    second = migrate.migrate_database(path, KEY, all_migrations)
    assert second.applied == (2,)
    assert second.final_version == 2


# --------------------------------------------------------------------------
# tokens table constraints (ADR-0026/0043)
# --------------------------------------------------------------------------


def _insert_token(conn: sqlcipher3.Connection, **overrides: object) -> None:
    row: dict[str, object] = {
        "name": "cli-admin",
        "token_hash": "ab" * 32,
        "scopes": "admin read",
        "authorship": "self",
        "publish_namespaces": None,
        "job_id": None,
        "created_utc": "2026-07-12T00:00:00Z",
        "revoked": 0,
        "revoked_utc": None,
    }
    row.update(overrides)
    conn.execute(
        "INSERT INTO tokens (name, token_hash, scopes, authorship, "
        "publish_namespaces, job_id, created_utc, revoked, revoked_utc) "
        "VALUES (:name, :token_hash, :scopes, :authorship, "
        ":publish_namespaces, :job_id, :created_utc, :revoked, :revoked_utc)",
        row,
    )


def test_auth_audit_indexes_exist(conn: sqlcipher3.Connection) -> None:
    names = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='auth_audit'"
        ).fetchall()
    }
    assert {"ix_auth_audit_time", "ix_auth_audit_name"} <= names


def test_tokens_and_auth_audit_are_strict(conn: sqlcipher3.Connection) -> None:
    for table in ("tokens", "auth_audit"):
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        assert row is not None
        assert "STRICT" in str(row[0])


def test_token_names_and_hashes_are_unique(conn: sqlcipher3.Connection) -> None:
    _insert_token(conn)
    with pytest.raises(sqlcipher3.IntegrityError):
        _insert_token(conn, token_hash="cd" * 32)  # same name
    with pytest.raises(sqlcipher3.IntegrityError):
        _insert_token(conn, name="gui")  # same hash


def test_token_authorship_and_revoked_checks(conn: sqlcipher3.Connection) -> None:
    with pytest.raises(sqlcipher3.IntegrityError):
        _insert_token(conn, authorship="model")
    with pytest.raises(sqlcipher3.IntegrityError):
        _insert_token(conn, revoked=2)
    with pytest.raises(sqlcipher3.IntegrityError):
        # revoked_utc without the flag is an inconsistent record
        _insert_token(conn, revoked_utc="2026-07-12T00:00:00Z")


def test_token_job_binding_is_a_real_foreign_key(
    conn: sqlcipher3.Connection,
) -> None:
    with pytest.raises(sqlcipher3.IntegrityError):
        _insert_token(conn, name="job:missing", job_id=999)
    conn.execute(
        "INSERT INTO jobs (id, job_type, status, submitted_utc) "
        "VALUES (7, 'import', 'running', '2026-07-12T00:00:00Z')"
    )
    _insert_token(conn, name="job:bound", job_id=7)


# --------------------------------------------------------------------------
# auth_audit: outcome enum and append-only immutability (ADR-0026)
# --------------------------------------------------------------------------


def _insert_audit(conn: sqlcipher3.Connection, outcome: str) -> None:
    conn.execute(
        "INSERT INTO auth_audit (occurred_utc, token_name, source_addr, "
        "endpoint, method, outcome) VALUES (?, ?, ?, ?, ?, ?)",
        (
            "2026-07-12T00:00:00Z",
            "cli-admin",
            "127.0.0.1",
            "/v1/metrics",
            "GET",
            outcome,
        ),
    )


def test_auth_audit_accepts_every_specified_outcome(
    conn: sqlcipher3.Connection,
) -> None:
    for outcome in (
        "ok",
        "denied:scope",
        "denied:invalid",
        "denied:revoked",
        "rate-limited",
    ):
        _insert_audit(conn, outcome)


def test_auth_audit_rejects_unknown_outcomes(conn: sqlcipher3.Connection) -> None:
    with pytest.raises(sqlcipher3.IntegrityError):
        _insert_audit(conn, "denied:unknown")


def test_auth_audit_is_append_only(conn: sqlcipher3.Connection) -> None:
    _insert_audit(conn, "ok")
    with pytest.raises(sqlcipher3.DatabaseError, match="append-only"):
        conn.execute("UPDATE auth_audit SET token_name = 'forged'")
    with pytest.raises(sqlcipher3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM auth_audit")
