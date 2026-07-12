"""The database migration runner (ADR-0009, ADR-0035).

A custom, dependency-free runner: numbered plain-SQL files applied in order,
each in its own explicitly-managed transaction. The execution semantics are
ADR-0035's, not the driver's — driver-level transaction management is
disabled on the connection (``isolation_level=None``, via
:func:`healthspan.db.connect_for_migration`), so the runner's own
``BEGIN IMMEDIATE`` … ``COMMIT`` is what makes "atomic per migration"
mechanically true instead of aspirational.

Per unapplied file, in numeric order:

1. ``PRAGMA foreign_keys = OFF`` — set outside the transaction (a no-op
   inside one), so SQLite's table-rebuild procedure stays available.
2. ``BEGIN IMMEDIATE`` — take the write lock up front; a busy database
   fails fast rather than deadlocking mid-migration.
3. Execute the file's statements, then insert the ``schema_version`` row —
   the ledger entry lives in the same transaction as the DDL it records.
4. ``PRAGMA foreign_key_check`` — any violation aborts the whole file.
5. ``COMMIT``; on any failure at any step, ``ROLLBACK`` and stop.

The ``schema_version`` bootstrap (``CREATE TABLE IF NOT EXISTS``) is the sole
sanctioned ``IF NOT EXISTS`` in the whole migration surface (ADR-0035): it
must run on both fresh and existing databases before the ledger exists.
Migration *files* never guard their DDL — drift must fail loudly.
"""

import contextlib
import importlib.resources
import re
import sqlite3
from dataclasses import dataclass
from importlib.resources.abc import Traversable
from pathlib import Path

import sqlcipher3

from healthspan import db
from healthspan.kdf import DbKey
from healthspan.keyparams import utc_now_iso

MIGRATIONS_PACKAGE = "healthspan.migrations"

# A migration filename: zero-padded sequence, underscore, descriptive slug.
_FILENAME = re.compile(r"^(\d+)_[A-Za-z0-9_]+\.sql$")


class MigrationError(Exception):
    """A migration could not be discovered or applied; the database is
    left exactly at its last successfully-applied version."""


@dataclass(frozen=True)
class Migration:
    """One numbered migration file: its version, name, and SQL body."""

    version: int
    filename: str
    sql: str


@dataclass(frozen=True)
class MigrationRun:
    """The outcome of a runner pass."""

    applied: tuple[int, ...]  # versions applied this run (empty if already current)
    final_version: int | None  # schema version afterwards (None if none exist at all)


def discover_migrations(root: Traversable | Path | None = None) -> list[Migration]:
    """Load and order the migration files from a package resource or directory.

    Defaults to the packaged :data:`MIGRATIONS_PACKAGE`. Fails loudly on a
    malformed filename or a duplicate version number rather than silently
    skipping or reordering — a migration corpus the runner cannot read
    unambiguously is a bug, not something to paper over.
    """
    if root is None:
        root = importlib.resources.files(MIGRATIONS_PACKAGE)
    migrations: list[Migration] = []
    seen: dict[int, str] = {}
    for entry in root.iterdir():
        if not entry.is_file() or not entry.name.endswith(".sql"):
            continue
        match = _FILENAME.match(entry.name)
        if match is None:
            raise MigrationError(
                f"migration file {entry.name!r} does not match the "
                "'<number>_<slug>.sql' naming convention (ADR-0009)"
            )
        version = int(match.group(1))
        if version in seen:
            raise MigrationError(
                f"duplicate migration version {version}: "
                f"{seen[version]!r} and {entry.name!r}"
            )
        seen[version] = entry.name
        migrations.append(
            Migration(
                version=version,
                filename=entry.name,
                sql=entry.read_text(encoding="utf-8"),
            )
        )
    migrations.sort(key=lambda m: m.version)
    return migrations


def target_version(migrations: list[Migration] | None = None) -> int | None:
    """The highest schema version this build ships (``None`` if none exist).

    The reference point for ``db restore``'s version policy (ADR-0038): a
    backup newer than this was made by a newer build and must not be
    installed under the current code.
    """
    if migrations is None:
        migrations = discover_migrations()
    return max((m.version for m in migrations), default=None)


def migrate_database(
    database_path: Path, key: DbKey, migrations: list[Migration] | None = None
) -> MigrationRun:
    """Open the encrypted database and bring it to the latest schema version.

    The connection is opened with the runner's discipline (no runtime
    pragma set — see :func:`healthspan.db.connect_for_migration`) and closed
    on the way out.
    """
    if migrations is None:
        migrations = discover_migrations()
    conn = db.connect_for_migration(database_path, key)
    try:
        return apply_migrations(conn, migrations)
    finally:
        db.close(conn)


def apply_migrations(
    conn: sqlcipher3.Connection, migrations: list[Migration]
) -> MigrationRun:
    """Apply every unapplied migration to an open connection, in order.

    The connection must have driver transaction management disabled
    (``isolation_level=None``); passing a runtime connection whose driver
    still auto-commits around DDL would silently break atomicity, which is
    the whole defect ADR-0035 exists to prevent.
    """
    _ensure_schema_version(conn)
    already = _applied_versions(conn)
    shipped_max = max((m.version for m in migrations), default=0)
    applied_max = max(already, default=0)
    if applied_max > shipped_max:
        # A database migrated by a newer build: applying nothing and reporting
        # the unknown version as current would let later code run against a
        # schema it does not understand (the restore path refuses this; the
        # runner must too).
        raise MigrationError(
            f"database is at schema version {applied_max}, newer than this "
            f"build understands (it ships up to schema version {shipped_max}). "
            "Upgrade healthspan before running migrations."
        )
    applied: list[int] = []
    for migration in sorted(migrations, key=lambda m: m.version):
        if migration.version in already:
            continue
        _apply_one(conn, migration)
        applied.append(migration.version)
    all_versions = already | set(applied)
    final = max(all_versions) if all_versions else None
    return MigrationRun(applied=tuple(applied), final_version=final)


def split_statements(script: str) -> list[str]:
    """Split a SQL script into individual statements.

    ``sqlite3.complete_statement`` is the same lexer the SQLite shell uses:
    it correctly treats the inner semicolons of a ``CREATE TRIGGER …
    BEGIN … END;`` body as part of one statement, and ignores semicolons in
    comments and string literals. This is why the runner cannot lean on the
    driver's ``executescript`` — that method issues an implicit COMMIT
    before running, which would dissolve the runner's explicit transaction.
    """
    statements: list[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement:
                statements.append(statement)
            buffer = ""
    if buffer.strip() and not _is_only_comments(buffer):
        raise MigrationError(
            "migration ends with an unterminated statement "
            f"(missing ';'?): {_snippet(buffer)}"
        )
    return statements


def _ensure_schema_version(conn: sqlcipher3.Connection) -> None:
    # The sole sanctioned IF NOT EXISTS: the ledger must exist on both fresh
    # and already-migrated databases before any migration runs (ADR-0035).
    # Runs as its own auto-committed statement, outside a migration txn.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "    version    INTEGER PRIMARY KEY,"
        "    filename   TEXT NOT NULL,"
        "    applied_at TEXT NOT NULL"
        ")"
    )


def _applied_versions(conn: sqlcipher3.Connection) -> set[int]:
    rows = conn.execute("SELECT version FROM schema_version").fetchall()
    return {row[0] for row in rows}


def _apply_one(conn: sqlcipher3.Connection, migration: Migration) -> None:
    statements = split_statements(migration.sql)
    # Set OUTSIDE the transaction: the pragma is a silent no-op inside one.
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("BEGIN IMMEDIATE")
    try:
        for statement in statements:
            _execute(conn, statement, migration.filename)
        conn.execute(
            "INSERT INTO schema_version (version, filename, applied_at) "
            "VALUES (?, ?, ?)",
            (migration.version, migration.filename, utc_now_iso()),
        )
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise MigrationError(
                f"{migration.filename}: foreign_key_check reported "
                f"{len(violations)} violation(s), migration rejected: {violations}"
            )
        conn.execute("COMMIT")
    except BaseException:
        _rollback(conn)
        raise


def _execute(conn: sqlcipher3.Connection, statement: str, filename: str) -> None:
    try:
        conn.execute(statement)
    except sqlcipher3.Error as exc:
        raise MigrationError(
            f"{filename}: failed on statement, migration rolled back: "
            f"{_snippet(statement)} ({exc})"
        ) from exc


def _rollback(conn: sqlcipher3.Connection) -> None:
    # A rollback failure must not mask the original error.
    with contextlib.suppress(sqlcipher3.Error):
        conn.execute("ROLLBACK")


def _is_only_comments(text: str) -> bool:
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped and not stripped.startswith("--"):
            return False
    return True


def _snippet(statement: str) -> str:
    first = statement.strip().splitlines()[0] if statement.strip() else ""
    return first if len(first) <= 80 else first[:77] + "..."
