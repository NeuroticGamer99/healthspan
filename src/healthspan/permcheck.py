"""Startup permission verification (ADR-0066, security.md).

Owner-only protection is set on the files the platform *creates*
(:mod:`healthspan.fsperm`), which says nothing about a file that arrived
some other way: a database restored from a tar or a cloud-sync copy, a
sidecar hand-copied to a new machine, a passphrase file typed into a home
directory. This module re-reads those permissions at the two points where
the platform takes hold of the database — Core Service start and the
sanctioned direct-database commands — and applies ADR-0066's graded posture:

* Files the platform owns — the database, its ``.keyparams`` sidecar, the
  backup directory and its published backups, the config file — are
  **repaired in place** and the drift reported. A warning that scrolls past
  leaves a 644 database at 644 forever; repairing ends the exposure and
  still says so, and the second start is silent because there is nothing
  left to fix.
* The ``passphrase_file`` is **refused, never repaired**. It may be an
  OS-secret-facility file the platform must not touch, and a plaintext
  secret that has been world-readable is already disclosed — tightening it
  would hide that fact behind a fixed mode bit. Refusing names the exposure
  and forces a rotation decision.

There is no override flag or config key: the refusal message names the
exact remedy, and the check would be worth little if the first response to
it were to turn it off (ADR-0066 §4, following ADR-0049 §4's
"not a knob shipped speculatively").
"""

from collections.abc import Callable
from pathlib import Path

from healthspan.fsperm import (
    PermissionSetError,
    owner_only_exposure,
    restrict_command,
    set_owner_only,
)
from healthspan.keyparams import sidecar_path


class BroadPermissionsError(Exception):
    """A file holding a secret is readable beyond its owner."""


def repair_owner_only(
    path: Path,
    description: str,
    warn: Callable[[str], None],
    *,
    acl_scan: bool = True,
) -> None:
    """Restore owner-only protection on a platform-owned file, reporting it.

    Silent when ``path`` is already owner-only or does not exist. A repair
    that cannot be applied (a foreign-owned file, a filesystem without
    permissions, a read-only mount) degrades to a warning naming the remedy
    rather than blocking — ``path`` here is always ciphertext or
    configuration, never a plaintext secret.
    """
    if not path.exists():
        return
    exposure = owner_only_exposure(path, acl_scan=acl_scan)
    if exposure is None:
        return
    try:
        set_owner_only(path)
    except (PermissionSetError, OSError) as exc:
        warn(
            f"{description} {path} is accessible beyond its owner ({exposure}) "
            f"and could not be restricted automatically: {exc}. {_remedy(path)}"
        )
        return
    warn(
        f"{description} {path} was accessible beyond its owner ({exposure}); "
        "permissions have been restricted to owner-only. This does not undo "
        "any read that already happened while it was exposed."
    )


def refuse_if_exposed(path: Path, description: str) -> None:
    """Raise rather than read a plaintext secret exposed beyond its owner."""
    if not path.exists():
        return
    exposure = owner_only_exposure(path)
    if exposure is None:
        return
    raise BroadPermissionsError(
        f"{description} {path} is accessible beyond its owner ({exposure}); "
        "refusing to read it. Treat the secret it holds as disclosed and "
        f"rotate it, then restrict the file. {_remedy(path)}"
    )


def verify_startup_files(
    *,
    config_path: Path,
    database_path: Path,
    backup_dir: Path,
    warn: Callable[[str], None],
) -> None:
    """Check everything the platform owns before it opens the database.

    The config file is re-checked here even though :func:`~healthspan.config
    .load_config` already checked it: that per-command check skips the
    Windows ACL scan to keep a subprocess off every CLI invocation, so this
    is where the config file gets its full-depth look (ADR-0066 §3).
    """
    repair_owner_only(config_path, "config file", warn)
    repair_owner_only(database_path, "database file", warn)
    repair_owner_only(sidecar_path(database_path), "database sidecar", warn)
    repair_owner_only(backup_dir, "backup directory", warn)


def verify_backup_pair(database: Path, warn: Callable[[str], None]) -> None:
    """Check one published backup and its sidecar (ADR-0038 artifacts).

    A backup is a full copy of the database, so it carries the same exposure;
    the pair is checked where it is *touched* — the source of a restore, and
    a sweep after a backup publishes — rather than on every start, which
    would cost one ACL scan per retained backup on Windows.
    """
    repair_owner_only(database, "backup database", warn)
    repair_owner_only(sidecar_path(database), "backup sidecar", warn)


def _remedy(path: Path) -> str:
    command = restrict_command(path)
    if command is None:
        # The account lookup failed, which is also why the check itself may
        # have. Describe the end state rather than print a command that
        # cannot be composed correctly.
        return (
            "Restrict it to its owner alone: remove inherited entries and "
            "leave a single full-control grant for your own account."
        )
    return f"Restrict it with: {command}"
