"""CLI surface for ``db backup`` and ``db restore`` (ADR-0038)."""

import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from healthspan.backup import list_backups
from healthspan.cli import app
from healthspan.fsperm import owner_only_exposure
from healthspan.keyparams import sidecar_path
from healthspan.migrate import target_version

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
    assert f"Restored database at schema version {target_version()}" in result.output
    assert "moved aside" in result.output  # the previous live file is kept
    assert list(initialized.parent.glob("hs.db.pre-restore-*"))


def test_restore_explicit_file(initialized: Path) -> None:
    assert _backup(initialized).exit_code == 0
    backup = list_backups(initialized.parent / "backups")[0]
    result = _run(initialized, ["db", "restore", str(backup)], f"{PASSPHRASE}\n")
    assert result.exit_code == 0, result.output
    assert f"Restored database at schema version {target_version()}" in result.output


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


# --------------------------------------------------------------------------
# Backup-archive permission verification (ADR-0066 decision 3)
# --------------------------------------------------------------------------


def _expose(path: Path) -> Path:
    """Make a file readable beyond its owner, on either platform."""
    if os.name == "posix":
        path.chmod(0o644)
    else:
        subprocess.run(  # noqa: S603 - fixed executable, no shell
            ["icacls", str(path), "/grant", "*S-1-1-0:(R)"],  # noqa: S607
            capture_output=True,
            encoding="oem",
            errors="replace",
            check=True,
        )
    return path


def test_restore_repairs_an_exposed_source_pair(initialized: Path) -> None:
    """The tar-arrival case: a backup handed to restore from elsewhere.

    ``db backup`` protects what it publishes, so the only way a published
    pair is broad is that it came from somewhere the platform did not write.
    """
    assert _backup(initialized).exit_code == 0
    backup = list_backups(initialized.parent / "backups")[0]
    _expose(backup)
    _expose(sidecar_path(backup))

    result = _run(initialized, ["db", "restore", str(backup)], f"{PASSPHRASE}\n")

    assert result.exit_code == 0, result.output
    assert "accessible beyond its owner" in result.output
    assert owner_only_exposure(backup) is None
    assert owner_only_exposure(sidecar_path(backup)) is None


def test_backup_sweeps_the_retained_archive(initialized: Path) -> None:
    """A drifted older backup is repaired the next time the archive is written."""
    assert _backup(initialized).exit_code == 0
    older = list_backups(initialized.parent / "backups")[0]
    _expose(older)

    result = _backup(initialized)

    assert result.exit_code == 0, result.output
    assert owner_only_exposure(older) is None


def test_exclusive_access_repairs_every_swept_surface(initialized: Path) -> None:
    """The sweep rides the lock, so every direct-database command carries it.

    All four surfaces go through the real `exclusive_database_access` wiring:
    a dropped config path or backup directory would survive every other test.
    """
    backups = initialized.parent / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    database = initialized.parent / "hs.db"
    surfaces = [initialized, database, sidecar_path(database), backups]
    for surface in surfaces:
        _expose(surface)
        assert owner_only_exposure(surface) is not None  # the setup took hold

    result = _run(initialized, ["db", "migrate"], f"{PASSPHRASE}\n")

    assert result.exit_code == 0, result.output
    assert "accessible beyond its owner" in result.output
    for surface in surfaces:
        assert owner_only_exposure(surface) is None, surface


def test_command_runs_when_a_repair_cannot_be_applied(
    initialized: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Degrade to a warning, not to a refusal — through a real command."""
    from healthspan import permcheck
    from healthspan.fsperm import PermissionSetError

    def _deny(_path: Path) -> None:
        raise PermissionSetError("simulated read-only mount")

    _expose(initialized.parent / "hs.db")
    monkeypatch.setattr(permcheck, "set_owner_only", _deny)

    result = _run(initialized, ["db", "migrate"], f"{PASSPHRASE}\n")

    assert result.exit_code == 0, result.output
    assert "could not be restricted automatically" in result.output
    assert "simulated read-only mount" in result.output
