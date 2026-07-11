"""Unit tests for config discovery and strict TOML parsing (ADR-0046)."""

import os
from collections.abc import Callable
from pathlib import Path

import pytest

from healthspan import paths
from healthspan.config import (
    SUPPORTED_CONFIG_VERSION,
    ConfigError,
    ConfigSource,
    load_config,
    path_status,
    resolve_config_path,
)

WriteFile = Callable[[Path, str], Path]

VALID = f"""
config_version = {SUPPORTED_CONFIG_VERSION}

[database]
path = "data/my.db"

[backup]
directory = "backups"
schedule = "daily"
retention_count = 7

[logging]
level = "debug"
"""


class TestResolveConfigPath:
    def test_flag_wins_over_env(self, tmp_path: Path) -> None:
        flag = tmp_path / "flag.toml"
        path, source = resolve_config_path(
            flag, {paths.CONFIG_ENV_VAR: str(tmp_path / "env.toml")}
        )
        assert (path, source) == (flag, ConfigSource.FLAG)

    def test_env_wins_over_default(self, tmp_path: Path) -> None:
        env_file = tmp_path / "env.toml"
        path, source = resolve_config_path(None, {paths.CONFIG_ENV_VAR: str(env_file)})
        assert (path, source) == (env_file, ConfigSource.ENV)

    def test_default_when_no_flag_or_env(self) -> None:
        path, source = resolve_config_path(None, {})
        assert (path, source) == (paths.default_config_path(), ConfigSource.DEFAULT)

    def test_empty_env_value_falls_through_to_default(self) -> None:
        _, source = resolve_config_path(None, {paths.CONFIG_ENV_VAR: ""})
        assert source is ConfigSource.DEFAULT

    def test_tilde_expanded_in_flag_and_env(self) -> None:
        expanded = Path("~/hs.toml").expanduser()
        assert resolve_config_path(Path("~/hs.toml"), {})[0] == expanded
        assert resolve_config_path(None, {paths.CONFIG_ENV_VAR: "~/hs.toml"})[0] == (
            expanded
        )


class TestPathStatus:
    def test_existing_file(self, tmp_path: Path, write_file: WriteFile) -> None:
        cfg = write_file(tmp_path / "config.toml", "config_version = 1\n")
        assert path_status(cfg, ConfigSource.FLAG) == "exists"

    def test_missing_default_means_defaults(self, tmp_path: Path) -> None:
        status = path_status(tmp_path / "absent.toml", ConfigSource.DEFAULT)
        assert status == "not created yet; defaults apply"

    @pytest.mark.parametrize("source", [ConfigSource.FLAG, ConfigSource.ENV])
    def test_missing_explicit_pointer_is_not_defaults(
        self, tmp_path: Path, source: ConfigSource
    ) -> None:
        assert path_status(tmp_path / "absent.toml", source) == "does not exist"


class TestLoadConfigDiscovery:
    def test_missing_file_behind_flag_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="--config flag"):
            load_config(flag=tmp_path / "absent.toml", env={})

    def test_missing_file_behind_env_is_an_error(self, tmp_path: Path) -> None:
        env = {paths.CONFIG_ENV_VAR: str(tmp_path / "absent.toml")}
        with pytest.raises(ConfigError, match=paths.CONFIG_ENV_VAR):
            load_config(env=env)

    def test_missing_default_file_yields_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            paths, "default_config_path", lambda: tmp_path / "config.toml"
        )
        cfg = load_config(env={})
        assert not cfg.loaded_from_file
        assert cfg.source is ConfigSource.DEFAULT
        assert cfg.config_version == SUPPORTED_CONFIG_VERSION
        assert cfg.database.path == paths.default_database_path()
        assert cfg.backup.directory == paths.default_backup_dir()
        # ADR-0038's decided defaults.
        assert cfg.backup.schedule == "daily"
        assert cfg.backup.retention_count == 14
        assert cfg.logging.level == "INFO"


class TestParsing:
    def test_valid_file_parses_fully(
        self, tmp_path: Path, write_file: WriteFile
    ) -> None:
        cfg = load_config(flag=write_file(tmp_path / "config.toml", VALID), env={})
        assert cfg.loaded_from_file
        assert cfg.database.path == tmp_path / "data" / "my.db"
        assert cfg.backup.directory == tmp_path / "backups"
        assert cfg.backup.retention_count == 7
        assert cfg.logging.level == "DEBUG"  # normalized to upper case

    def test_absolute_paths_kept_verbatim(
        self, tmp_path: Path, write_file: WriteFile
    ) -> None:
        db = tmp_path / "elsewhere" / "db.sqlite"
        content = f"config_version = 1\n[database]\npath = '{db.as_posix()}'\n"
        cfg = load_config(flag=write_file(tmp_path / "config.toml", content), env={})
        assert cfg.database.path == db
        assert cfg.database.path.is_absolute()

    def test_tilde_expanded_in_path_values(
        self, tmp_path: Path, write_file: WriteFile
    ) -> None:
        content = "config_version = 1\n[database]\npath = '~/hs/data.db'\n"
        cfg = load_config(flag=write_file(tmp_path / "config.toml", content), env={})
        assert cfg.database.path == Path("~/hs/data.db").expanduser()
        assert "~" not in cfg.database.path.parts

    def test_partial_file_fills_defaults(
        self, tmp_path: Path, write_file: WriteFile
    ) -> None:
        cfg = load_config(
            flag=write_file(tmp_path / "config.toml", "config_version = 1\n"), env={}
        )
        assert cfg.backup.retention_count == 14
        assert cfg.database.path == paths.default_database_path()

    @pytest.mark.parametrize(
        ("content", "match"),
        [
            ("config_version = 1\nsurprise = 1\n", "unknown key.*surprise"),
            ("config_version = 1\n[database]\npaht = 'x'\n", r"unknown key.*paht"),
            (
                "config_version = 1\n[backup]\nretentoin_count = 2\n",
                r"unknown key.*\[backup\].*retentoin_count",
            ),
            (
                "config_version = 1\n[logging]\nlvl = 'INFO'\n",
                r"unknown key.*\[logging\].*lvl",
            ),
            ("[database]\npath = 'x'\n", "missing required key 'config_version'"),
            ("config_version = 99\n", "unsupported config_version 99"),
            ("config_version = true\n", "'config_version' must be an integer"),
            ("config_version = 1\n[database]\npath = 5\n", "must be a string"),
            ("config_version = 1\n[database]\npath = ''\n", "must not be empty"),
            ("config_version = 1\ndatabase = 3\n", "must be a table"),
            (
                "config_version = 1\n[backup]\ndirectory = ''\n",
                "'backup.directory' must not be empty",
            ),
            (
                "config_version = 1\n[backup]\nretention_count = 0\n",
                "retention_count must be >= 1",
            ),
            (
                "config_version = 1\n[backup]\nretention_count = true\n",
                "must be an integer",
            ),
            ("config_version = 1\n[backup]\nschedule = ''\n", "must not be empty"),
            ("config_version = 1\n[logging]\nlevel = 'verbose'\n", "logging.level"),
            ("config_version = [\n", "invalid TOML"),
        ],
    )
    def test_rejects_bad_content(
        self, tmp_path: Path, write_file: WriteFile, content: str, match: str
    ) -> None:
        with pytest.raises(ConfigError, match=match):
            load_config(flag=write_file(tmp_path / "config.toml", content), env={})


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits only")
class TestPermissionWarning:
    def test_warns_on_broad_permissions(
        self, tmp_path: Path, write_file: WriteFile
    ) -> None:
        cfg_file = write_file(tmp_path / "config.toml", "config_version = 1\n")
        cfg_file.chmod(0o644)
        warnings: list[str] = []
        load_config(flag=cfg_file, env={}, warn=warnings.append)
        assert len(warnings) == 1
        assert "owner-only" in warnings[0]

    def test_silent_on_owner_only(self, tmp_path: Path, write_file: WriteFile) -> None:
        cfg_file = write_file(tmp_path / "config.toml", "config_version = 1\n")
        cfg_file.chmod(0o600)
        warnings: list[str] = []
        load_config(flag=cfg_file, env={}, warn=warnings.append)
        assert warnings == []
