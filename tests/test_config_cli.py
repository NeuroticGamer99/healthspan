"""The ``[cli]`` configuration section (ADR-0059)."""

from pathlib import Path

import pytest

from healthspan.config import ConfigError, load_config


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_cli_token_name_defaults_when_section_absent(tmp_path: Path) -> None:
    cfg = load_config(flag=_write(tmp_path, "config_version = 1\n"))
    assert cfg.cli.token_name == "cli-admin"


def test_cli_token_name_parsed(tmp_path: Path) -> None:
    cfg = load_config(
        flag=_write(tmp_path, 'config_version = 1\n[cli]\ntoken_name = "cli-entry"\n')
    )
    assert cfg.cli.token_name == "cli-entry"


def test_cli_rejects_unknown_key(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="unknown key"):
        load_config(flag=_write(tmp_path, "config_version = 1\n[cli]\nbogus = 1\n"))


def test_cli_token_name_must_not_be_empty(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"cli\.token_name must not be empty"):
        load_config(
            flag=_write(tmp_path, 'config_version = 1\n[cli]\ntoken_name = ""\n')
        )


def test_cli_token_name_must_be_a_string(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"cli\.token_name"):
        load_config(
            flag=_write(tmp_path, "config_version = 1\n[cli]\ntoken_name = 7\n")
        )
