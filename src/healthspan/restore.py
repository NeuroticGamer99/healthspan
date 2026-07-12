"""Verified restore: install a backup as the live database (ADR-0038).

The mirror image of :mod:`healthspan.backup`'s verify-then-publish
pipeline — here it is **verify-then-install**, so nothing unverified can
ever take the live database's name. A backup is copied into the live
directory under a temporary name, verified against its own ``.keyparams``
sidecar (key-open, full ``integrity_check`` and ``foreign_key_check``,
schema-version policy), and only then atomically renamed into place. The
displaced live file is aside-renamed, never deleted — it is ciphertext,
and a mistaken restore that destroyed the only live copy is exactly the
failure this pipeline exists to prevent.

Restore is offline-only (ADR-0038): there is no in-service restore job,
and the live file cannot be swapped under a running Core Service. The
ADR-0042 advisory lock the ADR prescribes arrives with process
supervision in a later phase; in Phase 1 the CLI is the sole database
opener, so restore runs without it.
"""

import contextlib
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from healthspan import db, keychain
from healthspan.fsperm import set_owner_only
from healthspan.kdf import DbKey, derive_db_key
from healthspan.keyparams import (
    RECOVERY_GUIDANCE,
    KeyMode,
    read_keyparams,
    sidecar_path,
)

_RESTORING_SUFFIX = ".restoring"


class RestoreError(Exception):
    """The backup could not be verified or installed; the live file is
    left untouched."""


@dataclass(frozen=True)
class RestoreResult:
    database: Path
    sidecar: Path
    restored_version: int | None
    needs_migration: bool  # the restored schema is older than this build's
    displaced: Path | None  # the aside-renamed previous live database, if any


def derive_backup_key(backup_database: Path, passphrase: str) -> DbKey:
    """Derive the key that opens ``backup_database`` from its own sidecar.

    Two-factor backups need the secret key from the OS keychain (put there
    by ``healthspan init`` or ``init --restore``); passphrase-only backups
    carry their salt in the sidecar. The key is validated later by opening
    the verified copy — deriving here never touches the live database.
    """
    params = read_keyparams(sidecar_path(backup_database))
    if params.mode is KeyMode.TWO_FACTOR:
        salt = keychain.load_secret_key()
    else:
        if params.salt is None:  # pragma: no cover - read_keyparams invariant
            raise RestoreError(
                f"{sidecar_path(backup_database)}: passphrase-only sidecar has no salt"
            )
        salt = params.salt
    return derive_db_key(passphrase, salt, params)


def restore_database(
    backup_database: Path,
    live_database_path: Path,
    key: DbKey,
    *,
    target_version: int | None,
) -> RestoreResult:
    """Install ``backup_database`` as the live database, verify-then-install.

    ``key`` must be the key that opens the backup (see
    :func:`derive_backup_key`). ``target_version`` is the highest schema
    version this build ships (:func:`healthspan.migrate.target_version`);
    it drives the version policy below. The caller zeroizes ``key``.
    """
    backup_sidecar = sidecar_path(backup_database)
    if not backup_database.is_file():
        raise RestoreError(f"backup {backup_database} does not exist")
    if not backup_sidecar.is_file():
        raise RestoreError(
            f"the backup's key-parameter sidecar {backup_sidecar} is missing; "
            f"the backup cannot be restored without it. {RECOVERY_GUIDANCE}"
        )

    live_database_path.parent.mkdir(parents=True, exist_ok=True)
    restoring_db = live_database_path.with_name(
        live_database_path.name + _RESTORING_SUFFIX
    )
    restoring_sidecar = sidecar_path(restoring_db)
    try:
        shutil.copyfile(backup_database, restoring_db)
        set_owner_only(restoring_db)
        shutil.copyfile(backup_sidecar, restoring_sidecar)
        set_owner_only(restoring_sidecar)
        if restoring_sidecar.read_bytes() != backup_sidecar.read_bytes():
            raise RestoreError("sidecar copy does not match the backup's")
        restored_version = _verify_copy(restoring_db, key)
        needs_migration = _check_version_policy(restored_version, target_version)
    except BaseException:
        restoring_db.unlink(missing_ok=True)
        restoring_sidecar.unlink(missing_ok=True)
        raise

    displaced = _install(restoring_db, restoring_sidecar, live_database_path)
    return RestoreResult(
        database=live_database_path,
        sidecar=sidecar_path(live_database_path),
        restored_version=restored_version,
        needs_migration=needs_migration,
        displaced=displaced,
    )


def _verify_copy(copy_path: Path, key: DbKey) -> int | None:
    """ADR-0038 verification, restore-adapted: key-open + integrity +
    foreign keys; the ``schema_version`` clause becomes the version policy."""
    try:
        conn = db.connect(copy_path, key)
    except db.DatabaseError as exc:
        raise RestoreError(
            f"the backup does not open with the derived key: {exc}"
        ) from exc
    try:
        if not db.integrity_ok(conn):
            raise RestoreError("the backup failed PRAGMA integrity_check")
        if not db.foreign_key_ok(conn):
            raise RestoreError("the backup failed PRAGMA foreign_key_check")
        return db.schema_version(conn)
    finally:
        # Plain close, not db.close(): db.close runs PRAGMA optimize, a write,
        # which would mutate the just-verified copy before it is installed —
        # the installed bytes must be exactly the bytes verified.
        conn.close()


def _check_version_policy(
    restored_version: int | None, target_version: int | None
) -> bool:
    """ADR-0038 version policy. Returns whether a migration is still due.

    - **newer** than this build ships: refuse — a backup made by newer code
      must not be installed under the current binary.
    - **older** (or no schema at all): proceed; the next launcher migration
      phase or an explicit ``healthspan db migrate`` brings it forward.
    - **equal**: proceed.
    """
    if (
        restored_version is not None
        and target_version is not None
        and restored_version > target_version
    ):
        raise RestoreError(
            f"the backup is at schema version {restored_version}, newer than "
            f"this build supports (version {target_version}). Upgrade "
            "healthspan first; nothing was changed and the backup is intact."
        )
    if target_version is None:
        return False
    return restored_version is None or restored_version < target_version


def _install(
    restoring_db: Path, restoring_sidecar: Path, live_database_path: Path
) -> Path | None:
    """Aside-rename any live file, then move the verified copy into place.

    The current live database, its sidecar, and any ``-wal``/``-shm``
    companions move together to ``<name>.pre-restore-<stamp>`` — a stale
    ``-wal`` must never pair with the restored file. On a brand-new machine
    there is nothing to displace and ``displaced`` is ``None``.

    The two renames are not one atomic operation, so a failure between them
    (a transient lock on the sidecar target, say) would leave the live
    database installed without its sidecar — unopenable. On any failure we
    roll the displaced originals back so the live file is genuinely
    untouched (the ADR-0038 guarantee); the backup source is intact for a
    re-run.
    """
    live_sidecar = sidecar_path(live_database_path)
    moves = _displace_live(live_database_path)
    try:
        restoring_db.replace(live_database_path)
        restoring_sidecar.replace(live_sidecar)
    except BaseException:
        _rollback_displacement(moves)
        # Clear the restoring-copy leftovers (restoring_db may already have
        # been consumed by its own replace before the failure) so a failed
        # install leaves no ``.restoring`` files behind, like the copy/verify
        # stage's cleanup does.
        restoring_db.unlink(missing_ok=True)
        restoring_sidecar.unlink(missing_ok=True)
        raise
    set_owner_only(live_database_path)
    set_owner_only(live_sidecar)
    return next(
        (aside for original, aside in moves if original == live_database_path),
        None,
    )


def _displace_live(live_database_path: Path) -> list[tuple[Path, Path]]:
    """Move the live database and its companions aside.

    Returns the ``(original, aside)`` moves in application order so
    :func:`_rollback_displacement` can reverse them if the install fails.
    """
    suffix = _pre_restore_suffix(live_database_path)
    companions = [
        live_database_path,
        sidecar_path(live_database_path),
        live_database_path.with_name(live_database_path.name + "-wal"),
        live_database_path.with_name(live_database_path.name + "-shm"),
    ]
    moves: list[tuple[Path, Path]] = []
    for companion in companions:
        if not companion.exists():
            continue
        target = companion.with_name(companion.name + suffix)
        companion.replace(target)
        moves.append((companion, target))
    return moves


def _rollback_displacement(moves: list[tuple[Path, Path]]) -> None:
    # Reverse the aside-renames; ``replace`` overwrites a partially installed
    # live file with its original. Best-effort — a rollback failure must not
    # mask the original install error, and the aside files are never deleted,
    # so a manual recovery always remains possible.
    for original, aside in reversed(moves):
        with contextlib.suppress(OSError):
            aside.replace(original)


def _pre_restore_suffix(live_database_path: Path) -> str:
    base = datetime.now(UTC).strftime(".pre-restore-%Y%m%dT%H%M%SZ")
    suffix = base
    n = 1
    while any(live_database_path.parent.glob(f"{live_database_path.name}*{suffix}")):
        n += 1
        suffix = f"{base}-{n}"
    return suffix
