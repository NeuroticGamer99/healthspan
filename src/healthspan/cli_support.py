"""Shared CLI plumbing: per-invocation state and the fatal-error path.

Lives outside cli.py so command modules (cli.py, cli_keys.py) can share it
without importing each other.
"""

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import typer

from healthspan import permcheck
from healthspan.config import Config, ConfigError, load_config
from healthspan.locking import InstanceLock, InstanceLockHeldError


@dataclass
class AppState:
    """Per-invocation state shared from the root callback to subcommands."""

    config_flag: Path | None


def state(ctx: typer.Context) -> AppState:
    obj = ctx.obj
    if not isinstance(obj, AppState):  # pragma: no cover - Typer wiring invariant
        raise RuntimeError("CLI state missing; root callback did not run")
    return obj


def fail(message: str) -> typer.Exit:
    """Print a fatal error and return the Exit for the caller to raise."""
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(code=1)


def warn_cli(message: str) -> None:
    """Report a non-fatal finding on stderr, in load_config's format."""
    typer.echo(f"warning: {message}", err=True)


def load_config_or_exit(ctx: typer.Context) -> Config:
    try:
        return load_config(flag=state(ctx).config_flag)
    except ConfigError as exc:
        raise fail(str(exc)) from exc


@contextmanager
def exclusive_database_access(cfg: Config) -> Generator[None]:
    """Hold the single-instance lock for a direct-database command's duration.

    Every sanctioned direct-database command (``db migrate``/``backup``/
    ``restore`` and the ``keys`` rotation commands) opens the database
    outside the Core Service and must refuse while the Core Service is up —
    a second writer risks corruption (ADR-0033/0038/0042). The advisory lock
    (ADR-0042) is the detector: acquired fail-fast before any prompt, held
    through the operation, released after.

    Holding the lock is also the moment the platform takes hold of the
    database outside the Core Service, so it is where the ADR-0066 startup
    permission sweep runs for these commands — the same set the service
    checks, under the same lock, before any prompt.
    """
    lock = InstanceLock(cfg.database.path)
    try:
        lock.acquire()
    except InstanceLockHeldError as exc:
        raise fail(
            f"{exc}. This command needs exclusive database access and cannot "
            "run while the Core Service (or another instance) holds the "
            "database; stop it first (ADR-0038/0042)."
        ) from exc
    try:
        permcheck.verify_startup_files(
            config_path=cfg.path,
            database_path=cfg.database.path,
            backup_dir=cfg.backup.directory,
            warn=warn_cli,
        )
        yield
    finally:
        lock.release()
