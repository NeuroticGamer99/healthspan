"""Owner-only protection: the ADR-0046 write-side obligation, both platforms."""

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from healthspan.fsperm import set_owner_only


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
