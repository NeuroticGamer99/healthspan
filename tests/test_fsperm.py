"""Owner-only protection: the ADR-0046 write-side obligation, both platforms."""

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from healthspan.fsperm import (
    PermissionSetError,
    _removal_target,  # pyright: ignore[reportPrivateUsage]
    set_owner_only,
)


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode-bit branch")
def test_posix_file_gets_owner_only_mode(tmp_path: Path) -> None:
    path = tmp_path / "secret.txt"
    path.write_text("x", encoding="utf-8")
    set_owner_only(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode-bit branch")
def test_posix_directory_gets_owner_only_mode(tmp_path: Path) -> None:
    directory = tmp_path / "private"
    directory.mkdir()
    set_owner_only(directory)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACL branch")
def test_windows_acl_grants_only_the_current_user(tmp_path: Path) -> None:
    path = tmp_path / "secret.txt"
    path.write_text("x", encoding="utf-8")
    # Stamp an explicit non-owner grant first (SID S-1-1-0 = Everyone) —
    # /inheritance:r alone would not remove it, which is exactly the CI-
    # runner condition (explicit SYSTEM/Administrators ACEs on temp files).
    subprocess.run(  # noqa: S603 - fixed executable, no shell
        ["icacls", str(path), "/grant", "*S-1-1-0:(R)"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    set_owner_only(path)
    listing = subprocess.run(  # noqa: S603 - fixed executable, no shell
        ["icacls", str(path)],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    user = subprocess.run(
        ["whoami"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    grant_lines = [
        line.strip()
        for line in listing.splitlines()
        if ":" in line and "(" in line and line.strip()
    ]
    # Exactly one explicit grant: full control for the current user.
    assert len(grant_lines) == 1, listing
    assert user.lower() in grant_lines[0].lower()
    assert "(F)" in grant_lines[0]


def test_logon_session_principal_translates_to_sid() -> None:
    # The pseudo-name cannot be mapped back by icacls; the SID can.
    assert (
        _removal_target("NT AUTHORITY\\LogonSessionId_0_5626116")
        == "*S-1-5-5-0-5626116"
    )
    # Bare form (no authority prefix) and case variance still translate.
    assert _removal_target("logonsessionid_3_42") == "*S-1-5-5-3-42"
    # Ordinary principals pass through untouched.
    assert _removal_target("BUILTIN\\Administrators") == "BUILTIN\\Administrators"
    # A name merely *containing* the pattern mid-string is not a match.
    assert _removal_target("LogonSessionId_1_2_backup") == "LogonSessionId_1_2_backup"
    # A bare SID (icacls's rendering of an unresolvable principal, e.g. a
    # deleted account) gets the *SID form rather than a name lookup.
    assert (
        _removal_target("S-1-5-21-1004336348-1177238915-682003330-1001")
        == "*S-1-5-21-1004336348-1177238915-682003330-1001"
    )
    # The logon-session branch wins over the generic SID branch.
    assert _removal_target("NT AUTHORITY\\LogonSessionId_7_8") == "*S-1-5-5-7-8"
    # Something SID-like but malformed is not translated.
    assert _removal_target("S-1-") == "S-1-"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACL branch")
def test_windows_removes_unmappable_logon_session_ace(tmp_path: Path) -> None:
    """The `init` regression: a logon-session ACE must not abort protection.

    A file created inside an already-restricted directory (no inheritable
    ACEs) receives the creator token's default DACL, which in a non-elevated
    session carries an ``NT AUTHORITY\\LogonSessionId_<hi>_<lo>:(RX)`` entry.
    That pseudo-name is unmappable, so removal must go through the SID.
    Fabricate the ACE deterministically by granting to a logon-session SID.
    """
    path = tmp_path / "secret.txt"
    path.write_text("x", encoding="utf-8")
    subprocess.run(  # noqa: S603 - fixed executable, no shell
        ["icacls", str(path), "/grant", "*S-1-5-5-0-424242:(RX)"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    listing = subprocess.run(  # noqa: S603 - fixed executable, no shell
        ["icacls", str(path)],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "LogonSessionId_0_424242" in listing  # fabrication took hold
    set_owner_only(path)  # must not raise
    listing = subprocess.run(  # noqa: S603 - fixed executable, no shell
        ["icacls", str(path)],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "LogonSessionId" not in listing
    grant_lines = [
        line.strip()
        for line in listing.splitlines()
        if ":" in line and "(" in line and line.strip()
    ]
    user = subprocess.run(
        ["whoami"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    # The sole surviving grant is the current user's full control.
    assert len(grant_lines) == 1, listing
    assert user.lower() in grant_lines[0].lower()
    assert "(F)" in grant_lines[0]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACL branch")
def test_windows_permission_error_carries_diagnostics(tmp_path: Path) -> None:
    """A failing icacls run must surface exit code and output, never ': '.

    Name-mapping failures write nothing to stderr (the original bug showed
    an error ending in a bare colon); the message must carry the exit code
    and whatever icacls did print.
    """
    from healthspan.fsperm import (
        _icacls,  # pyright: ignore[reportPrivateUsage]
    )

    path = tmp_path / "secret.txt"
    path.write_text("x", encoding="utf-8")
    with pytest.raises(PermissionSetError) as excinfo:
        # Unmappable pseudo-name: exits nonzero with stderr empty.
        _icacls(path, "/remove:g", "NT AUTHORITY\\LogonSessionId_0_1")
    message = str(excinfo.value)
    assert "exited" in message
    # The label names the operation that failed, not just the first token.
    assert "/remove:g" in message
    assert not message.rstrip().endswith(":")
