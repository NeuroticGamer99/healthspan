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
from healthspan.config import Config, ConfigSource, toml_quote
from healthspan.fsperm import set_owner_only
from healthspan.kdf import decode_secret_key, derive_db_key, generate_secret_key
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


@dataclass(frozen=True)
class RestoreCredentialsResult:
    config_path: Path
    config_created: bool
    replaced_existing: bool  # a secret key was already in the keychain


def restore_credentials(cfg: Config, secret_key_text: str) -> RestoreCredentialsResult:
    """Re-establish two-factor credentials on a new machine (ADR-0038).

    Stores the Recovery Kit's secret key in the OS keychain and ensures a
    skeleton config exists — the credential half of new-machine setup. The
    data half is ``healthspan db restore``; the ADR sequences ``init
    --restore`` first, this command, then ``db restore``. It provisions no
    database and derives no key: the passphrase is not needed until the
    restored data file is opened.
    """
    try:
        secret_key = decode_secret_key(secret_key_text)
    except ValueError as exc:
        raise InitError(f"that is not a valid Recovery Kit secret key: {exc}") from exc

    replaced_existing = _keychain_has_secret_key()
    keychain.store_secret_key(secret_key)
    config_created = _ensure_config_file(cfg)
    return RestoreCredentialsResult(
        config_path=cfg.path,
        config_created=config_created,
        replaced_existing=replaced_existing,
    )


def _keychain_has_secret_key() -> bool:
    try:
        keychain.load_secret_key()
    except keychain.KeychainError:
        return False
    return True


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
        # Store BEFORE creating files: if the keychain is unavailable, init
        # aborts with nothing on disk. The reverse order could create an
        # encrypted database whose secret key was never stored or shown.
        # An orphaned entry from a later failure is harmless - the next
        # successful init overwrites it.
        keychain.store_secret_key(secret_key)
    else:
        secret_key = None
        salt = generate_secret_key()  # a salt, not a secret (ADR-0028)
        params = KeyParams(mode=mode, salt=salt, created_utc=utc_now_iso())

    key = derive_db_key(passphrase, salt, params)
    try:
        db.provision(database_path, key)
        write_keyparams(sidecar, params)
    except BaseException:
        # The database is empty at this point; removing the partial files
        # is safe and leaves init re-runnable instead of wedged on the
        # "already exists" guard.
        for leftover in (
            sidecar,
            database_path,
            database_path.with_name(database_path.name + "-wal"),
            database_path.with_name(database_path.name + "-shm"),
        ):
            leftover.unlink(missing_ok=True)
        raise
    finally:
        key.zeroize()

    return InitResult(
        database_path=database_path,
        sidecar_path=sidecar,
        mode=mode,
        secret_key=secret_key,
    )


_CONFIG_TEMPLATE = """\
# Healthspan configuration (created by 'healthspan init').
# The database path is pinned to where init created it, so a future
# change in platform-default locations can never orphan the file.
# Commented values show the defaults in force; uncomment to override.
config_version = 1

[database]
path = {db_path}

# [backup]
# directory = {backup_dir}
# schedule = "daily"
# retention_count = 14

# [logging]
# level = "INFO"
"""


def _ensure_config_file(cfg: Config) -> bool:
    """Create a skeleton config at the platform default if none exists.

    Readers never write (ADR-0046); creation belongs to init. An explicit
    ``--config``/env path must already exist (load_config enforces it), so
    only the platform-default location can be missing here. Returns whether
    a file was created.
    """
    if cfg.loaded_from_file or cfg.source is not ConfigSource.DEFAULT:
        return False
    cfg.path.parent.mkdir(parents=True, exist_ok=True)
    set_owner_only(cfg.path.parent)
    cfg.path.write_text(
        _CONFIG_TEMPLATE.format(
            db_path=toml_quote(str(cfg.database.path)),
            backup_dir=toml_quote(str(cfg.backup.directory)),
        ),
        encoding="utf-8",
    )
    set_owner_only(cfg.path)
    return True
