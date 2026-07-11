"""CLI skeleton tests: entry point, --version, config inspection commands."""

import dataclasses
import subprocess
import sys
from collections.abc import Callable
from importlib.metadata import entry_points
from pathlib import Path

import pytest
from typer.testing import CliRunner

from healthspan import paths
from healthspan.cli import app, main
from healthspan.config import load_config

WriteFile = Callable[[Path, str], Path]

runner = CliRunner()


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.startswith("healthspan ")


def test_no_args_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "config" in result.output


def test_console_script_mapping_resolves_to_main() -> None:
    (ep,) = entry_points(group="console_scripts", name="healthspan")
    assert ep.load() is main


def test_python_dash_m_entry_point() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "healthspan", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert proc.stdout.startswith("healthspan ")


class TestConfigPath:
    def test_reports_flag_source_and_missing_file(self, tmp_path: Path) -> None:
        target = tmp_path / "config.toml"
        result = runner.invoke(app, ["--config", str(target), "config", "path"])
        assert result.exit_code == 0
        assert str(target) in result.stdout
        assert "--config flag" in result.stdout
        # A missing file behind an explicit flag is an error at load time,
        # so `config path` must not claim defaults apply.
        assert "does not exist" in result.stdout

    def test_reports_flag_source_and_existing_file(
        self, tmp_path: Path, write_file: WriteFile
    ) -> None:
        target = write_file(tmp_path / "config.toml", "config_version = 1\n")
        result = runner.invoke(app, ["--config", str(target), "config", "path"])
        assert result.exit_code == 0
        assert "--config flag" in result.stdout
        assert "exists" in result.stdout

    def test_missing_default_reports_defaults_apply(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            paths, "default_config_path", lambda: tmp_path / "config.toml"
        )
        monkeypatch.delenv(paths.CONFIG_ENV_VAR, raising=False)
        result = runner.invoke(app, ["config", "path"])
        assert result.exit_code == 0
        assert "defaults apply" in result.stdout

    def test_reports_env_source(self, tmp_path: Path, write_file: WriteFile) -> None:
        target = write_file(tmp_path / "config.toml", "config_version = 1\n")
        result = runner.invoke(
            app, ["config", "path"], env={paths.CONFIG_ENV_VAR: str(target)}
        )
        assert result.exit_code == 0
        assert paths.CONFIG_ENV_VAR in result.stdout
        assert "exists" in result.stdout


class TestConfigShow:
    def test_renders_effective_config(
        self, tmp_path: Path, write_file: WriteFile
    ) -> None:
        cfg_file = write_file(
            tmp_path / "config.toml",
            "config_version = 1\n[backup]\nretention_count = 3\n",
        )
        result = runner.invoke(app, ["--config", str(cfg_file), "config", "show"])
        assert result.exit_code == 0
        assert "config_version = 1" in result.stdout
        assert "retention_count = 3" in result.stdout
        # Unset keys render their defaults (ADR-0038: daily / 14 minus override).
        assert 'schedule = "daily"' in result.stdout
        assert "[database]" in result.stdout
        assert "--config flag" in result.stdout  # provenance names the source

    def test_renders_every_config_section_and_key(
        self, tmp_path: Path, write_file: WriteFile
    ) -> None:
        """Drift guard: a section added to Config must appear in `config show`."""
        cfg_file = write_file(tmp_path / "config.toml", "config_version = 1\n")
        result = runner.invoke(app, ["--config", str(cfg_file), "config", "show"])
        assert result.exit_code == 0
        cfg = load_config(flag=cfg_file, env={})
        for field in dataclasses.fields(cfg):
            if not dataclasses.is_dataclass(getattr(cfg, field.name)):
                continue
            assert f"[{field.name}]" in result.stdout
            for key in dataclasses.fields(getattr(cfg, field.name)):
                assert f"{key.name} = " in result.stdout

    def test_missing_flagged_file_fails(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app, ["--config", str(tmp_path / "absent.toml"), "config", "show"]
        )
        assert result.exit_code == 1
        assert "error:" in result.stderr
        assert "does not exist" in result.stderr

    def test_invalid_file_fails_with_key_name(
        self, tmp_path: Path, write_file: WriteFile
    ) -> None:
        cfg_file = write_file(
            tmp_path / "config.toml", "config_version = 1\ntypo_key = 1\n"
        )
        result = runner.invoke(app, ["--config", str(cfg_file), "config", "show"])
        assert result.exit_code == 1
        assert "typo_key" in result.stderr
