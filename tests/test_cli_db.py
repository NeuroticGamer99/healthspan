"""CLI surface for ``db backup`` and ``db restore`` (ADR-0038)."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from healthspan.backup import list_backups
from healthspan.cli import app

runner = CliRunner()

PASSPHRASE = "a perfectly reasonable passphrase"


@pytest.fixture
def initialized(tmp_path: Path) -> Path:
    """A config file whose database is initialized and migrated to 0001."""
    config = tmp_path / "config.toml"
    config.write_text(
        'config_version = 1\n\n[database]\npath = "hs.db"\n\n'
        '[backup]\ndirectory = "backups"\nretention_count = 2\n',
        encoding="utf-8",
    )
    assert _run(config, ["init"], f"{PASSPHRASE}\n{PASSPHRASE}\n").exit_code == 0
    assert _run(config, ["db", "migrate"], f"{PASSPHRASE}\n").exit_code == 0
    return config


def _run(config: Path, args: list[str], input_text: str):
    return runner.invoke(app, ["--config", str(config), *args], input=input_text)


def _backup(config: Path):
    return _run(config, ["db", "backup"], f"{PASSPHRASE}\n")


def test_backup_creates_verified_pair(initialized: Path) -> None:
    result = _backup(initialized)
    assert result.exit_code == 0, result.output
    assert "Verified backup created" in result.output
    backup_dir = initialized.parent / "backups"
    assert len(list_backups(backup_dir)) == 1


def test_backup_permission_error_is_reported_cleanly(
    initialized: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # backup calls fsperm.set_owner_only, which raises PermissionSetError
    # (subclasses Exception, not OSError) on a Windows icacls failure. cli_db's
    # _run must catch it and exit cleanly, not surface a raw traceback.
    from healthspan import backup as backup_mod
    from healthspan.fsperm import PermissionSetError

    def _deny(path: Path) -> None:
        raise PermissionSetError("simulated ACL failure")

    monkeypatch.setattr(backup_mod, "set_owner_only", _deny)
    result = _backup(initialized)
    assert result.exit_code == 1
    assert "error:" in result.output
    assert "simulated ACL failure" in result.output
    assert "Traceback" not in result.output


def test_backup_prunes_beyond_retention(initialized: Path) -> None:
    _backup(initialized)
    _backup(initialized)
    result = _backup(initialized)  # third, retention_count is 2
    assert result.exit_code == 0, result.output
    assert "Pruned 1 old backup(s)" in result.output
    assert len(list_backups(initialized.parent / "backups")) == 2


def test_restore_latest_round_trips(initialized: Path) -> None:
    assert _backup(initialized).exit_code == 0
    result = _run(initialized, ["db", "restore", "--latest"], f"{PASSPHRASE}\n")
    assert result.exit_code == 0, result.output
    assert "Restored database at schema version 1" in result.output
    assert "moved aside" in result.output  # the previous live file is kept
    assert list(initialized.parent.glob("hs.db.pre-restore-*"))


def test_restore_explicit_file(initialized: Path) -> None:
    assert _backup(initialized).exit_code == 0
    backup = list_backups(initialized.parent / "backups")[0]
    result = _run(initialized, ["db", "restore", str(backup)], f"{PASSPHRASE}\n")
    assert result.exit_code == 0, result.output
    assert "Restored database at schema version 1" in result.output


def test_restore_requires_a_selection(initialized: Path) -> None:
    result = _run(initialized, ["db", "restore"], "")
    assert result.exit_code == 1
    assert "specify a backup file" in result.output


def test_restore_rejects_file_and_latest_together(initialized: Path) -> None:
    result = _run(initialized, ["db", "restore", "some.db", "--latest"], "")
    assert result.exit_code == 1
    assert "not both" in result.output


def test_restore_latest_without_backups_fails(initialized: Path) -> None:
    result = _run(initialized, ["db", "restore", "--latest"], f"{PASSPHRASE}\n")
    assert result.exit_code == 1
    assert "no published backups" in result.output
