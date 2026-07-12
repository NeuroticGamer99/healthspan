"""``healthspan service start`` CLI (ADR-0039/0049)."""

from pathlib import Path

import pytest
from fastapi import FastAPI
from typer.testing import CliRunner

import healthspan.service as service_mod
from healthspan.cli import app
from healthspan.config import Config

runner = CliRunner()
PASSPHRASE = "a perfectly reasonable passphrase"


def _init(tmp_path: Path, *, migrate: bool) -> Path:
    config = tmp_path / "config.toml"
    config.write_text(
        'config_version = 1\n\n[database]\npath = "hs.db"\n', encoding="utf-8"
    )
    assert (
        runner.invoke(
            app,
            ["--config", str(config), "init"],
            input=f"{PASSPHRASE}\n{PASSPHRASE}\n",
        ).exit_code
        == 0
    )
    if migrate:
        assert (
            runner.invoke(
                app, ["--config", str(config), "db", "migrate"], input=f"{PASSPHRASE}\n"
            ).exit_code
            == 0
        )
    return config


def test_start_refuses_pending_migration(tmp_path: Path) -> None:
    config = _init(tmp_path, migrate=False)
    result = runner.invoke(
        app, ["--config", str(config), "service", "start"], input=f"{PASSPHRASE}\n"
    )
    assert result.exit_code == 1
    assert "db migrate" in result.output


def test_start_runs_server_after_successful_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _init(tmp_path, migrate=True)
    captured: dict[str, object] = {}

    def fake_run(application: FastAPI, cfg: Config) -> None:
        captured["host"] = cfg.service.host
        captured["port"] = cfg.service.port

    monkeypatch.setattr(service_mod, "_run_uvicorn", fake_run)
    result = runner.invoke(
        app, ["--config", str(config), "service", "start"], input=f"{PASSPHRASE}\n"
    )
    assert result.exit_code == 0, result.output
    assert captured == {"host": "127.0.0.1", "port": 8464}


def test_start_wrong_passphrase_exits_nonzero(tmp_path: Path) -> None:
    config = _init(tmp_path, migrate=True)
    result = runner.invoke(
        app,
        ["--config", str(config), "service", "start"],
        input="the wrong passphrase\n",
    )
    assert result.exit_code == 1


def test_start_never_reads_passphrase_from_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _init(tmp_path, migrate=True)
    monkeypatch.setenv("HEALTHSPAN_PASSPHRASE", PASSPHRASE)
    # Empty stdin, no passphrase_file: startup must fail rather than fall back
    # to the environment variable (ADR-0039).
    result = runner.invoke(app, ["--config", str(config), "service", "start"], input="")
    assert result.exit_code == 1
    assert "no passphrase channel" in result.output
