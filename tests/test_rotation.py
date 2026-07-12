"""Rotation and mode conversion: ADR-0028 shared mechanics and salt semantics."""

from collections.abc import Callable

import pytest
import sqlcipher3

from healthspan import db, keychain, rotation
from healthspan.config import Config
from healthspan.kdf import derive_db_key
from healthspan.keyparams import KeyMode, read_keyparams, sidecar_path
from healthspan.provisioning import initialize

PASSPHRASE = "a perfectly reasonable passphrase"


def _open_with(cfg: Config, passphrase: str, *, expect_row: bool = False) -> None:
    """Assert the database opens with the current sidecar + passphrase.

    With ``expect_row``, also assert the row `_seed_row` wrote survived the
    rekey intact (testing-strategy.md rekey security test).
    """
    params = read_keyparams(sidecar_path(cfg.database.path))
    if params.mode is KeyMode.TWO_FACTOR:
        salt = keychain.load_secret_key()
    else:
        assert params.salt is not None
        salt = params.salt
    key = derive_db_key(passphrase, salt, params)
    conn = db.connect(cfg.database.path, key)
    try:
        if expect_row:
            assert conn.execute("SELECT v FROM t").fetchone() == ("payload",)
    finally:
        db.close(conn)


def _seed_row(cfg: Config, passphrase: str) -> None:
    params = read_keyparams(sidecar_path(cfg.database.path))
    salt = keychain.load_secret_key() if params.salt is None else params.salt
    key = derive_db_key(passphrase, salt, params)
    conn = db.connect(cfg.database.path, key)
    conn.execute("CREATE TABLE t (v TEXT) STRICT")
    conn.execute("INSERT INTO t VALUES (?)", ("payload",))
    db.close(conn)


def test_unlock_rejects_wrong_passphrase(make_config: Callable[[], Config]) -> None:
    cfg = make_config()
    initialize(cfg, PASSPHRASE, KeyMode.PASSPHRASE_ONLY)
    with pytest.raises(rotation.RotationError, match="wrong passphrase"):
        rotation.unlock(cfg, "not the passphrase!!")


def test_change_passphrase_two_factor_keeps_secret_key(
    make_config: Callable[[], Config],
) -> None:
    cfg = make_config()
    initialize(cfg, PASSPHRASE, KeyMode.TWO_FACTOR)
    secret_before = keychain.load_secret_key()
    unlocked = rotation.unlock(cfg, PASSPHRASE)
    new_pass = "an entirely different phrase"
    result = rotation.change_passphrase(cfg, unlocked, new_pass)
    assert keychain.load_secret_key() == secret_before
    assert result.backup_database is not None
    assert result.backup_database.exists()
    _open_with(cfg, new_pass)
    with pytest.raises(rotation.RotationError):
        rotation.unlock(cfg, PASSPHRASE)
    # The pre-rekey backup still opens with the OLD credentials
    # (testing-strategy.md rekey security test; ADR-0028 non-retroactivity).
    old_key = derive_db_key(PASSPHRASE, unlocked.salt, unlocked.params)
    conn = db.connect(result.backup_database, old_key)
    db.close(conn)


def test_change_passphrase_passphrase_only_regenerates_salt(
    make_config: Callable[[], Config],
) -> None:
    cfg = make_config()
    initialize(cfg, PASSPHRASE, KeyMode.PASSPHRASE_ONLY)
    salt_before = read_keyparams(sidecar_path(cfg.database.path)).salt
    unlocked = rotation.unlock(cfg, PASSPHRASE)
    new_pass = "an entirely different phrase"
    rotation.change_passphrase(cfg, unlocked, new_pass)
    params_after = read_keyparams(sidecar_path(cfg.database.path))
    assert params_after.salt != salt_before
    assert params_after.rotated_utc
    _open_with(cfg, new_pass)


def test_rotate_secret_key_two_factor_replaces_keychain_and_data_survives(
    make_config: Callable[[], Config],
) -> None:
    cfg = make_config()
    initialize(cfg, PASSPHRASE, KeyMode.TWO_FACTOR)
    _seed_row(cfg, PASSPHRASE)
    secret_before = keychain.load_secret_key()
    unlocked = rotation.unlock(cfg, PASSPHRASE)
    result = rotation.rotate_secret_key(cfg, unlocked, PASSPHRASE)
    assert result.new_secret_key is not None
    assert result.new_secret_key != secret_before
    assert keychain.load_secret_key() == result.new_secret_key
    # No fresh old-key kit (unlike convert-mode): the user retains their
    # existing kit for pre-rotation backups (ADR-0028 non-retroactivity).
    assert result.old_secret_key is None
    _open_with(cfg, PASSPHRASE, expect_row=True)


def test_rotate_secret_key_passphrase_only_rotates_salt(
    make_config: Callable[[], Config],
) -> None:
    cfg = make_config()
    initialize(cfg, PASSPHRASE, KeyMode.PASSPHRASE_ONLY)
    salt_before = read_keyparams(sidecar_path(cfg.database.path)).salt
    unlocked = rotation.unlock(cfg, PASSPHRASE)
    result = rotation.rotate_secret_key(cfg, unlocked, PASSPHRASE)
    assert result.new_secret_key is None  # no kit: no second factor exists
    assert read_keyparams(sidecar_path(cfg.database.path)).salt != salt_before
    _open_with(cfg, PASSPHRASE)


def test_convert_to_two_factor(make_config: Callable[[], Config]) -> None:
    cfg = make_config()
    initialize(cfg, PASSPHRASE, KeyMode.PASSPHRASE_ONLY)
    _seed_row(cfg, PASSPHRASE)
    unlocked = rotation.unlock(cfg, PASSPHRASE)
    result = rotation.convert_mode(cfg, unlocked, PASSPHRASE, KeyMode.TWO_FACTOR)
    assert result.new_secret_key is not None
    params = read_keyparams(sidecar_path(cfg.database.path))
    assert params.mode is KeyMode.TWO_FACTOR
    assert params.salt is None  # the secret key is the salt now
    _open_with(cfg, PASSPHRASE, expect_row=True)


def test_convert_to_passphrase_only_offers_old_key_and_clears_keychain(
    make_config: Callable[[], Config],
) -> None:
    cfg = make_config()
    initialize(cfg, PASSPHRASE, KeyMode.TWO_FACTOR)
    old_secret = keychain.load_secret_key()
    unlocked = rotation.unlock(cfg, PASSPHRASE)
    result = rotation.convert_mode(cfg, unlocked, PASSPHRASE, KeyMode.PASSPHRASE_ONLY)
    assert result.old_secret_key == old_secret  # for the final Recovery Kit
    with pytest.raises(keychain.KeychainError, match="no secret key"):
        keychain.load_secret_key()
    params = read_keyparams(sidecar_path(cfg.database.path))
    assert params.mode is KeyMode.PASSPHRASE_ONLY
    _open_with(cfg, PASSPHRASE)


def test_convert_refuses_same_mode(make_config: Callable[[], Config]) -> None:
    cfg = make_config()
    initialize(cfg, PASSPHRASE, KeyMode.TWO_FACTOR)
    unlocked = rotation.unlock(cfg, PASSPHRASE)
    with pytest.raises(rotation.RotationError, match="already in two-factor"):
        rotation.convert_mode(cfg, unlocked, PASSPHRASE, KeyMode.TWO_FACTOR)


def test_failed_backup_aborts_rekey_leaving_database_unchanged(
    make_config: Callable[[], Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rekey gates on backup success: no backup, no change (ADR-0028)."""
    from healthspan.backup import BackupError

    cfg = make_config()
    initialize(cfg, PASSPHRASE, KeyMode.PASSPHRASE_ONLY)
    sidecar_before = sidecar_path(cfg.database.path).read_bytes()
    unlocked = rotation.unlock(cfg, PASSPHRASE)

    def _boom(*args: object, **kwargs: object) -> object:
        raise BackupError("simulated backup verification failure")

    monkeypatch.setattr(rotation, "create_verified_backup", _boom)
    with pytest.raises(BackupError):
        rotation.change_passphrase(cfg, unlocked, "would-be new passphrase")
    assert sidecar_path(cfg.database.path).read_bytes() == sidecar_before
    _open_with(cfg, PASSPHRASE)  # old credentials still open the live file


def test_rekey_survives_a_close_failure_after_a_successful_rekey(
    make_config: Callable[[], Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-fix bug: ``db.rekey`` + ``db.close`` sat under one
    ``except BaseException: pending.unlink()``, so a connection-close failure
    AFTER a successful rekey deleted the pending sidecar and stranded the
    database (rekeyed, but with the sidecar still recording the old params and
    no ``.pending`` recovery hint). The fix must swallow that close failure —
    the rekey already succeeded — and complete the sidecar install."""
    cfg = make_config()
    initialize(cfg, PASSPHRASE, KeyMode.PASSPHRASE_ONLY)
    _seed_row(cfg, PASSPHRASE)
    unlocked = rotation.unlock(cfg, PASSPHRASE)  # real db.close runs here
    sidecar = sidecar_path(cfg.database.path)
    pending = sidecar.with_name(sidecar.name + ".pending")
    new_pass = "an entirely different passphrase"

    real_close = db.close

    def _close_then_fail(conn: sqlcipher3.Connection) -> None:
        real_close(conn)  # do the real close, THEN signal a failure
        raise RuntimeError("simulated close failure after a successful rekey")

    monkeypatch.setattr(db, "close", _close_then_fail)
    # Must NOT raise: pre-fix this propagated and deleted the pending sidecar;
    # the fix installs the sidecar and completes the rotation.
    rotation.change_passphrase(cfg, unlocked, new_pass, backup=False)
    monkeypatch.undo()

    assert not pending.exists()  # no stranded pending file
    _open_with(cfg, new_pass)  # the NEW passphrase opens the rekeyed database
    with pytest.raises(rotation.RotationError):  # the OLD one no longer does
        rotation.unlock(cfg, PASSPHRASE)


def test_rotation_without_backup_skips_backup_dir(
    make_config: Callable[[], Config],
) -> None:
    cfg = make_config()
    initialize(cfg, PASSPHRASE, KeyMode.PASSPHRASE_ONLY)
    unlocked = rotation.unlock(cfg, PASSPHRASE)
    result = rotation.rotate_secret_key(cfg, unlocked, PASSPHRASE, backup=False)
    assert result.backup_database is None
    assert not cfg.backup.directory.exists()


def test_keychain_store_failure_after_rekey_returns_key_with_warning(
    make_config: Callable[[], Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A keychain failure must never discard the only copy of the new key."""
    cfg = make_config()
    initialize(cfg, PASSPHRASE, KeyMode.TWO_FACTOR)
    unlocked = rotation.unlock(cfg, PASSPHRASE)

    def _boom(secret: bytes) -> None:
        raise keychain.KeychainError("simulated keychain outage")

    monkeypatch.setattr(keychain, "store_secret_key", _boom)
    result = rotation.rotate_secret_key(cfg, unlocked, PASSPHRASE)
    assert result.new_secret_key is not None  # the key is still handed back
    assert result.keychain_warning is not None
    assert "ONLY copy" in result.keychain_warning
    # The returned key (destined for the Recovery Kit) opens the database.
    params = read_keyparams(sidecar_path(cfg.database.path))
    key = derive_db_key(PASSPHRASE, result.new_secret_key, params)
    conn = db.connect(cfg.database.path, key)
    db.close(conn)


def test_sidecar_write_failure_aborts_before_rekey(
    make_config: Callable[[], Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pending sidecar is staged BEFORE the rekey: a write failure
    leaves the database untouched and no .pending file behind."""
    import healthspan.keyparams as keyparams_mod
    from healthspan.fsperm import PermissionSetError

    cfg = make_config()
    initialize(cfg, PASSPHRASE, KeyMode.PASSPHRASE_ONLY)
    sidecar = sidecar_path(cfg.database.path)
    sidecar_before = sidecar.read_bytes()
    unlocked = rotation.unlock(cfg, PASSPHRASE)

    def _deny(path: object) -> None:
        raise PermissionSetError("simulated ACL failure")

    monkeypatch.setattr(keyparams_mod, "set_owner_only", _deny)
    with pytest.raises(PermissionSetError):
        rotation.change_passphrase(cfg, unlocked, "a new passphrase here")
    assert sidecar.read_bytes() == sidecar_before
    assert not sidecar.with_name(sidecar.name + ".pending").exists()
    _open_with(cfg, PASSPHRASE)  # old credentials still open the live file
