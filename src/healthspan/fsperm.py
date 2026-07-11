"""Owner-only file protection (ADR-0046 writer obligation, security.md).

POSIX: mode bits (0600 files, 0700 directories). Windows: mode bits carry
no ACL information, so the writer replaces the ACL outright — inheritance
removed, a single full-control grant to the current user (``icacls``, the
supported command-line surface for DACL edits).
"""

import os
import stat
import subprocess
from pathlib import Path


class PermissionSetError(Exception):
    """Owner-only protection could not be applied."""


def set_owner_only(path: Path) -> None:
    """Restrict ``path`` (file or directory) to its owner."""
    if os.name == "posix":
        mode = stat.S_IRWXU if path.is_dir() else stat.S_IRUSR | stat.S_IWUSR
        path.chmod(mode)
        return
    _set_owner_only_windows(path)


def _set_owner_only_windows(path: Path) -> None:
    user = _current_windows_user()
    # /inheritance:r drops inherited entries; /grant:r replaces any explicit
    # grant with full control for the owner alone.
    result = subprocess.run(  # noqa: S603 - fixed executable, no shell
        ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(F)"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise PermissionSetError(
            f"could not set owner-only ACL on {path}: {result.stderr.strip()}"
        )


def _current_windows_user() -> str:
    result = subprocess.run(
        ["whoami"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    user = result.stdout.strip()
    if result.returncode != 0 or not user:
        raise PermissionSetError("could not determine the current user (whoami)")
    return user
