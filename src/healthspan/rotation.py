"""Key rotation and mode conversion (ADR-0028).

The shared mechanics all rekey operations follow: verify the current
credentials by opening the database, take a mandatory verified backup
(skippable only by explicit expert flag), derive the new key, ``PRAGMA
rekey``, rewrite the sidecar — with the parameter-upgrade ride-along and
the mode-aware salt semantics ADR-0028 specifies.
"""

from dataclasses import dataclass, replace
from pathlib import Path

from healthspan import db, keychain
from healthspan.backup import create_verified_backup
from healthspan.config import Config
from healthspan.kdf import derive_db_key, generate_secret_key
from healthspan.keyparams import (
    KeyMode,
    KeyParams,
    read_keyparams,
    sidecar_path,
    utc_now_iso,
    write_keyparams,
)


class RotationError(Exception):
    """A rotation or conversion could not proceed; the database is unchanged."""


@dataclass(frozen=True)
class Unlocked:
    """Current credentials, verified against the live database."""

    database_path: Path
    sidecar: Path
    params: KeyParams
    salt: bytes  # secret key (two-factor) or sidecar salt (passphrase-only)


@dataclass(frozen=True)
class RotationResult:
    mode: KeyMode
    new_secret_key: bytes | None  # set when a new Recovery Kit is due
    old_secret_key: bytes | None  # set when a final kit for the old key is offered
    backup_database: Path | None


def unlock(cfg: Config, passphrase: str) -> Unlocked:
    """Read the sidecar, fetch the mode's salt, and verify the passphrase."""
    database_path = cfg.database.path
    sidecar = sidecar_path(database_path)
    params = read_keyparams(sidecar)
    if params.mode is KeyMode.TWO_FACTOR:
        salt = keychain.load_secret_key()
    else:
        if params.salt is None:  # pragma: no cover - read_keyparams invariant
            raise RotationError(f"{sidecar}: passphrase-only sidecar has no salt")
        salt = params.salt
    key = derive_db_key(passphrase, salt, params)
    try:
        conn = db.connect(database_path, key)
        db.close(conn)
    except db.DatabaseError as exc:
        raise RotationError(str(exc)) from exc
    finally:
        key.zeroize()
    return Unlocked(
        database_path=database_path, sidecar=sidecar, params=params, salt=salt
    )


def change_passphrase(
    cfg: Config,
    unlocked: Unlocked,
    old_passphrase: str,
    new_passphrase: str,
    *,
    backup: bool = True,
) -> RotationResult:
    """New passphrase. Two-factor: secret key unchanged. Passphrase-only:
    the sidecar salt regenerates in the same rekey (ADR-0028)."""
    if unlocked.params.mode is KeyMode.TWO_FACTOR:
        new_salt = unlocked.salt
        new_params = _rotated(unlocked.params)
    else:
        new_salt = generate_secret_key()
        new_params = replace(_rotated(unlocked.params), salt=new_salt)
    backup_path = _rekey(
        cfg,
        unlocked,
        old_passphrase,
        new_passphrase,
        new_salt,
        new_params,
        backup=backup,
    )
    return RotationResult(
        mode=new_params.mode,
        new_secret_key=None,
        old_secret_key=None,
        backup_database=backup_path,
    )


def rotate_secret_key(
    cfg: Config, unlocked: Unlocked, passphrase: str, *, backup: bool = True
) -> RotationResult:
    """New secret key (two-factor) or new sidecar salt (passphrase-only);
    passphrase unchanged."""
    new_salt = generate_secret_key()
    if unlocked.params.mode is KeyMode.TWO_FACTOR:
        new_params = _rotated(unlocked.params)
    else:
        new_params = replace(_rotated(unlocked.params), salt=new_salt)
    backup_path = _rekey(
        cfg, unlocked, passphrase, passphrase, new_salt, new_params, backup=backup
    )
    new_secret: bytes | None = None
    if new_params.mode is KeyMode.TWO_FACTOR:
        keychain.store_secret_key(new_salt)
        new_secret = new_salt
    return RotationResult(
        mode=new_params.mode,
        new_secret_key=new_secret,
        old_secret_key=None,
        backup_database=backup_path,
    )


def convert_mode(
    cfg: Config,
    unlocked: Unlocked,
    passphrase: str,
    target: KeyMode,
    *,
    backup: bool = True,
) -> RotationResult:
    """In-place mode conversion (ADR-0028); passphrase unchanged."""
    if unlocked.params.mode is target:
        raise RotationError(f"database is already in {target.value} mode")
    new_salt = generate_secret_key()
    if target is KeyMode.TWO_FACTOR:
        new_params = replace(_rotated(unlocked.params), mode=target, salt=None)
    else:
        new_params = replace(_rotated(unlocked.params), mode=target, salt=new_salt)
    backup_path = _rekey(
        cfg, unlocked, passphrase, passphrase, new_salt, new_params, backup=backup
    )
    if target is KeyMode.TWO_FACTOR:
        keychain.store_secret_key(new_salt)
        return RotationResult(
            mode=target,
            new_secret_key=new_salt,
            old_secret_key=None,
            backup_database=backup_path,
        )
    # Downgrade: the keychain entry goes only after the rekey succeeded;
    # the old secret key is offered back for a final Recovery Kit render
    # (old backups are still ciphertext under it).
    keychain.delete_secret_key()
    return RotationResult(
        mode=target,
        new_secret_key=None,
        old_secret_key=unlocked.salt,
        backup_database=backup_path,
    )


def _rotated(params: KeyParams) -> KeyParams:
    # Parameter upgrades ride along with every rotation (ADR-0028).
    return replace(params.with_upgraded_parameters(), rotated_utc=utc_now_iso())


def _rekey(
    cfg: Config,
    unlocked: Unlocked,
    old_passphrase: str,
    new_passphrase: str,
    new_salt: bytes,
    new_params: KeyParams,
    *,
    backup: bool,
) -> Path | None:
    old_key = derive_db_key(old_passphrase, unlocked.salt, unlocked.params)
    new_key = derive_db_key(new_passphrase, new_salt, new_params)
    try:
        backup_path: Path | None = None
        if backup:
            result = create_verified_backup(
                unlocked.database_path, old_key, cfg.backup.directory
            )
            backup_path = result.database
        conn = db.connect(unlocked.database_path, old_key)
        try:
            db.rekey(conn, new_key)
        finally:
            db.close(conn)
        write_keyparams(unlocked.sidecar, new_params)
        return backup_path
    finally:
        old_key.zeroize()
        new_key.zeroize()
