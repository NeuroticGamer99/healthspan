"""Owner-only file protection (ADR-0046 writer obligation, security.md).

POSIX: mode bits (0600 files, 0700 directories). Windows: mode bits carry
no ACL information, so the writer replaces the ACL outright — inheritance
removed, a single full-control grant to the current user (``icacls``, the
supported command-line surface for DACL edits), then removal of any
explicit grant other principals already held (some environments stamp
SYSTEM/Administrators explicitly on new files).
"""

import functools
import os
import stat
import subprocess
from pathlib import Path

# Console tools (whoami, icacls) emit the OEM code page when piped, not the
# ANSI code page subprocess's text=True would assume; decoding with the
# wrong one mangles non-ASCII principal names (localized well-known SIDs,
# accented usernames) and breaks the grant-removal pass.
_CONSOLE_ENCODING = "oem"


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
    # /inheritance:r drops inherited entries; /grant:r replaces the user's
    # explicit grant with full control.
    _icacls(path, "/inheritance:r", "/grant:r", f"{user}:(F)")
    # /inheritance:r leaves *explicit* entries other principals may already
    # hold; remove every grant that is not the current user's.
    for principal in _explicit_principals(path):
        if principal.lower() != user.lower():
            _icacls(path, "/remove:g", principal)


def _icacls(path: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603 - fixed executable, no shell
        ["icacls", str(path), *args],  # noqa: S607
        capture_output=True,
        encoding=_CONSOLE_ENCODING,
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise PermissionSetError(
            f"could not set owner-only ACL on {path}: {result.stderr.strip()}"
        )
    return result.stdout


def _explicit_principals(path: Path) -> list[str]:
    """Principals holding ACL entries on ``path``, per ``icacls`` listing."""
    listing = _icacls(path)
    principals: list[str] = []
    prefix = str(path)
    for raw in listing.splitlines():
        line = raw.strip()
        if line.startswith(prefix):
            line = line[len(prefix) :].strip()
        if ":(" not in line:
            continue
        principals.append(line.split(":(", 1)[0].strip())
    return principals


@functools.cache
def _current_windows_user() -> str:
    # Process-invariant; cached so multi-file operations (init, backup)
    # spawn whoami once, not once per file.
    result = subprocess.run(
        ["whoami"],  # noqa: S607
        capture_output=True,
        encoding=_CONSOLE_ENCODING,
        errors="replace",
        check=False,
    )
    user = result.stdout.strip()
    if result.returncode != 0 or not user:
        raise PermissionSetError("could not determine the current user (whoami)")
    return user
