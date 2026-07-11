"""CLI surface for the sanctioned direct-database commands (ADR-0006).

``healthspan db migrate`` today; ``healthspan db backup`` / ``restore``
arrive with WI-4. These are the only paths besides the Core Service allowed
to open the database directly (ADR-0006).
"""

from collections.abc import Callable

import typer

from healthspan import db, keychain, migrate, rotation
from healthspan.cli_support import fail, load_config_or_exit
from healthspan.keyparams import KeyParamsError

db_app = typer.Typer(
    help="Sanctioned direct-database commands: schema migrations.",
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


def _run[T](operation: Callable[[], T]) -> T:
    try:
        return operation()
    except (
        rotation.RotationError,
        migrate.MigrationError,
        db.DatabaseError,
        keychain.KeychainError,
        KeyParamsError,
        OSError,
    ) as exc:
        raise fail(str(exc)) from exc
