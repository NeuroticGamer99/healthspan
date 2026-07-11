"""Shared CLI plumbing: per-invocation state and the fatal-error path.

Lives outside cli.py so command modules (cli.py, cli_keys.py) can share it
without importing each other.
"""

from dataclasses import dataclass
from pathlib import Path

import typer

from healthspan.config import Config, ConfigError, load_config


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


def load_config_or_exit(ctx: typer.Context) -> Config:
    try:
        return load_config(flag=state(ctx).config_flag)
    except ConfigError as exc:
        raise fail(str(exc)) from exc
