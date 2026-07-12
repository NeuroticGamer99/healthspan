"""CLI surface for provisioning and key management (ADR-0013/0028/0033).

``healthspan init`` and the ``healthspan keys`` group. Prompting, warnings,
and rendering live here; the mechanics live in :mod:`healthspan.provisioning`
and :mod:`healthspan.rotation`.
"""

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer

from healthspan import db, keychain, recovery_kit, rotation
from healthspan.backup import BackupError
from healthspan.cli_support import fail, load_config_or_exit
from healthspan.config import Config
from healthspan.fsperm import PermissionSetError
from healthspan.keyparams import KeyMode, KeyParamsError
from healthspan.provisioning import (
    PASSPHRASE_ADVISORY_MIN,
    InitError,
    initialize,
    restore_credentials,
)

keys_app = typer.Typer(
    help="Manage the database key: rotation, mode conversion, Recovery Kit.",
    no_args_is_help=True,
)

_NON_RETROACTIVITY_WARNING = (
    "Rotation is not retroactive: existing backups still open with the old "
    "credentials. Either re-create backups under the new credentials, or "
    "retain the old credentials until old backups have aged out (ADR-0028)."
)


def _prompt_new_passphrase() -> str:
    """Prompt twice, applying the advisory policy: warn short, never refuse."""
    while True:
        passphrase = typer.prompt(
            "New master passphrase", hide_input=True, confirmation_prompt=True
        )
        if len(passphrase) >= PASSPHRASE_ADVISORY_MIN:
            return passphrase
        typer.echo(
            f"warning: passphrases shorter than {PASSPHRASE_ADVISORY_MIN} "
            "characters are easier to guess; a longer phrase of several "
            "words is stronger and easier to remember."
        )
        if typer.confirm("Use this short passphrase anyway?", default=False):
            return passphrase


def _echo_kit(secret_key: bytes, output: Path | None) -> None:
    """Show the kit, degrading gracefully — never lose the key to I/O.

    The terminal render comes first so a later file-write failure can
    never discard the only copy of a secret key; a stdout encoding that
    cannot carry the QR's half-block cells gets a QR-free render instead
    of a UnicodeEncodeError.
    """
    kit = recovery_kit.render_kit(secret_key)
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        kit.encode(encoding)
    except UnicodeEncodeError:
        kit = recovery_kit.render_kit(secret_key, include_qr=False)
    typer.echo(kit)
    typer.echo(
        "Print this kit and store it in a safe place; it is shown only on "
        "request ('healthspan keys recovery-kit')."
    )
    if output is not None:
        try:
            written = recovery_kit.write_kit(secret_key, output)
        except (OSError, PermissionSetError) as exc:
            typer.echo(
                f"warning: could not write the Recovery Kit file ({exc}); "
                "the kit printed above is your copy.",
                err=True,
            )
            return
        typer.echo(f"Recovery Kit written to {written}")
        typer.echo(f"warning: {recovery_kit.OUTPUT_WARNING}")


def _echo_keychain_warning(warning: str | None) -> None:
    if warning is not None:
        typer.echo(f"WARNING: {warning}", err=True)


def _offer_final_old_kit(old_secret_key: bytes, output: Path | None) -> None:
    """Offer a final Recovery Kit for a retired secret key (ADR-0028).

    Old backups remain two-factor ciphertext requiring the outgoing key. The
    rotation/conversion is already committed, so an unanswered prompt (EOF in
    a scripted run) must default to SHOWING the kit — never silently discard
    the last copy of a key that still opens existing backups.
    """
    typer.echo(
        "Old backups are still two-factor ciphertext requiring the previous "
        "secret key, which is no longer in the keychain."
    )
    try:
        render_final = typer.confirm(
            "Render a final Recovery Kit for the OLD secret key?", default=True
        )
    except typer.Abort:
        typer.echo("(no answer; rendering the final kit for safety)")
        render_final = True
    if render_final:
        _echo_kit(old_secret_key, output)


def _announce_backup(backup_database: Path | None, skipped: bool) -> None:
    if skipped:
        typer.echo(
            "warning: --no-backup skips the mandatory verified pre-rekey "
            "backup; a failure during rekey can corrupt the database with "
            "no fresh copy to fall back on (ADR-0028)."
        )
    elif backup_database is not None:
        typer.echo(f"Verified backup created: {backup_database}")


_KitOutput = Annotated[
    Path | None,
    typer.Option(
        "--output",
        help=(
            "Also write the Recovery Kit to a file (deliberate digital "
            "copy; store only on encrypted storage)."
        ),
    ),
]
_NoBackup = Annotated[
    bool,
    typer.Option(
        "--no-backup",
        help="Expert: skip the mandatory verified pre-rekey backup.",
    ),
]


@keys_app.command("change-passphrase")
def keys_change_passphrase(ctx: typer.Context, no_backup: _NoBackup = False) -> None:
    """Set a new master passphrase; the secret key is unchanged."""
    cfg = load_config_or_exit(ctx)
    old_passphrase = typer.prompt("Current master passphrase", hide_input=True)
    unlocked = _unlock_or_exit(cfg, old_passphrase)
    new_passphrase = _prompt_new_passphrase()
    result = _run(
        lambda: rotation.change_passphrase(
            cfg, unlocked, new_passphrase, backup=not no_backup
        )
    )
    _announce_backup(result.backup_database, skipped=no_backup)
    typer.echo("Passphrase changed.")
    if result.mode is KeyMode.TWO_FACTOR:
        typer.echo(
            "Your printed Recovery Kit remains valid: it holds the secret "
            "key, and its passphrase line is handwritten - update it."
        )
    else:
        typer.echo(
            "The sidecar salt was regenerated in the same rekey, unlinking "
            "the new passphrase from any previously leaked sidecar."
        )
    typer.echo(_NON_RETROACTIVITY_WARNING)


@keys_app.command("rotate-secret-key")
def keys_rotate_secret_key(
    ctx: typer.Context, output: _KitOutput = None, no_backup: _NoBackup = False
) -> None:
    """Generate a new secret key; the passphrase is unchanged."""
    cfg = load_config_or_exit(ctx)
    passphrase = typer.prompt("Master passphrase", hide_input=True)
    unlocked = _unlock_or_exit(cfg, passphrase)
    if unlocked.params.mode is KeyMode.TWO_FACTOR:
        typer.echo(
            "This generates a new secret key and a new Recovery Kit. Keep your "
            "current kit too: its secret key stops opening the live database, "
            "but still opens backups made before this rotation (ADR-0028 "
            "non-retroactivity)."
        )
        typer.confirm("Continue?", default=False, abort=True)
    result = _run(
        lambda: rotation.rotate_secret_key(
            cfg, unlocked, passphrase, backup=not no_backup
        )
    )
    _announce_backup(result.backup_database, skipped=no_backup)
    if result.new_secret_key is not None:
        typer.echo("Secret key rotated.")
        _echo_keychain_warning(result.keychain_warning)
        _echo_kit(result.new_secret_key, output)
    else:
        typer.echo(
            "Passphrase-only mode: the sidecar salt was rotated and the "
            "database rekeyed. No keychain entry and no Recovery Kit exist "
            "in this mode - no second factor does. This invalidates "
            "precomputation against a leaked sidecar and revokes an "
            "exfiltrated derived key against future copies of the file; it "
            "cannot protect a database already copied (ADR-0028)."
        )
    typer.echo(_NON_RETROACTIVITY_WARNING)


@keys_app.command("convert-mode")
def keys_convert_mode(
    ctx: typer.Context,
    to: Annotated[
        str,
        typer.Option("--to", help="Target mode: two-factor or passphrase-only."),
    ],
    output: _KitOutput = None,
    no_backup: _NoBackup = False,
) -> None:
    """Convert between two-factor and passphrase-only in place."""
    cfg = load_config_or_exit(ctx)
    try:
        target = KeyMode(to)
    except ValueError:
        raise fail(
            f"unknown mode {to!r}; expected 'two-factor' or 'passphrase-only'"
        ) from None
    passphrase = typer.prompt("Master passphrase", hide_input=True)
    unlocked = _unlock_or_exit(cfg, passphrase)
    if target is KeyMode.PASSPHRASE_ONLY:
        typer.echo(
            "This DOWNGRADES protection to a single factor: the passphrase "
            "alone will unlock the database on any machine (ADR-0013)."
        )
        typer.confirm("Continue?", default=False, abort=True)
    result = _run(
        lambda: rotation.convert_mode(
            cfg, unlocked, passphrase, target, backup=not no_backup
        )
    )
    _announce_backup(result.backup_database, skipped=no_backup)
    typer.echo(f"Database converted to {target.value} mode; passphrase unchanged.")
    _echo_keychain_warning(result.keychain_warning)
    if result.new_secret_key is not None:
        _echo_kit(result.new_secret_key, output)
    if result.old_secret_key is not None:
        _offer_final_old_kit(result.old_secret_key, output)
    typer.echo(_NON_RETROACTIVITY_WARNING)


@keys_app.command("recovery-kit")
def keys_recovery_kit(ctx: typer.Context, output: _KitOutput = None) -> None:
    """Render the Recovery Kit for the secret key in the OS keychain."""
    load_config_or_exit(ctx)  # validates config; kit needs only the keychain
    try:
        secret_key = keychain.load_secret_key()
    except keychain.KeychainError as exc:
        raise fail(
            f"{exc} (in passphrase-only mode no Recovery Kit exists - "
            "no secret key does)"
        ) from exc
    _echo_kit(secret_key, output)


def _init_restore(cfg: Config, key_from_passphrase: bool) -> None:
    """Store a Recovery Kit secret key on a new machine (ADR-0038)."""
    if key_from_passphrase:
        raise fail(
            "--restore and --key-from-passphrase are incompatible: "
            "passphrase-only mode has no secret key to restore, and its "
            "backups carry their salt in the sidecar."
        )
    typer.echo("Restoring two-factor credentials from your Recovery Kit.")
    secret_key_text = typer.prompt(
        "Secret key from the Recovery Kit (Base32)", hide_input=True
    )
    result = _run(lambda: restore_credentials(cfg, secret_key_text))
    if result.replaced_existing:
        typer.echo("warning: an existing secret key in the keychain was replaced.")
    typer.echo("Secret key stored in the OS keychain.")
    if result.config_created:
        typer.echo(f"Config file created: {result.config_path}")
    typer.echo(
        "Next: 'healthspan db restore <backup-file>' (or --latest) to "
        "install your data from a backup."
    )


def init_command(
    ctx: typer.Context,
    key_from_passphrase: Annotated[
        bool,
        typer.Option(
            "--key-from-passphrase",
            help=(
                "Passphrase-only mode: single-factor, fully portable, no OS "
                "keychain (default is two-factor: secret key + passphrase)."
            ),
        ),
    ] = False,
    restore: Annotated[
        bool,
        typer.Option(
            "--restore",
            help=(
                "New-machine credential recovery: store the secret key from a "
                "Recovery Kit in the OS keychain (then 'healthspan db restore' "
                "installs the data). Provisions no database."
            ),
        ),
    ] = False,
    output: _KitOutput = None,
) -> None:
    """Initialize Healthspan: credentials, encrypted database, sidecar."""
    cfg = load_config_or_exit(ctx)
    if restore:
        if output is not None:
            raise fail(
                "--output has no effect with --restore: restore stores an "
                "existing key, it renders no new Recovery Kit."
            )
        _init_restore(cfg, key_from_passphrase)
        return
    mode = KeyMode.PASSPHRASE_ONLY if key_from_passphrase else KeyMode.TWO_FACTOR
    typer.echo(f"Initializing in {mode.value} mode.")
    if mode is KeyMode.PASSPHRASE_ONLY:
        typer.echo(
            "Passphrase-only provides single-factor protection: the "
            "passphrase alone unlocks the database (ADR-0013)."
        )
    passphrase = _prompt_new_passphrase()
    result = _run(lambda: initialize(cfg, passphrase, mode))
    typer.echo(f"Encrypted database created: {result.database_path}")
    typer.echo(f"Key parameters recorded:    {result.sidecar_path}")
    if result.secret_key is not None:
        _echo_kit(result.secret_key, output)
    typer.echo("Next: 'healthspan db migrate' to create the schema (Phase 1 WI-3).")


def _unlock_or_exit(cfg: Config, passphrase: str) -> rotation.Unlocked:
    return _run(lambda: rotation.unlock(cfg, passphrase))


def _run[T](operation: Callable[[], T]) -> T:
    try:
        return operation()
    except (
        rotation.RotationError,
        InitError,
        KeyParamsError,
        BackupError,
        keychain.KeychainError,
        db.DatabaseError,
        PermissionSetError,
        OSError,
    ) as exc:
        raise fail(str(exc)) from exc
