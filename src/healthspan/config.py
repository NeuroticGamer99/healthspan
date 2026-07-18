"""Shared TOML configuration: discovery, strict parsing, defaults.

Implements ADR-0046 (location, discovery precedence, load semantics) for
the shared configuration file ADR-0006 defines. Readers never write the
file; creation belongs to ``healthspan init`` and, later, the launcher
(ADR-0008). A missing file at the platform-default location means
defaults apply; a missing file behind an explicit ``--config`` flag or
``HEALTHSPAN_CONFIG`` is an error.
"""

import json
import os
import stat
import sys
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, cast

from healthspan import paths

SUPPORTED_CONFIG_VERSION = 1

_VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


class ConfigError(Exception):
    """A configuration file could not be resolved, parsed, or validated."""


def toml_quote(value: str) -> str:
    """Render a string as a TOML basic string literal.

    TOML basic strings share JSON's escape rules, so json.dumps produces
    valid TOML (Windows path backslashes included). The single writer-side
    escaper — every module emitting TOML uses this.
    """
    return json.dumps(value)


class ConfigSource(Enum):
    """Which discovery step (ADR-0046 precedence) produced the config path."""

    FLAG = "--config flag"
    ENV = f"{paths.CONFIG_ENV_VAR} environment variable"
    DEFAULT = "platform default"


@dataclass(frozen=True)
class DatabaseConfig:
    path: Path


@dataclass(frozen=True)
class BackupConfig:
    directory: Path
    schedule: str
    retention_count: int


@dataclass(frozen=True)
class LoggingConfig:
    level: str


@dataclass(frozen=True)
class ServiceConfig:
    """Core Service HTTP listener and passphrase-channel settings (ADR-0049).

    ``page_cap`` is the server-enforced read-endpoint page bound (ADR-0053):
    the single enforcement point every client inherits.
    """

    host: str
    port: int
    passphrase_file: Path | None
    page_cap: int


@dataclass(frozen=True)
class AuthConfig:
    """Auth-failure rate-limiter settings (ADR-0026 rule 4, ADR-0051)."""

    failure_threshold: int
    max_backoff_seconds: int


@dataclass(frozen=True)
class CliConfig:
    """Client-side CLI settings (ADR-0059).

    ``token_name`` is the OS-keyring token the data-entry/readback CLI groups
    authenticate as (entry ``token:<token_name>``); it defaults to the
    always-minted ``cli-admin`` and is overridable per-invocation by
    ``--token-name``. A narrower hand-minted token (``read``/``import``) can be
    named here for least privilege.
    """

    token_name: str


@dataclass(frozen=True)
class Config:
    """Effective configuration: file values merged over defaults."""

    config_version: int
    database: DatabaseConfig
    backup: BackupConfig
    logging: LoggingConfig
    service: ServiceConfig
    auth: AuthConfig
    cli: CliConfig
    path: Path
    source: ConfigSource
    loaded_from_file: bool


def resolve_config_path(
    flag: Path | None,
    env: Mapping[str, str] | None = None,
) -> tuple[Path, ConfigSource]:
    """Resolve the config file path per the ADR-0046 precedence chain."""
    if flag is not None:
        return flag.expanduser(), ConfigSource.FLAG
    env_map = os.environ if env is None else env
    env_value = env_map.get(paths.CONFIG_ENV_VAR)
    if env_value:
        return Path(env_value).expanduser(), ConfigSource.ENV
    return paths.default_config_path(), ConfigSource.DEFAULT


def path_status(path: Path, source: ConfigSource) -> str:
    """Human-readable status of a resolved config path.

    Kept next to ``load_config`` so the missing-file policy (error for
    explicit sources, defaults for the platform default) has one home.
    """
    if path.is_file():
        return "exists"
    if source is ConfigSource.DEFAULT:
        return "not created yet; defaults apply"
    return "does not exist"


def load_config(
    flag: Path | None = None,
    env: Mapping[str, str] | None = None,
    warn: Callable[[str], None] | None = None,
) -> Config:
    """Load the effective configuration.

    ``warn`` receives non-fatal findings (currently: overly broad file
    permissions, per security.md); it defaults to writing on stderr.
    """
    if warn is None:
        warn = _warn_stderr
    path, source = resolve_config_path(flag, env)
    if not path.is_file():
        if source is not ConfigSource.DEFAULT:
            raise ConfigError(
                f"config file {path} (from {source.value}) does not exist"
            )
        return _defaults(path, source)
    _check_permissions(path, warn)
    data = _load_toml(path)
    return _parse(data, path, source)


def _warn_stderr(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def _defaults(path: Path, source: ConfigSource) -> Config:
    return Config(
        config_version=SUPPORTED_CONFIG_VERSION,
        database=DatabaseConfig(path=paths.default_database_path()),
        backup=BackupConfig(
            directory=paths.default_backup_dir(),
            # Defaults daily / retain 14 are ADR-0038's decided values.
            schedule="daily",
            retention_count=14,
        ),
        logging=LoggingConfig(level="INFO"),
        # Core Service defaults (ADR-0049): loopback-only binding, a port
        # clear of the common-collision range, no OS-secret passphrase file.
        # Read page cap default 100 rows (ADR-0053).
        service=ServiceConfig(
            host="127.0.0.1", port=8464, passphrase_file=None, page_cap=100
        ),
        # Limiter defaults (ADR-0051): 5 free failures per bucket, then
        # exponential backoff capped at ADR-0026's decided 60 seconds.
        auth=AuthConfig(failure_threshold=5, max_backoff_seconds=60),
        # CLI default credential (ADR-0059): the always-minted cli-admin token.
        cli=CliConfig(token_name="cli-admin"),  # noqa: S106 - token name, not a secret
        path=path,
        source=source,
        loaded_from_file=False,
    )


def _check_permissions(path: Path, warn: Callable[[str], None]) -> None:
    # POSIX only: Windows permission bits carry no ACL information, and the
    # writer side (`init`) owns setting owner-only ACLs there.
    if os.name != "posix":
        return
    try:
        mode = path.stat().st_mode
    except OSError:
        return  # an unreadable stat is not fatal here; _load_toml surfaces it
    if mode & 0o077:
        warn(
            f"config file {path} is accessible beyond its owner "
            f"(mode {stat.filemode(mode)}); expected owner-only (chmod 600)"
        )


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    except OSError as exc:
        # Unreadable file, a directory at the config path, a transient I/O
        # error: a clean ConfigError, not an uncaught traceback.
        raise ConfigError(f"{path}: could not read config file: {exc}") from exc


def _parse(data: dict[str, Any], path: Path, source: ConfigSource) -> Config:
    base = path.parent
    defaults = _defaults(path, source)

    _reject_unknown_keys(
        data,
        {"config_version", "database", "backup", "logging", "service", "auth", "cli"},
        path,
        where="top level",
    )

    if "config_version" not in data:
        raise ConfigError(f"{path}: missing required key 'config_version'")
    version = _expect_int(data["config_version"], path, "config_version")
    if version != SUPPORTED_CONFIG_VERSION:
        raise ConfigError(
            f"{path}: unsupported config_version {version} "
            f"(this build supports {SUPPORTED_CONFIG_VERSION})"
        )

    database = defaults.database
    table = _section(data, "database", {"path"}, path)
    if table is not None and "path" in table:
        raw = _expect_str(table["path"], path, "database.path")
        database = DatabaseConfig(path=_resolve_path(raw, base, path, "database.path"))

    backup = defaults.backup
    known = {"directory", "schedule", "retention_count"}
    table = _section(data, "backup", known, path)
    if table is not None:
        directory = backup.directory
        if "directory" in table:
            raw = _expect_str(table["directory"], path, "backup.directory")
            directory = _resolve_path(raw, base, path, "backup.directory")
        schedule = backup.schedule
        if "schedule" in table:
            schedule = _expect_str(table["schedule"], path, "backup.schedule")
            # Cadence vocabulary belongs to the Core-internal scheduler
            # (ADR-0038, Phase 2); until then only non-emptiness is enforced.
            if not schedule:
                raise ConfigError(f"{path}: backup.schedule must not be empty")
        retention = backup.retention_count
        if "retention_count" in table:
            retention = _expect_int(
                table["retention_count"], path, "backup.retention_count"
            )
            if retention < 1:
                raise ConfigError(
                    f"{path}: backup.retention_count must be >= 1, got {retention}"
                )
        backup = BackupConfig(
            directory=directory, schedule=schedule, retention_count=retention
        )

    logging_cfg = defaults.logging
    table = _section(data, "logging", {"level"}, path)
    if table is not None and "level" in table:
        raw_level = _expect_str(table["level"], path, "logging.level").upper()
        if raw_level not in _VALID_LOG_LEVELS:
            raise ConfigError(
                f"{path}: logging.level must be one of "
                f"{', '.join(sorted(_VALID_LOG_LEVELS))}; got {table['level']!r}"
            )
        logging_cfg = LoggingConfig(level=raw_level)

    service = _parse_service(data, defaults.service, base, path)
    auth = _parse_auth(data, defaults.auth, path)
    cli = _parse_cli(data, defaults.cli, path)

    return Config(
        config_version=version,
        database=database,
        backup=backup,
        logging=logging_cfg,
        service=service,
        auth=auth,
        cli=cli,
        path=path,
        source=source,
        loaded_from_file=True,
    )


def _parse_service(
    data: dict[str, Any], default: ServiceConfig, base: Path, path: Path
) -> ServiceConfig:
    """Parse the optional ``[service]`` table (ADR-0049)."""
    table = _section(
        data, "service", {"host", "port", "passphrase_file", "page_cap"}, path
    )
    if table is None:
        return default
    host = default.host
    if "host" in table:
        host = _expect_str(table["host"], path, "service.host")
        if not host:
            raise ConfigError(f"{path}: service.host must not be empty")
    port = default.port
    if "port" in table:
        port = _expect_int(table["port"], path, "service.port")
        if not 1 <= port <= 65535:
            raise ConfigError(
                f"{path}: service.port must be between 1 and 65535, got {port}"
            )
    passphrase_file = default.passphrase_file
    if "passphrase_file" in table:
        raw = _expect_str(table["passphrase_file"], path, "service.passphrase_file")
        passphrase_file = _resolve_path(raw, base, path, "service.passphrase_file")
    page_cap = default.page_cap
    if "page_cap" in table:
        page_cap = _expect_int(table["page_cap"], path, "service.page_cap")
        if page_cap < 1:
            raise ConfigError(f"{path}: service.page_cap must be >= 1, got {page_cap}")
    return ServiceConfig(
        host=host, port=port, passphrase_file=passphrase_file, page_cap=page_cap
    )


def _parse_auth(data: dict[str, Any], default: AuthConfig, path: Path) -> AuthConfig:
    """Parse the optional ``[auth]`` table (ADR-0051)."""
    table = _section(data, "auth", {"failure_threshold", "max_backoff_seconds"}, path)
    if table is None:
        return default

    def positive_int(key: str, fallback: int) -> int:
        if key not in table:
            return fallback
        value = _expect_int(table[key], path, f"auth.{key}")
        if value < 1:
            raise ConfigError(f"{path}: auth.{key} must be >= 1, got {value}")
        return value

    return AuthConfig(
        failure_threshold=positive_int("failure_threshold", default.failure_threshold),
        max_backoff_seconds=positive_int(
            "max_backoff_seconds", default.max_backoff_seconds
        ),
    )


def _parse_cli(data: dict[str, Any], default: CliConfig, path: Path) -> CliConfig:
    """Parse the optional ``[cli]`` table (ADR-0059)."""
    table = _section(data, "cli", {"token_name"}, path)
    if table is None:
        return default
    token_name = default.token_name
    if "token_name" in table:
        token_name = _expect_str(table["token_name"], path, "cli.token_name")
        if not token_name:
            raise ConfigError(f"{path}: cli.token_name must not be empty")
    return CliConfig(token_name=token_name)


def _reject_unknown_keys(
    table: dict[str, Any], known: set[str], path: Path, where: str
) -> None:
    unknown = sorted(set(table) - known)
    if unknown:
        # Strict by decision (ADR-0046): a typo that silently falls back to a
        # default is a misconfiguration that looks configured.
        raise ConfigError(f"{path}: unknown key(s) at {where}: {', '.join(unknown)}")


def _section(
    data: dict[str, Any], name: str, known: set[str], path: Path
) -> dict[str, Any] | None:
    """Fetch an optional section table, enforcing the unknown-key rule."""
    if name not in data:
        return None
    table = _expect_table(data[name], path, name)
    _reject_unknown_keys(table, known, path, where=f"[{name}]")
    return table


def _resolve_path(raw: str, base: Path, config_path: Path, key: str) -> Path:
    if not raw:
        # Path("") collapses to "." and would silently point at the config
        # directory — a misconfiguration that looks configured.
        raise ConfigError(f"{config_path}: '{key}' must not be empty")
    p = Path(raw).expanduser()
    return p if p.is_absolute() else base / p


def _expect_table(value: Any, path: Path, key: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{path}: '{key}' must be a table")
    # TOML table keys are always strings.
    return cast(dict[str, Any], value)


def _expect_str(value: Any, path: Path, key: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{path}: '{key}' must be a string")
    return value


def _expect_int(value: Any, path: Path, key: str) -> int:
    # bool is a subclass of int; `retention_count = true` must not pass.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{path}: '{key}' must be an integer")
    return value
