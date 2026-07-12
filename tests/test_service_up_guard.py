"""Direct-database commands refuse while the database is held (ADR-0033/0038/0042).

The single-instance advisory lock is the "refuse while Core Service is up"
detector for the sanctioned direct-database commands (ADR-0049).
"""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from healthspan.cli import app
from healthspan.config import load_config
from healthspan.locking import InstanceLock

runner = CliRunner()
PASSPHRASE = "a perfectly reasonable passphrase"


def _init(tmp_path: Path) -> Path:
    config = tmp_path / "config.toml"
    config.write_text(
        'config_version = 1\n\n[database]\npath = "hs.db"\n\n'
        '[backup]\ndirectory = "backups"\n',
        encoding="utf-8",
    )
    assert (
        runner.invoke(
            app,
            ["--config", str(config), "init"],
            input=f"{PASSPHRASE}\n{PASSPHRASE}\n",
        ).exit_code
        == 0
    )
    assert (
        runner.invoke(
            app, ["--config", str(config), "db", "migrate"], input=f"{PASSPHRASE}\n"
        ).exit_code
        == 0
    )
    return config


@pytest.mark.parametrize(
    "command",
    [
        ["db", "migrate"],
        ["db", "backup"],
        ["db", "restore", "--latest"],
        # The keys rekeying commands joined the guard in WI-2b (the WI-1
        # deferral): they open the database outside the Core Service too.
        ["keys", "change-passphrase"],
        ["keys", "rotate-secret-key"],
        ["keys", "convert-mode", "--to", "passphrase-only"],
    ],
)
def test_direct_db_command_refuses_when_database_held(
    tmp_path: Path, command: list[str]
) -> None:
    config = _init(tmp_path)
    cfg = load_config(flag=config)
    holder = InstanceLock(cfg.database.path)
    holder.acquire()
    try:
        result = runner.invoke(
            app, ["--config", str(config), *command], input=f"{PASSPHRASE}\n"
        )
        assert result.exit_code == 1, result.output
        assert "exclusive database access" in result.output
    finally:
        holder.release()


def test_backup_succeeds_when_database_not_held(tmp_path: Path) -> None:
    config = _init(tmp_path)
    result = runner.invoke(
        app, ["--config", str(config), "db", "backup"], input=f"{PASSPHRASE}\n"
    )
    assert result.exit_code == 0, result.output
    assert "Verified backup created" in result.output
