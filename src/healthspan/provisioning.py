"""``healthspan init``: first-run provisioning (ADR-0013/0028/0046).

Creates the credentials for the chosen key mode, the empty encrypted
database (schema arrives via ``healthspan db migrate``), the ``.keyparams``
sidecar, and — when none exists at the platform-default location — a
skeleton config file. Every file it writes gets owner-only protection
(ADR-0046's writer obligation).
"""

from dataclasses import dataclass
from pathlib import Path

from healthspan import db, keychain
from healthspan.config import Config, ConfigSource
from healthspan.fsperm import set_owner_only
from healthspan.kdf import derive_db_key, generate_secret_key
from healthspan.keyparams import (
    KeyMode,
    KeyParams,
    sidecar_path,
    utc_now_iso,
    write_keyparams,
)

# Below this length the passphrase advisory triggers: warn and require
# confirmation, never refuse (Phase-1 owner decision, recorded in ADR-0047).
PASSPHRASE_ADVISORY_MIN = 12


class InitError(Exception):
    """Initialization could not proceed."""


@dataclass(frozen=True)
class InitResult:
    database_path: Path
    sidecar_path: Path
    mode: KeyMode
    secret_key: bytes | None  # two-factor only; caller renders the Recovery Kit


def initialize(cfg: Config, passphrase: str, mode: KeyMode) -> InitResult:
    """Provision a new encrypted database under the given key mode."""
    database_path = cfg.database.path
    sidecar = sidecar_path(database_path)
    for existing in (database_path, sidecar):
        if existing.exists():
            raise InitError(
                f"{existing} already exists; refusing to overwrite. "
                "Healthspan is already initialized (or a previous init "
                "left files behind - remove them only if you are certain "
                "they hold no data)."
            )

    _ensure_config_file(cfg)

    if mode is KeyMode.TWO_FACTOR:
        secret_key: bytes | None = generate_secret_key()
        salt = secret_key
        params = KeyParams(mode=mode, created_utc=utc_now_iso())
    else:
        secret_key = None
        salt = generate_secret_key()  # a salt, not a secret (ADR-0028)
        params = KeyParams(mode=mode, salt=salt, created_utc=utc_now_iso())

    key = derive_db_key(passphrase, salt, params)
    try:
        db.provision(database_path, key)
        write_keyparams(sidecar, params)
    finally:
        key.zeroize()

    if secret_key is not None:
        # After the database exists: an orphaned keychain entry from a
        # failed init would otherwise shadow nothing but confuse recovery.
        keychain.store_secret_key(secret_key)

    return InitResult(
        database_path=database_path,
        sidecar_path=sidecar,
        mode=mode,
        secret_key=secret_key,
    )


_CONFIG_TEMPLATE = """\
# Healthspan configuration (created by 'healthspan init').
# Commented values show the defaults in force; uncomment to override.
config_version = 1

# [database]
# path = {db_path}

# [backup]
# directory = {backup_dir}
# schedule = "daily"
# retention_count = 14

# [logging]
# level = "INFO"
"""


def _ensure_config_file(cfg: Config) -> None:
    """Create a skeleton config at the platform default if none exists.

    Readers never write (ADR-0046); creation belongs to init. An explicit
    ``--config``/env path must already exist (load_config enforces it), so
    only the platform-default location can be missing here.
    """
    if cfg.loaded_from_file or cfg.source is not ConfigSource.DEFAULT:
        return
    cfg.path.parent.mkdir(parents=True, exist_ok=True)
    set_owner_only(cfg.path.parent)

    def toml_str(p: Path) -> str:
        return '"' + str(p).replace("\\", "\\\\") + '"'

    cfg.path.write_text(
        _CONFIG_TEMPLATE.format(
            db_path=toml_str(cfg.database.path),
            backup_dir=toml_str(cfg.backup.directory),
        ),
        encoding="utf-8",
    )
    set_owner_only(cfg.path)
