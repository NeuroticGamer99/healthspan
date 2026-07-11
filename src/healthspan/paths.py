"""Platform-conventional filesystem locations (ADR-0046).

Single source of truth for where the config file, database, and backups
live by default. Every process resolves these identically; nothing else
in the codebase may hardcode a platform path.
"""

from pathlib import Path

import platformdirs

APP_NAME = "healthspan"

CONFIG_FILENAME = "config.toml"
DATABASE_FILENAME = "healthspan.db"
BACKUP_DIRNAME = "backups"

# Environment variable naming a config *file path* — never a secret value,
# so it is outside ADR-0039's env-var prohibition.
CONFIG_ENV_VAR = "HEALTHSPAN_CONFIG"


def config_dir() -> Path:
    """Per-user configuration directory."""
    return Path(platformdirs.user_config_dir(APP_NAME, appauthor=False, roaming=False))


def data_dir() -> Path:
    """Per-user data directory (database, sidecar, default backup home)."""
    return Path(platformdirs.user_data_dir(APP_NAME, appauthor=False, roaming=False))


def default_config_path() -> Path:
    return config_dir() / CONFIG_FILENAME


def default_database_path() -> Path:
    return data_dir() / DATABASE_FILENAME


def default_backup_dir() -> Path:
    return data_dir() / BACKUP_DIRNAME
