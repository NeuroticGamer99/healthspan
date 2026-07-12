"""Single-instance advisory lock (ADR-0042, ADR-0049).

The Core Service holds an OS advisory lock on ``<database-path>.lock`` for
its whole lifetime; the kernel releases it when the holder dies — clean
exit, crash, ``SIGKILL``, or power loss — so the lock can never go stale
and there is no check-then-create race. A second Core Service, or an
offline ``db backup`` / ``db restore``, that cannot acquire it knows the
database is already held and refuses (ADR-0038/0042).

The sentinel also records the holder PID as human-facing diagnostics; the
kernel lock, not the recorded PID, is the correctness guarantee (the
``psutil`` reused-PID hygiene check is deferred to the supervision work,
ADR-0049).
"""

import contextlib
import os
import sys
from pathlib import Path
from types import TracebackType

LOCK_SUFFIX = ".lock"


class InstanceLockError(Exception):
    """The advisory lock could not be acquired, or a sentinel op failed."""


class InstanceLockHeldError(InstanceLockError):
    """Another live process holds the lock — the database is already in use."""


if sys.platform == "win32":
    import msvcrt

    # Lock a single byte at a high, content-free offset so the PID text at
    # offset 0 stays readable by other processes while the lock byte alone
    # is what a second acquirer contends on.
    _WIN_LOCK_OFFSET = 0x4000_0000

    def _try_lock(fd: int) -> bool:
        os.lseek(fd, _WIN_LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(fd: int) -> None:
        os.lseek(fd, _WIN_LOCK_OFFSET, os.SEEK_SET)
        with contextlib.suppress(OSError):
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _unlock(fd: int) -> None:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)


def lock_path(database_path: Path) -> Path:
    """The sentinel path for a database: ``<database-path>.lock``."""
    return database_path.with_name(database_path.name + LOCK_SUFFIX)


def read_holder_pid(database_path: Path) -> int | None:
    """Best-effort PID of the current lock holder, for human-facing messages.

    Diagnostics only — a stale or reused PID is possible; the advisory lock
    is the authority on whether a second instance may start.
    """
    return _pid_in(lock_path(database_path))


def _pid_in(lock_file: Path) -> int | None:
    try:
        text = lock_file.read_text(encoding="ascii")
    except OSError:
        return None
    try:
        return int(text.split("\n", 1)[0].strip())
    except ValueError:
        return None


class InstanceLock:
    """An advisory lock on a database's sentinel file (context manager)."""

    def __init__(self, database_path: Path) -> None:
        self._path = lock_path(database_path)
        self._fd: int | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        """Take the lock, or raise :class:`InstanceLockHeldError` if another has it.

        Non-blocking: fails immediately rather than waiting. The holder PID
        is written only after the lock is won, so a failed acquisition never
        clobbers the real holder's sentinel.
        """
        if self._fd is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if not _try_lock(fd):
                pid = _pid_in(self._path)
                held_by = f" (held by PID {pid})" if pid is not None else ""
                raise InstanceLockHeldError(
                    f"another process holds the database lock {self._path}{held_by}"
                )
            _write_pid(fd)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        _unlock(self._fd)
        os.close(self._fd)
        self._fd = None

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


def _write_pid(fd: int) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    data = f"{os.getpid()}\n".encode("ascii")
    os.write(fd, data)
    # Windows may refuse to truncate below a locked byte-range; the PID line
    # is still the first line, so a stale tail is harmless.
    with contextlib.suppress(OSError):
        os.ftruncate(fd, len(data))
