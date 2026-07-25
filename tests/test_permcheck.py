"""Startup permission verification — the ADR-0066 graded posture.

Two implementations hide behind one contract (POSIX mode bits, a Windows
``icacls`` principal enumeration), so the layers are tested separately:
the *policy* tests stub the detector and run everywhere, the mode-bit and
ACL tests are each skipped off their own platform (testing-strategy.md,
Cross-Platform Testing).
"""

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from healthspan import permcheck
from healthspan.fsperm import (
    PermissionSetError,
    owner_only_exposure,
    restrict_command,
    set_owner_only,
)
from healthspan.keyparams import sidecar_path
from healthspan.permcheck import (
    BroadPermissionsError,
    refuse_if_exposed,
    repair_owner_only,
    verify_backup_pair,
    verify_startup_files,
)

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX mode-bit branch")
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows ACL branch")


def _broad_file(path: Path, content: bytes = b"ciphertext") -> Path:
    """A file the platform did not create: readable beyond its owner."""
    path.write_bytes(content)
    if os.name == "posix":
        path.chmod(0o644)
    else:
        _grant_everyone(path)
    return path


def _grant_everyone(path: Path) -> None:
    # S-1-1-0 is Everyone — a principal that is neither the owner nor one of
    # the two ADR-0066 treats as benign, so it must be reported.
    subprocess.run(  # noqa: S603 - fixed executable, no shell
        ["icacls", str(path), "/grant", "*S-1-1-0:(R)"],  # noqa: S607
        capture_output=True,
        encoding="oem",
        errors="replace",
        check=True,
    )


# --------------------------------------------------------------------------
# Repair posture: the platform's own files (decision 1)
# --------------------------------------------------------------------------


def test_broad_file_is_repaired_and_reported(tmp_path: Path) -> None:
    path = _broad_file(tmp_path / "healthspan.db")
    warnings: list[str] = []
    repair_owner_only(path, "database file", warnings.append)

    assert owner_only_exposure(path) is None  # the exposure is over
    assert len(warnings) == 1
    assert "database file" in warnings[0]
    assert str(path) in warnings[0]
    # The report must not let the owner believe the repair undid the read.
    assert "does not undo" in warnings[0]


def test_repair_is_idempotent(tmp_path: Path) -> None:
    path = _broad_file(tmp_path / "healthspan.db")
    warnings: list[str] = []
    repair_owner_only(path, "database file", warnings.append)
    repair_owner_only(path, "database file", warnings.append)
    # Exactly one report: the second start has nothing left to fix, so a
    # clean machine never accumulates a recurring warning to tune out.
    assert len(warnings) == 1


def test_owner_only_file_is_silent(tmp_path: Path) -> None:
    path = tmp_path / "healthspan.db"
    path.write_bytes(b"ciphertext")
    set_owner_only(path)
    warnings: list[str] = []
    repair_owner_only(path, "database file", warnings.append)
    assert warnings == []


def test_missing_file_is_silent(tmp_path: Path) -> None:
    warnings: list[str] = []
    repair_owner_only(tmp_path / "absent.db", "database file", warnings.append)
    assert warnings == []


@posix_only
def test_repair_restores_the_creation_mode(tmp_path: Path) -> None:
    path = _broad_file(tmp_path / "healthspan.db")
    repair_owner_only(path, "database file", lambda _m: None)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@posix_only
def test_directory_repair_uses_the_directory_mode(tmp_path: Path) -> None:
    directory = tmp_path / "backups"
    directory.mkdir(mode=0o755)
    warnings: list[str] = []
    repair_owner_only(directory, "backup directory", warnings.append)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert len(warnings) == 1


@posix_only
def test_report_names_the_mode_it_found(tmp_path: Path) -> None:
    path = _broad_file(tmp_path / "healthspan.db")
    warnings: list[str] = []
    repair_owner_only(path, "database file", warnings.append)
    assert "-rw-r--r--" in warnings[0]


def _refuse_to_repair(_path: Path) -> None:
    raise PermissionSetError("read-only mount")


def _always_exposed(_path: Path, *, acl_scan: bool = True) -> str | None:
    return "mode -rw-r--r--"


def test_unrepairable_file_degrades_to_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repair that cannot be applied must not block the owner out.

    Every path under the repair posture is ciphertext or configuration, so
    refusing here would trade a rare exposure for a common lockout — the
    failure mode ADR-0066 is built to avoid.
    """
    path = tmp_path / "healthspan.db"
    path.write_bytes(b"ciphertext")
    monkeypatch.setattr(permcheck, "owner_only_exposure", _always_exposed)
    monkeypatch.setattr(permcheck, "set_owner_only", _refuse_to_repair)

    warnings: list[str] = []
    repair_owner_only(path, "database file", warnings.append)

    assert len(warnings) == 1
    assert "could not be restricted" in warnings[0]
    assert "read-only mount" in warnings[0]  # the underlying cause survives
    assert "chmod 600" in warnings[0] or "icacls" in warnings[0]  # the remedy


# --------------------------------------------------------------------------
# Refuse posture: the passphrase file (decision 2)
# --------------------------------------------------------------------------


def test_exposed_passphrase_file_refuses(tmp_path: Path) -> None:
    path = _broad_file(tmp_path / "pp.secret", b"a perfectly reasonable passphrase\n")
    with pytest.raises(BroadPermissionsError) as excinfo:
        refuse_if_exposed(path, "passphrase file")
    message = str(excinfo.value)
    assert "passphrase file" in message
    assert "rotate" in message
    assert "chmod 600" in message or "icacls" in message


def test_refused_passphrase_file_is_left_untouched(tmp_path: Path) -> None:
    """Never repaired: a quiet chmod would hide a disclosed secret."""
    path = _broad_file(tmp_path / "pp.secret", b"a perfectly reasonable passphrase\n")
    with pytest.raises(BroadPermissionsError):
        refuse_if_exposed(path, "passphrase file")
    assert owner_only_exposure(path) is not None


def test_owner_only_passphrase_file_is_accepted(tmp_path: Path) -> None:
    path = tmp_path / "pp.secret"
    path.write_bytes(b"a perfectly reasonable passphrase\n")
    set_owner_only(path)
    refuse_if_exposed(path, "passphrase file")  # must not raise


def test_missing_passphrase_file_is_not_this_check_s_error(tmp_path: Path) -> None:
    # Absence is the reader's error to report, with its own message.
    refuse_if_exposed(tmp_path / "absent", "passphrase file")


# --------------------------------------------------------------------------
# The sweep: which surfaces, and where (decision 3)
# --------------------------------------------------------------------------


def test_startup_sweep_covers_every_surface(tmp_path: Path) -> None:
    config = _broad_file(tmp_path / "config.toml", b"config_version = 1\n")
    database = _broad_file(tmp_path / "healthspan.db")
    sidecar = _broad_file(sidecar_path(database), b"format = 1\n")
    backups = tmp_path / "backups"
    backups.mkdir()
    if os.name == "posix":
        backups.chmod(0o755)
    else:
        _grant_everyone(backups)

    warnings: list[str] = []
    verify_startup_files(
        config_path=config,
        database_path=database,
        backup_dir=backups,
        warn=warnings.append,
    )

    assert len(warnings) == 4
    for path in (config, database, sidecar, backups):
        assert owner_only_exposure(path) is None


def test_backup_pair_covers_the_sidecar(tmp_path: Path) -> None:
    backup = _broad_file(tmp_path / "healthspan-20260725T000000Z.db")
    sidecar = _broad_file(sidecar_path(backup), b"format = 1\n")
    warnings: list[str] = []
    verify_backup_pair(backup, warnings.append)
    assert len(warnings) == 2
    assert owner_only_exposure(backup) is None
    assert owner_only_exposure(sidecar) is None


# --------------------------------------------------------------------------
# Detector: platform depth and the fail-open rule
# --------------------------------------------------------------------------


@posix_only
def test_posix_detector_reads_mode_bits(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"x")
    path.chmod(0o604)  # other-readable only: still an exposure
    exposure = owner_only_exposure(path)
    assert exposure is not None
    assert "-rw----r--" in exposure
    path.chmod(0o600)
    assert owner_only_exposure(path) is None


@posix_only
def test_acl_scan_flag_is_inert_on_posix(tmp_path: Path) -> None:
    # The flag exists to keep a subprocess off the per-command config path on
    # Windows; on POSIX a mode read is already cheap, so nothing is skipped.
    path = _broad_file(tmp_path / "config.toml", b"config_version = 1\n")
    assert owner_only_exposure(path, acl_scan=False) is not None


@windows_only
def test_windows_detector_reports_a_foreign_principal(tmp_path: Path) -> None:
    path = tmp_path / "healthspan.db"
    path.write_bytes(b"ciphertext")
    set_owner_only(path)
    assert owner_only_exposure(path) is None
    _grant_everyone(path)
    exposure = owner_only_exposure(path)
    assert exposure is not None
    assert "ACL grants held by" in exposure
    # The offending principal is named, not just counted — a report that
    # reached this branch with an empty list would be useless to act on.
    assert "everyone" in exposure.lower()


@windows_only
def test_windows_detector_ignores_system_and_administrators(tmp_path: Path) -> None:
    """The benign-set decision: members of either can rewrite any DACL.

    Reporting them would fire on ordinary profile files and train the owner
    to ignore the check (ADR-0066 §3).
    """
    path = tmp_path / "healthspan.db"
    path.write_bytes(b"ciphertext")
    set_owner_only(path)
    for sid in ("*S-1-5-18", "*S-1-5-32-544"):
        subprocess.run(  # noqa: S603 - fixed executable, no shell
            ["icacls", str(path), "/grant", f"{sid}:(F)"],  # noqa: S607
            capture_output=True,
            encoding="oem",
            errors="replace",
            check=True,
        )
    assert owner_only_exposure(path) is None


@windows_only
def test_benign_principals_resolve_from_their_well_known_sids(tmp_path: Path) -> None:
    """Matched by localized name, resolved from the SID — not hardcoded English."""
    from healthspan.fsperm import (
        _account_name_for_sid,  # pyright: ignore[reportPrivateUsage]
        _benign_windows_principals,  # pyright: ignore[reportPrivateUsage]
    )

    resolved = _account_name_for_sid("S-1-5-32-544")
    assert resolved is not None
    assert "\\" in resolved  # DOMAIN\Name, the form icacls prints
    assert resolved.lower() in _benign_windows_principals()
    # The English backstop survives a lookup failure on any locale.
    assert "nt authority\\system" in _benign_windows_principals()


@windows_only
def test_acl_scan_can_be_skipped_on_the_hot_path(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_bytes(b"config_version = 1\n")
    set_owner_only(path)
    _grant_everyone(path)
    assert owner_only_exposure(path) is not None
    # load_config passes acl_scan=False so no CLI invocation spawns icacls;
    # the startup sweep is where the config file gets its full-depth look.
    assert owner_only_exposure(path, acl_scan=False) is None


@windows_only
@pytest.mark.parametrize(
    "error",
    [PermissionSetError("icacls exited 5"), OSError("icacls is not on PATH")],
    ids=["probe-failed", "probe-missing"],
)
def test_undeterminable_check_reports_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """Fail open: a broken *or absent* probe must not become a lockout.

    The OSError case is the probe tools themselves being unreachable — a
    FileNotFoundError out of subprocess when icacls or whoami is off PATH.
    Escaping the handler aborts startup with a raw traceback, the opposite
    of the documented behavior.
    """
    import healthspan.fsperm as fsperm_mod

    path = _broad_file(tmp_path / "healthspan.db")

    def _raise(_path: Path) -> list[str]:
        raise error

    monkeypatch.setattr(fsperm_mod, "_acl_principals", _raise)
    assert owner_only_exposure(path) is None


# --------------------------------------------------------------------------
# The remedy command: it has to run as printed (decision 4 ships no override)
# --------------------------------------------------------------------------


@posix_only
def test_posix_remedy_matches_the_target_kind(tmp_path: Path) -> None:
    file = tmp_path / "f"
    file.write_bytes(b"x")
    directory = tmp_path / "d"
    directory.mkdir()
    assert restrict_command(file) == f"chmod 600 {file}"
    assert restrict_command(directory) == f"chmod 700 {directory}"


@windows_only
def test_windows_remedy_names_a_resolved_account(tmp_path: Path) -> None:
    """No `%USERNAME%`: it expands in cmd.exe alone.

    PowerShell is this project's shell, and ADR-0066 §4 ships no override —
    so a remedy that fails when pasted leaves the owner with no way out of
    the refusal at all.
    """
    from healthspan.fsperm import (
        _current_windows_user,  # pyright: ignore[reportPrivateUsage]
    )

    path = _broad_file(tmp_path / "pp.secret", b"a perfectly reasonable passphrase\n")
    with pytest.raises(BroadPermissionsError) as excinfo:
        refuse_if_exposed(path, "passphrase file")
    message = str(excinfo.value)
    assert "%USERNAME%" not in message
    assert _current_windows_user() in message
    assert "icacls" in message


def _no_command(_path: Path) -> str | None:
    return None


def test_remedy_falls_back_when_no_command_can_be_composed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The account lookup can fail for the same reason the probe did."""
    path = tmp_path / "pp.secret"
    path.write_bytes(b"a perfectly reasonable passphrase\n")
    monkeypatch.setattr(permcheck, "owner_only_exposure", _always_exposed)
    monkeypatch.setattr(permcheck, "restrict_command", _no_command)

    with pytest.raises(BroadPermissionsError) as excinfo:
        refuse_if_exposed(path, "passphrase file")
    message = str(excinfo.value)
    # Prose, not a half-composed command the owner would paste and lose to.
    assert "Restrict it with:" not in message
    assert "remove inherited entries" in message


@posix_only
def test_unreadable_stat_reports_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _broad_file(tmp_path / "healthspan.db")

    def _boom(_self: Path) -> os.stat_result:
        raise OSError("stat failed")

    monkeypatch.setattr(Path, "stat", _boom)
    assert owner_only_exposure(path) is None
