"""Typer CLI entry point (ADR-0006) and config inspection (ADR-0046).

Phase-1 skeleton: global ``--config``/``--version`` plus the ``config``
inspection group. The ``db`` and ``keys`` command groups arrive with the
work items that implement them.
"""

import json
from dataclasses import dataclass
from importlib.metadata import version as _dist_version
from pathlib import Path
from typing import Annotated

import typer

from healthspan.config import (
    Config,
    ConfigError,
    load_config,
    path_status,
    resolve_config_path,
)
from healthspan.paths import APP_NAME

app = typer.Typer(
    name=APP_NAME,
    help="Local-first personal health data platform.",
    no_args_is_help=True,
)

config_app = typer.Typer(
    help="Inspect configuration discovery and effective values.",
    no_args_is_help=True,
)
app.add_typer(config_app, name="config")


@dataclass
class AppState:
    """Per-invocation state shared from the root callback to subcommands."""

    config_flag: Path | None


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"{APP_NAME} {_dist_version(APP_NAME)}")
        raise typer.Exit()


@app.callback()
def root(
    ctx: typer.Context,
    config: Annotated[
        Path | None,
        typer.Option(
            "--config",
            help=(
                "Path to the config file (overrides HEALTHSPAN_CONFIG "
                "and the platform default)."
            ),
        ),
    ] = None,
    _version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Print the version and exit.",
        ),
    ] = False,
) -> None:
    ctx.obj = AppState(config_flag=config)


def _state(ctx: typer.Context) -> AppState:
    obj = ctx.obj
    if not isinstance(obj, AppState):  # pragma: no cover - Typer wiring invariant
        raise RuntimeError("CLI state missing; root callback did not run")
    return obj


@config_app.command("path")
def config_path(ctx: typer.Context) -> None:
    """Print the resolved config file path and which source resolved it."""
    path, source = resolve_config_path(_state(ctx).config_flag)
    typer.echo(f"{path} (from {source.value}; {path_status(path, source)})")


@config_app.command("show")
def config_show(ctx: typer.Context) -> None:
    """Print the effective configuration (file values merged over defaults)."""
    try:
        cfg = load_config(flag=_state(ctx).config_flag)
    except ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(_render_toml(cfg))


def _render_toml(cfg: Config) -> str:
    # TOML basic strings share JSON's escape rules, so json.dumps produces
    # valid TOML string literals (Windows path backslashes included).
    def s(value: str) -> str:
        return json.dumps(value)

    provenance = (
        f"effective configuration from {cfg.path} (from {cfg.source.value})"
        if cfg.loaded_from_file
        else f"defaults (no config file at {cfg.path})"
    )
    return "\n".join(
        [
            f"# {provenance}",
            f"config_version = {cfg.config_version}",
            "",
            "[database]",
            f"path = {s(str(cfg.database.path))}",
            "",
            "[backup]",
            f"directory = {s(str(cfg.backup.directory))}",
            f"schedule = {s(cfg.backup.schedule)}",
            f"retention_count = {cfg.backup.retention_count}",
            "",
            "[logging]",
            f"level = {s(cfg.logging.level)}",
        ]
    )


def main() -> None:
    """Console-script entry point."""
    app()
