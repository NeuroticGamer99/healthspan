"""``healthspan service`` — run the Core Service (ADR-0006/0039/0049).

Direct-start only in Phase 2: the launcher, process supervision, and
full-auto-unlock are later phases (ADR-0049). ``service start`` runs the
Core Service in the foreground; Ctrl-C stops it.
"""

from pathlib import Path
from typing import Annotated

import typer

from healthspan import db, keychain, rotation
from healthspan.cli_support import fail, load_config_or_exit
from healthspan.keyparams import KeyParamsError
from healthspan.service import ServiceStartupError, start_service

service_app = typer.Typer(
    help="Run the Core Service (the REST API every other client speaks to).",
    no_args_is_help=True,
)


@service_app.command("start")
def service_start(
    ctx: typer.Context,
    passphrase_file: Annotated[
        Path | None,
        typer.Option(
            "--passphrase-file",
            help=(
                "Read the master passphrase from this OS-secret file instead "
                "of prompting (ADR-0039 channel c)."
            ),
        ),
    ] = None,
) -> None:
    """Start the Core Service in the foreground (blocks until Ctrl-C).

    Collects the master passphrase (TTY, stdin, or --passphrase-file),
    verifies the schema, holds the single-instance lock, and serves the REST
    API. Refuses to start if migrations are pending (run 'healthspan db
    migrate' first) or another instance already holds the database.
    """
    cfg = load_config_or_exit(ctx)
    try:
        start_service(cfg, passphrase_file)
    except (
        ServiceStartupError,
        rotation.RotationError,
        db.DatabaseError,
        keychain.KeychainError,
        KeyParamsError,
        OSError,
    ) as exc:
        raise fail(str(exc)) from exc
