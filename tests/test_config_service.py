"""The ``[service]`` configuration section (ADR-0049)."""

from pathlib import Path

import pytest

from healthspan.config import ConfigError, load_config


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_service_defaults_when_section_absent(tmp_path: Path) -> None:
    cfg = load_config(flag=_write(tmp_path, "config_version = 1\n"))
    assert cfg.service.host == "127.0.0.1"
    assert cfg.service.port == 8464
    assert cfg.service.passphrase_file is None


def test_service_values_parsed(tmp_path: Path) -> None:
    cfg = load_config(
        flag=_write(
            tmp_path,
            'config_version = 1\n[service]\nhost = "0.0.0.0"\nport = 9000\n',
        )
    )
    assert cfg.service.host == "0.0.0.0"
    assert cfg.service.port == 9000


def test_passphrase_file_resolves_relative_to_config_dir(tmp_path: Path) -> None:
    cfg = load_config(
        flag=_write(
            tmp_path,
            'config_version = 1\n[service]\npassphrase_file = "secret/pp"\n',
        )
    )
    assert cfg.service.passphrase_file == tmp_path / "secret" / "pp"


def test_service_rejects_unknown_key(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="unknown key"):
        load_config(flag=_write(tmp_path, "config_version = 1\n[service]\nbogus = 1\n"))


def test_service_port_range_validated(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="between 1 and 65535"):
        load_config(
            flag=_write(tmp_path, "config_version = 1\n[service]\nport = 70000\n")
        )


def test_service_host_not_empty(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="host must not be empty"):
        load_config(flag=_write(tmp_path, 'config_version = 1\n[service]\nhost = ""\n'))


def test_service_port_must_be_int(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="must be an integer"):
        load_config(
            flag=_write(tmp_path, 'config_version = 1\n[service]\nport = "x"\n')
        )
