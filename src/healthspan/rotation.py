"""Key rotation and mode conversion (ADR-0028).

The shared mechanics all rekey operations follow: verify the current
credentials by opening the database, take a mandatory verified backup
(skippable only by explicit expert flag), stage the new sidecar durably,
``PRAGMA rekey``, then atomically install the staged sidecar — with the
parameter-upgrade ride-along and the mode-aware salt semantics ADR-0028
specifies. The staging order means no failure can leave the database
encrypted under credentials no file on disk records.
"""

import contextlib
import os
from dataclasses import dataclass, replace
from pathlib import Path

import sqlcipher3

from healthspan import db, keychain
from healthspan.backup import create_verified_backup
from healthspan.config import Config
from healthspan.kdf import DbKey, derive_db_key, generate_secret_key
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
    """Current credentials, verified against the live database.

    Carries the verified derived key so the rekey step does not pay a
    second Argon2id run; the rekey flow zeroizes it when done.
    """

    database_path: Path
    sidecar: Path
    params: KeyParams
    salt: bytes  # secret key (two-factor) or sidecar salt (passphrase-only)
    key: DbKey


@dataclass(frozen=True)
class RotationResult:
    mode: KeyMode
    new_secret_key: bytes | None  # set when a new Recovery Kit is due
    old_secret_key: bytes | None  # set when a final kit for the old key is offered
    backup_database: Path | None
    # Non-fatal keychain trouble the CLI must surface prominently; when set
    # alongside new_secret_key, the rendered kit is the ONLY copy of the key.
    keychain_warning: str | None = None


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
        key.zeroize()
        message = str(exc)
        pending = sidecar.with_name(sidecar.name + ".pending")
        if pending.is_file():
            message += (
                f" Note: {pending} exists - an interrupted rotation may have "
                "rekeyed the database without installing its new sidecar; "
                "if so, replace the sidecar with the pending file and retry."
            )
        raise RotationError(message) from exc
    return Unlocked(
        database_path=database_path,
        sidecar=sidecar,
        params=params,
        salt=salt,
        key=key,
    )


def change_passphrase(
    cfg: Config,
    unlocked: Unlocked,
    new_passphrase: str,
    *,
    backup: bool = True,
) -> RotationResult:
    """New passphrase. Two-factor: secret key unchanged. Passphrase-only:
    the sidecar salt regenerates in the same rekey (ADR-0028)."""
    if unlocked.params.mode is KeyMode.TWO_FACTOR:
        new_salt = unlocked.salt
    else:
        new_salt = generate_secret_key()
    new_params = _next_params(unlocked.params, new_salt)
    backup_path = _rekey(
        cfg, unlocked, new_passphrase, new_salt, new_params, backup=backup
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
    new_params = _next_params(unlocked.params, new_salt)
    backup_path = _rekey(cfg, unlocked, passphrase, new_salt, new_params, backup=backup)
    if new_params.mode is not KeyMode.TWO_FACTOR:
        return RotationResult(
            mode=new_params.mode,
            new_secret_key=None,
            old_secret_key=None,
            backup_database=backup_path,
        )
    # No final kit for the outgoing key: two-factor rotate-secret-key keeps the
    # same mode, and ADR-0028's non-retroactivity guidance is to RETAIN the
    # existing kit (it still opens pre-rotation backups). The CLI message says
    # so. Rendering a fresh old-key kit here is a convert-mode-only behavior
    # (ADR-0028/ADR-0033); adding it to rotate would extend those ADRs.
    return RotationResult(
        mode=new_params.mode,
        new_secret_key=new_salt,
        old_secret_key=None,
        backup_database=backup_path,
        keychain_warning=_store_secret(new_salt),
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
    new_params = _next_params(unlocked.params, new_salt, mode=target)
    backup_path = _rekey(cfg, unlocked, passphrase, new_salt, new_params, backup=backup)
    if target is KeyMode.TWO_FACTOR:
        return RotationResult(
            mode=target,
            new_secret_key=new_salt,
            old_secret_key=None,
            backup_database=backup_path,
            keychain_warning=_store_secret(new_salt),
        )
    # Downgrade: the keychain entry goes only after the rekey succeeded;
    # the old secret key is offered back for a final Recovery Kit render
    # (old backups are still ciphertext under it).
    warning: str | None = None
    try:
        keychain.delete_secret_key()
    except keychain.KeychainError as exc:
        warning = (
            f"could not remove the old keychain entry ({exc}); the stale "
            "entry is harmless but should be deleted by hand (service "
            f"'{keychain.SERVICE}', name '{keychain.SECRET_KEY_ENTRY}')."
        )
    return RotationResult(
        mode=target,
        new_secret_key=None,
        old_secret_key=unlocked.salt,
        backup_database=backup_path,
        keychain_warning=warning,
    )


def _store_secret(new_salt: bytes) -> str | None:
    """Store the new secret key; a failure must never hide the key.

    By the time this runs the database is already rekeyed, so the caller
    always renders the Recovery Kit from the returned result — a store
    failure downgrades to a prominent warning instead of an exception
    that would discard the only copy of the key.
    """
    try:
        keychain.store_secret_key(new_salt)
    except keychain.KeychainError as exc:
        return (
            f"the OS keychain store FAILED ({exc}). The Recovery Kit below "
            "is the ONLY copy of the new secret key - print or save it NOW, "
            "then either add the keychain entry by hand (service "
            f"'{keychain.SERVICE}', name '{keychain.SECRET_KEY_ENTRY}', the "
            "Base32 string from the kit) or repair the keychain and run "
            "'healthspan keys rotate-secret-key' again."
        )
    return None


def _next_params(
    params: KeyParams, new_salt: bytes, mode: KeyMode | None = None
) -> KeyParams:
    """The post-rotation sidecar record: mode-aware salt semantics
    (ADR-0028) plus the parameter-upgrade ride-along."""
    target = mode if mode is not None else params.mode
    stored_salt = new_salt if target is KeyMode.PASSPHRASE_ONLY else None
    return replace(
        params.with_upgraded_parameters(),
        mode=target,
        salt=stored_salt,
        rotated_utc=utc_now_iso(),
    )


def _rekey(
    cfg: Config,
    unlocked: Unlocked,
    new_passphrase: str,
    new_salt: bytes,
    new_params: KeyParams,
    *,
    backup: bool,
) -> Path | None:
    """Shared rekey mechanics, ordered for durability.

    The new sidecar is staged to ``<sidecar>.pending`` BEFORE the rekey:
    every failure mode that can refuse a write (disk full, ACLs) fires
    while the database still opens with the old credentials, and a crash
    between the rekey and the atomic install leaves the new parameters on
    disk in the pending file rather than only in memory.
    """
    new_key = derive_db_key(new_passphrase, new_salt, new_params)
    pending = unlocked.sidecar.with_name(unlocked.sidecar.name + ".pending")
    try:
        backup_path: Path | None = None
        if backup:
            result = create_verified_backup(
                unlocked.database_path, unlocked.key, cfg.backup.directory
            )
            backup_path = result.database
        try:
            write_keyparams(pending, new_params)
            conn = db.connect(unlocked.database_path, unlocked.key)
        except BaseException:
            pending.unlink(missing_ok=True)  # nothing rekeyed yet; no trace
            raise
        try:
            db.rekey(conn, new_key)
        except BaseException:
            # rekey failed: the database still opens with the OLD credentials,
            # so the pending sidecar (new params) records a state that never
            # happened — remove it.
            pending.unlink(missing_ok=True)
            raise
        finally:
            _close_quietly(conn)
        # rekey succeeded: the pending sidecar is now the ONLY on-disk record
        # of the credentials the database is actually encrypted under. It must
        # survive every failure from here (a Ctrl-C, a failed os.replace) so
        # unlock()'s recovery hint can find it — never unlink it past this line.
        os.replace(pending, unlocked.sidecar)  # atomic same-directory swap
        return backup_path
    finally:
        unlocked.key.zeroize()
        new_key.zeroize()


def _close_quietly(conn: sqlcipher3.Connection) -> None:
    # After a successful rekey the connection close must not be able to raise
    # into _rekey's cleanup (which would delete the pending sidecar) or mask
    # the rekey; swallow its errors. A KeyboardInterrupt still propagates, but
    # by then the pending sidecar is preserved and os.replace is the next line.
    with contextlib.suppress(Exception):
        db.close(conn)
