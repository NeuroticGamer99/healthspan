"""Typer CLI entry point (ADR-0006) and config inspection (ADR-0046).

Global ``--config``/``--version``, the ``config`` inspection group,
``init`` and the ``keys`` group (WI-2, ADR-0028/0033). The ``db`` command
group arrives with the work items that implement it.
"""

from importlib.metadata import version as _dist_version
from pathlib import Path
from typing import Annotated

import typer

from healthspan.cli_db import db_app
from healthspan.cli_keys import init_command, keys_app
from healthspan.cli_support import AppState, load_config_or_exit, state
from healthspan.config import (
    Config,
    path_status,
    resolve_config_path,
    toml_quote,
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
app.command("init")(init_command)
app.add_typer(keys_app, name="keys")
app.add_typer(db_app, name="db")


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


@config_app.command("path")
def config_path(ctx: typer.Context) -> None:
    """Print the resolved config file path and which source resolved it."""
    path, source = resolve_config_path(state(ctx).config_flag)
    typer.echo(f"{path} (from {source.value}; {path_status(path, source)})")


@config_app.command("show")
def config_show(ctx: typer.Context) -> None:
    """Print the effective configuration (file values merged over defaults)."""
    typer.echo(_render_toml(load_config_or_exit(ctx)))


def _render_toml(cfg: Config) -> str:
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
            f"path = {toml_quote(str(cfg.database.path))}",
            "",
            "[backup]",
            f"directory = {toml_quote(str(cfg.backup.directory))}",
            f"schedule = {toml_quote(cfg.backup.schedule)}",
            f"retention_count = {cfg.backup.retention_count}",
            "",
            "[logging]",
            f"level = {toml_quote(cfg.logging.level)}",
        ]
    )


def main() -> None:
    """Console-script entry point."""
    app()
