"""The ``[auth]`` configuration section (ADR-0051)."""

from pathlib import Path

import pytest

from healthspan.config import ConfigError, load_config


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_auth_defaults_when_section_absent(tmp_path: Path) -> None:
    cfg = load_config(flag=_write(tmp_path, "config_version = 1\n"))
    assert cfg.auth.failure_threshold == 5
    assert cfg.auth.max_backoff_seconds == 60


def test_auth_values_parsed(tmp_path: Path) -> None:
    cfg = load_config(
        flag=_write(
            tmp_path,
            "config_version = 1\n[auth]\n"
            "failure_threshold = 3\nmax_backoff_seconds = 120\n",
        )
    )
    assert cfg.auth.failure_threshold == 3
    assert cfg.auth.max_backoff_seconds == 120


def test_auth_rejects_unknown_key(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="unknown key"):
        load_config(flag=_write(tmp_path, "config_version = 1\n[auth]\nbogus = 1\n"))


@pytest.mark.parametrize("key", ["failure_threshold", "max_backoff_seconds"])
def test_auth_values_must_be_positive(tmp_path: Path, key: str) -> None:
    with pytest.raises(ConfigError, match=key):
        load_config(flag=_write(tmp_path, f"config_version = 1\n[auth]\n{key} = 0\n"))


@pytest.mark.parametrize("key", ["failure_threshold", "max_backoff_seconds"])
def test_auth_values_must_be_integers(tmp_path: Path, key: str) -> None:
    with pytest.raises(ConfigError, match=key):
        load_config(flag=_write(tmp_path, f'config_version = 1\n[auth]\n{key} = "x"\n'))
