"""CLI surface for the sanctioned direct-database commands (ADR-0006).

``healthspan db migrate``, ``db backup``, and ``db restore`` — the only
paths besides the Core Service allowed to open the database directly
(ADR-0006). Backup and restore run the verify-then-publish and
verify-then-install pipelines from :mod:`healthspan.backup` and
:mod:`healthspan.restore`; ADR-0038's rule that these refuse while Core
Service is up is enforced by the ADR-0042 advisory lock, which arrives
with process supervision in a later phase (in Phase 1 the CLI is the sole
database opener).
"""

from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer

from healthspan import db, keychain, migrate, restore, rotation
from healthspan.backup import (
    BackupError,
    create_verified_backup,
    latest_backup,
    prune_backups,
)
from healthspan.cli_support import fail, load_config_or_exit
from healthspan.fsperm import PermissionSetError
from healthspan.keyparams import KeyParamsError

db_app = typer.Typer(
    help="Sanctioned direct-database commands: migrate, backup, restore.",
    no_args_is_help=True,
)


@db_app.command("migrate")
def db_migrate(ctx: typer.Context) -> None:
    """Apply pending schema migrations to bring the database current."""
    cfg = load_config_or_exit(ctx)
    passphrase = typer.prompt("Master passphrase", hide_input=True)
    unlocked = _run(lambda: rotation.unlock(cfg, passphrase))
    try:
        run = _run(
            lambda: migrate.migrate_database(unlocked.database_path, unlocked.key)
        )
    finally:
        unlocked.key.zeroize()

    if not run.applied:
        if run.final_version is None:
            typer.echo("No migrations found; the database has no schema.")
        else:
            typer.echo(
                f"Database is already up to date at schema version {run.final_version}."
            )
        return
    versions = ", ".join(str(v) for v in run.applied)
    typer.echo(
        f"Applied {len(run.applied)} migration(s) [{versions}]; "
        f"schema is now at version {run.final_version}."
    )


@db_app.command("backup")
def db_backup(ctx: typer.Context) -> None:
    """Create a verified backup of the database and prune old ones."""
    cfg = load_config_or_exit(ctx)
    passphrase = typer.prompt("Master passphrase", hide_input=True)
    unlocked = _run(lambda: rotation.unlock(cfg, passphrase))
    try:
        result = _run(
            lambda: create_verified_backup(
                unlocked.database_path, unlocked.key, cfg.backup.directory
            )
        )
    finally:
        unlocked.key.zeroize()
    typer.echo(f"Verified backup created:  {result.database}")
    typer.echo(f"Sidecar copied alongside: {result.sidecar}")
    pruned = _run(
        lambda: prune_backups(cfg.backup.directory, cfg.backup.retention_count)
    )
    if pruned:
        typer.echo(
            f"Pruned {len(pruned)} old backup(s) beyond the retention count "
            f"of {cfg.backup.retention_count}."
        )


@db_app.command("restore")
def db_restore(
    ctx: typer.Context,
    backup_file: Annotated[
        Path | None,
        typer.Argument(
            help="The backup database file to restore (omit with --latest)."
        ),
    ] = None,
    latest: Annotated[
        bool,
        typer.Option(
            "--latest",
            help="Restore the newest backup in the configured backup directory.",
        ),
    ] = False,
) -> None:
    """Install a verified backup as the live database (offline, ADR-0038)."""
    cfg = load_config_or_exit(ctx)
    if latest and backup_file is not None:
        raise fail("give either a backup file or --latest, not both")
    if not latest and backup_file is None:
        raise fail("specify a backup file to restore, or --latest")
    source = backup_file or _run(lambda: latest_backup(cfg.backup.directory))

    typer.echo(f"Restoring {source} to {cfg.database.path}.")
    passphrase = typer.prompt("Master passphrase", hide_input=True)
    key = _run(lambda: restore.derive_backup_key(source, passphrase))
    try:
        result = _run(
            lambda: restore.restore_database(
                source,
                cfg.database.path,
                key,
                target_version=migrate.target_version(),
            )
        )
    finally:
        key.zeroize()

    version = "none" if result.restored_version is None else result.restored_version
    typer.echo(f"Restored database at schema version {version}: {result.database}")
    if result.displaced is not None:
        typer.echo(
            f"Previous live database moved aside to {result.displaced} "
            "(ciphertext; delete it once you are sure the restore is good)."
        )
    if result.needs_migration:
        typer.echo(
            "The restored schema is older than this build. Run "
            "'healthspan db migrate' to bring it forward (the launcher will "
            "also migrate on next start)."
        )


def _run[T](operation: Callable[[], T]) -> T:
    try:
        return operation()
    except (
        rotation.RotationError,
        migrate.MigrationError,
        restore.RestoreError,
        BackupError,
        db.DatabaseError,
        keychain.KeychainError,
        KeyParamsError,
        PermissionSetError,
        OSError,
    ) as exc:
        raise fail(str(exc)) from exc
