"""CLI surface: init, keys commands, advisory policy, Recovery Kit output."""

import re
from pathlib import Path

import pytest
from conftest import InMemoryKeyring
from typer.testing import CliRunner

from healthspan.cli import app
from healthspan.keyparams import KeyMode, read_keyparams

runner = CliRunner()

PASSPHRASE = "a perfectly reasonable passphrase"
SHORT = "tiny pass"


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        'config_version = 1\n\n[database]\npath = "healthspan.db"\n\n'
        '[backup]\ndirectory = "backups"\n',
        encoding="utf-8",
    )
    return path


def _invoke(config_file: Path, args: list[str], input_text: str):
    return runner.invoke(app, ["--config", str(config_file), *args], input=input_text)


def _init(config_file: Path, *extra: str, passphrase: str = PASSPHRASE):
    return _invoke(config_file, ["init", *extra], f"{passphrase}\n{passphrase}\n")


def test_init_two_factor_creates_db_and_shows_kit(config_file: Path) -> None:
    result = _init(config_file)
    assert result.exit_code == 0, result.output
    assert "Encrypted database created" in result.output
    assert "HEALTHSPAN RECOVERY KIT" in result.output
    db_path = config_file.parent / "healthspan.db"
    assert db_path.exists()
    assert read_keyparams(config_file.parent / "healthspan.db.keyparams").mode is (
        KeyMode.TWO_FACTOR
    )
    # The kit's Base32 secret key never lands in any file init writes.
    assert db_path.read_bytes()[:16] != b"SQLite format 3\x00"


def test_init_passphrase_only_creates_salted_sidecar(config_file: Path) -> None:
    result = _init(config_file, "--key-from-passphrase")
    assert result.exit_code == 0, result.output
    assert "HEALTHSPAN RECOVERY KIT" not in result.output  # no kit: no secret key
    params = read_keyparams(config_file.parent / "healthspan.db.keyparams")
    assert params.mode is KeyMode.PASSPHRASE_ONLY
    assert params.salt is not None


def test_init_refuses_when_database_exists(config_file: Path) -> None:
    assert _init(config_file).exit_code == 0
    result = _init(config_file)
    assert result.exit_code == 1
    assert "already exists" in result.output


def test_short_passphrase_warns_and_requires_confirmation(config_file: Path) -> None:
    # Decline the short passphrase once, then provide a long one.
    result = _invoke(
        config_file,
        ["init", "--key-from-passphrase"],
        f"{SHORT}\n{SHORT}\nn\n{PASSPHRASE}\n{PASSPHRASE}\n",
    )
    assert result.exit_code == 0, result.output
    assert "easier to guess" in result.output


def test_short_passphrase_accepted_on_explicit_confirmation(
    config_file: Path,
) -> None:
    result = _invoke(
        config_file,
        ["init", "--key-from-passphrase"],
        f"{SHORT}\n{SHORT}\ny\n",
    )
    assert result.exit_code == 0, result.output  # advisory: warn, never refuse


def test_recovery_kit_command_renders_from_keychain(
    config_file: Path, fake_keychain: InMemoryKeyring
) -> None:
    assert _init(config_file).exit_code == 0
    result = _invoke(config_file, ["keys", "recovery-kit"], "")
    assert result.exit_code == 0, result.output
    assert "HEALTHSPAN RECOVERY KIT" in result.output


def test_recovery_kit_fails_cleanly_in_passphrase_only_mode(
    config_file: Path,
) -> None:
    assert _init(config_file, "--key-from-passphrase").exit_code == 0
    result = _invoke(config_file, ["keys", "recovery-kit"], "")
    assert result.exit_code == 1
    assert "no Recovery Kit exists" in result.output


def test_recovery_kit_output_writes_recognizably_named_file(
    config_file: Path, tmp_path: Path
) -> None:
    assert _init(config_file).exit_code == 0
    out_dir = tmp_path / "kit-out"
    out_dir.mkdir()
    result = _invoke(
        config_file, ["keys", "recovery-kit", "--output", str(out_dir)], ""
    )
    assert result.exit_code == 0, result.output
    written = list(out_dir.glob("healthspan-recovery-kit-*.txt"))
    assert len(written) == 1
    assert "encrypted storage" in result.output  # ADR-0033 digital-copy warning


def test_change_passphrase_cli_round_trip(config_file: Path) -> None:
    assert _init(config_file, "--key-from-passphrase").exit_code == 0
    new = "an entirely different phrase"
    result = _invoke(
        config_file,
        ["keys", "change-passphrase"],
        f"{PASSPHRASE}\n{new}\n{new}\n",
    )
    assert result.exit_code == 0, result.output
    assert "Verified backup created" in result.output
    assert "not retroactive" in result.output
    # New passphrase unlocks; old fails.
    ok = _invoke(
        config_file, ["keys", "change-passphrase"], f"{new}\n{new}2x!\n{new}2x!\n"
    )
    assert ok.exit_code == 0, ok.output
    bad = _invoke(config_file, ["keys", "change-passphrase"], f"{PASSPHRASE}\n")
    assert bad.exit_code == 1


def test_rotate_secret_key_cli_requires_confirmation_and_prints_new_kit(
    config_file: Path,
) -> None:
    assert _init(config_file).exit_code == 0
    declined = _invoke(config_file, ["keys", "rotate-secret-key"], f"{PASSPHRASE}\nn\n")
    assert declined.exit_code != 0  # aborted before any change
    result = _invoke(config_file, ["keys", "rotate-secret-key"], f"{PASSPHRASE}\ny\n")
    assert result.exit_code == 0, result.output
    # The confirmation tells the user to KEEP the old kit (it still opens
    # pre-rotation backups), not that it becomes invalid.
    assert "Keep your current kit too" in result.output
    assert "HEALTHSPAN RECOVERY KIT" in result.output


def test_rotate_secret_key_no_backup_warns(config_file: Path) -> None:
    assert _init(config_file, "--key-from-passphrase").exit_code == 0
    result = _invoke(
        config_file, ["keys", "rotate-secret-key", "--no-backup"], f"{PASSPHRASE}\n"
    )
    assert result.exit_code == 0, result.output
    assert "--no-backup skips" in result.output
    assert not (config_file.parent / "backups").exists()


def test_convert_mode_downgrade_warns_confirms_and_offers_final_kit(
    config_file: Path,
) -> None:
    assert _init(config_file).exit_code == 0
    result = _invoke(
        config_file,
        ["keys", "convert-mode", "--to", "passphrase-only"],
        f"{PASSPHRASE}\ny\ny\n",
    )
    assert result.exit_code == 0, result.output
    assert "DOWNGRADES" in result.output
    assert "HEALTHSPAN RECOVERY KIT" in result.output  # final kit for the old key
    params = read_keyparams(config_file.parent / "healthspan.db.keyparams")
    assert params.mode is KeyMode.PASSPHRASE_ONLY


def test_convert_mode_upgrade_generates_kit(config_file: Path) -> None:
    assert _init(config_file, "--key-from-passphrase").exit_code == 0
    result = _invoke(
        config_file,
        ["keys", "convert-mode", "--to", "two-factor"],
        f"{PASSPHRASE}\n",
    )
    assert result.exit_code == 0, result.output
    assert "HEALTHSPAN RECOVERY KIT" in result.output
    params = read_keyparams(config_file.parent / "healthspan.db.keyparams")
    assert params.mode is KeyMode.TWO_FACTOR
    assert params.salt is None


def test_convert_mode_rejects_unknown_target(config_file: Path) -> None:
    assert _init(config_file).exit_code == 0
    result = _invoke(
        config_file, ["keys", "convert-mode", "--to", "sideways"], f"{PASSPHRASE}\n"
    )
    assert result.exit_code == 1
    assert "unknown mode" in result.output


def test_init_creates_skeleton_config_at_default_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from healthspan import paths

    monkeypatch.setattr(paths, "config_dir", lambda: tmp_path / "cfg")
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path / "data")
    monkeypatch.delenv("HEALTHSPAN_CONFIG", raising=False)
    result = runner.invoke(
        app, ["init", "--key-from-passphrase"], input=f"{PASSPHRASE}\n{PASSPHRASE}\n"
    )
    assert result.exit_code == 0, result.output
    config_path = tmp_path / "cfg" / "config.toml"
    assert config_path.exists()
    assert "config_version = 1" in config_path.read_text(encoding="utf-8")
    assert (tmp_path / "data" / "healthspan.db").exists()
    # The database path is pinned (active, not commented) to where init
    # actually created the file, and the skeleton re-parses cleanly.
    from healthspan.config import load_config

    cfg = load_config(flag=config_path)
    assert cfg.database.path == tmp_path / "data" / "healthspan.db"


def test_init_keychain_failure_leaves_nothing_on_disk(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two-factor init stores the secret key BEFORE creating files, so a
    keychain outage aborts cleanly and init stays re-runnable."""
    from healthspan import keychain

    def _boom(secret: bytes) -> None:
        raise keychain.KeychainError("simulated keychain outage")

    monkeypatch.setattr(keychain, "store_secret_key", _boom)
    result = _init(config_file)
    assert result.exit_code == 1
    assert "keychain outage" in result.output
    assert "Traceback" not in result.output
    assert not (config_file.parent / "healthspan.db").exists()
    assert not (config_file.parent / "healthspan.db.keyparams").exists()
    monkeypatch.undo()
    assert _init(config_file).exit_code == 0  # re-run succeeds


def test_keychain_store_failure_cli_still_prints_kit(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _init(config_file).exit_code == 0
    from healthspan import keychain

    def _boom(secret: bytes) -> None:
        raise keychain.KeychainError("simulated keychain outage")

    monkeypatch.setattr(keychain, "store_secret_key", _boom)
    result = _invoke(config_file, ["keys", "rotate-secret-key"], f"{PASSPHRASE}\ny\n")
    assert result.exit_code == 0, result.output
    assert "ONLY copy" in result.output
    assert "HEALTHSPAN RECOVERY KIT" in result.output


def test_permission_error_is_reported_cleanly_not_a_traceback(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import healthspan.keyparams as keyparams_mod
    from healthspan.fsperm import PermissionSetError

    assert _init(config_file, "--key-from-passphrase").exit_code == 0

    def _deny(path: Path) -> None:
        raise PermissionSetError("simulated ACL failure")

    monkeypatch.setattr(keyparams_mod, "set_owner_only", _deny)
    new = "an entirely different phrase"
    result = _invoke(
        config_file, ["keys", "change-passphrase"], f"{PASSPHRASE}\n{new}\n{new}\n"
    )
    assert result.exit_code == 1
    assert "error: simulated ACL failure" in result.output
    assert "Traceback" not in result.output


def test_convert_downgrade_renders_final_kit_on_prompt_eof(
    config_file: Path,
) -> None:
    """An exhausted stdin after the committed downgrade must default to
    rendering the old key's final kit, not abort with a failure."""
    assert _init(config_file).exit_code == 0
    result = _invoke(
        config_file,
        ["keys", "convert-mode", "--to", "passphrase-only"],
        f"{PASSPHRASE}\ny\n",  # no answer for the final-kit confirm
    )
    assert result.exit_code == 0, result.output
    assert "HEALTHSPAN RECOVERY KIT" in result.output


def test_kit_output_write_failure_keeps_terminal_copy(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _init(config_file).exit_code == 0
    from healthspan import recovery_kit

    def _boom(secret: bytes, output: Path) -> Path:
        raise OSError("simulated disk full")

    monkeypatch.setattr(recovery_kit, "write_kit", _boom)
    result = _invoke(
        config_file,
        ["keys", "recovery-kit", "--output", str(config_file.parent)],
        "",
    )
    assert result.exit_code == 0, result.output
    assert "HEALTHSPAN RECOVERY KIT" in result.output  # terminal copy intact
    assert "could not write the Recovery Kit file" in result.output


def test_render_kit_without_qr_carries_note() -> None:
    from healthspan.kdf import generate_secret_key
    from healthspan.recovery_kit import render_kit

    text = render_kit(generate_secret_key(), include_qr=False)
    assert "QR code omitted" in text
    assert "█" not in text  # no half-block cells


# --- init --restore: new-machine credential recovery (ADR-0038) ----------


def _kit_secret_key(config_file: Path) -> str:
    """The grouped Base32 secret key from the rendered Recovery Kit."""
    kit = _invoke(config_file, ["keys", "recovery-kit"], "").output
    match = re.search(r"Secret key \(Base32\):\s*\n\s*\n\s*([A-Z2-7-]+)", kit)
    assert match is not None, kit
    return match.group(1)


def test_init_restore_stores_secret_key_in_keychain(
    config_file: Path, fake_keychain: InMemoryKeyring
) -> None:
    assert _init(config_file).exit_code == 0
    b32 = _kit_secret_key(config_file)
    fake_keychain.store.clear()  # a brand-new machine has an empty keychain

    result = _invoke(config_file, ["init", "--restore"], f"{b32}\n")
    assert result.exit_code == 0, result.output
    assert "Secret key stored in the OS keychain" in result.output
    # The same key is now retrievable — db restore will derive from it.
    assert _kit_secret_key(config_file) == b32


def test_init_restore_warns_when_replacing_an_existing_key(
    config_file: Path,
) -> None:
    assert _init(config_file).exit_code == 0
    b32 = _kit_secret_key(config_file)  # keychain still holds a key
    result = _invoke(config_file, ["init", "--restore"], f"{b32}\n")
    assert result.exit_code == 0, result.output
    assert "existing secret key in the keychain was replaced" in result.output


def test_init_restore_rejects_an_invalid_key(config_file: Path) -> None:
    result = _invoke(config_file, ["init", "--restore"], "not-a-real-key\n")
    assert result.exit_code == 1
    assert "not a valid Recovery Kit secret key" in result.output


def test_init_restore_incompatible_with_passphrase_only(config_file: Path) -> None:
    result = _invoke(config_file, ["init", "--restore", "--key-from-passphrase"], "")
    assert result.exit_code == 1
    assert "incompatible" in result.output


def test_init_restore_rejects_output_flag(config_file: Path) -> None:
    # --restore renders no new kit, so --output is meaningless; reject it
    # instead of silently ignoring it.
    result = _invoke(config_file, ["init", "--restore", "--output", "kit.txt"], "")
    assert result.exit_code == 1
    assert "--output has no effect with --restore" in result.output
