"""Verified backup primitive (ADR-0038 pipeline, offline CLI form).

The core verify-then-publish routine the rotation commands' mandatory
pre-rekey backup (ADR-0028) requires. WI-4's ``healthspan db backup``
command wraps this same routine with retention and UX polish; the
pipeline ordering is ADR-0038's: nothing partial or unverified ever
appears under a final name.
"""

import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import sqlcipher3

from healthspan import db
from healthspan.fsperm import set_owner_only
from healthspan.kdf import DbKey
from healthspan.keyparams import SIDECAR_SUFFIX, sidecar_path


class BackupError(Exception):
    """The backup could not be created or did not verify."""


@dataclass(frozen=True)
class BackupResult:
    database: Path
    sidecar: Path


def create_verified_backup(
    database_path: Path, key: DbKey, backup_dir: Path
) -> BackupResult:
    """Back up the database and its sidecar; publish only after verification.

    1. Native backup API into a ``.partial`` file (handles WAL correctly).
    2. Sidecar copied alongside, byte-compared against the original.
    3. Verify the copy: opens with the current key, full
       ``PRAGMA integrity_check`` and ``PRAGMA foreign_key_check`` pass, and
       ``schema_version`` matches the source (ADR-0038's definition, adopted
       by ADR-0028's pre-rekey check).
    4. Atomic rename to the final timestamped names.
    On any failure the partial files are deleted and nothing is published.
    """
    source_sidecar = sidecar_path(database_path)
    if not source_sidecar.is_file():
        raise BackupError(
            f"sidecar {source_sidecar} is missing; refusing to create a "
            "backup that could not be restored"
        )
    backup_dir.mkdir(parents=True, exist_ok=True)
    set_owner_only(backup_dir)

    stem = _backup_stem(backup_dir)
    partial_db = backup_dir / f"{stem}.db.partial"
    partial_sidecar = backup_dir / f"{stem}.db{SIDECAR_SUFFIX}.partial"
    try:
        source_version = _copy_database(database_path, key, partial_db)
        set_owner_only(partial_db)
        shutil.copyfile(source_sidecar, partial_sidecar)
        set_owner_only(partial_sidecar)
        if partial_sidecar.read_bytes() != source_sidecar.read_bytes():
            raise BackupError("sidecar copy does not match the original")
        _verify_copy(partial_db, key, source_version)
    except BaseException:
        partial_db.unlink(missing_ok=True)
        partial_sidecar.unlink(missing_ok=True)
        raise

    final_db = backup_dir / f"{stem}.db"
    final_sidecar = backup_dir / f"{stem}.db{SIDECAR_SUFFIX}"
    partial_db.rename(final_db)
    partial_sidecar.rename(final_sidecar)
    set_owner_only(final_db)
    set_owner_only(final_sidecar)
    return BackupResult(database=final_db, sidecar=final_sidecar)


def list_backups(backup_dir: Path) -> list[Path]:
    """Published backup database files in the directory, oldest first.

    A published backup is a ``healthspan-<stamp>.db`` file; the timestamped
    stem sorts chronologically, so a name sort is a time sort. ``.partial``
    files (a run in flight or a crashed one) and the ``.keyparams`` sidecars
    are excluded — only fully published database files are returned.
    """
    if not backup_dir.is_dir():
        return []
    return sorted(p for p in backup_dir.glob("healthspan-*.db") if p.is_file())


def latest_backup(backup_dir: Path) -> Path:
    """The newest published backup database (``db restore --latest``)."""
    backups = list_backups(backup_dir)
    if not backups:
        raise BackupError(
            f"no published backups found in {backup_dir}; nothing to restore "
            "(give an explicit backup file instead of --latest)"
        )
    return backups[-1]


def prune_backups(backup_dir: Path, retention_count: int) -> list[Path]:
    """Delete verified backups beyond ``retention_count``, oldest first.

    Each pruned database takes its ``.keyparams`` sidecar with it. Runs only
    after a successful publish (ADR-0038: a failing pipeline must never eat
    the good copies it is failing to replace), so the caller sequences it
    after :func:`create_verified_backup` returns. Returns the pruned database
    paths (empty when nothing aged out).
    """
    backups = list_backups(backup_dir)
    if len(backups) <= retention_count:
        return []
    pruned: list[Path] = []
    for database in backups[: len(backups) - retention_count]:
        database.unlink(missing_ok=True)
        sidecar_path(database).unlink(missing_ok=True)
        pruned.append(database)
    return pruned


def _backup_stem(backup_dir: Path) -> str:
    base = datetime.now(UTC).strftime("healthspan-%Y%m%dT%H%M%SZ")
    stem = base
    n = 1
    while any(backup_dir.glob(f"{stem}.db*")):
        n += 1
        stem = f"{base}-{n}"
    return stem


def _copy_database(database_path: Path, key: DbKey, target_path: Path) -> int | None:
    """Native-backup the live file into ``target_path``; return its version."""
    try:
        source = db.connect(database_path, key)
    except db.DatabaseError as exc:
        raise BackupError(str(exc)) from exc
    try:
        source_version = db.schema_version(source)
        target = db.open_backup_target(target_path, key)
        try:
            source.backup(target)
        finally:
            target.close()
    except (sqlcipher3.Error, db.DatabaseError) as exc:
        raise BackupError(f"backup copy failed: {exc}") from exc
    finally:
        source.close()
    return source_version


def _verify_copy(copy_path: Path, key: DbKey, source_version: int | None) -> None:
    try:
        conn = db.connect(copy_path, key)
    except db.DatabaseError as exc:
        raise BackupError(f"backup does not open with the current key: {exc}") from exc
    try:
        if not db.integrity_ok(conn):
            raise BackupError("backup failed PRAGMA integrity_check")
        if not db.foreign_key_ok(conn):
            raise BackupError("backup failed PRAGMA foreign_key_check")
        copy_version = db.schema_version(conn)
        if copy_version != source_version:
            raise BackupError(
                f"backup schema_version {copy_version!r} does not match "
                f"source {source_version!r}"
            )
    finally:
        conn.close()
