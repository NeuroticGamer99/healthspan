"""Single-instance advisory lock (ADR-0042, ADR-0049).

Covers acquire/release, non-blocking contention, and the load-bearing
ADR-0042 property: the kernel releases the lock when the holder dies, so a
crash or kill leaves no stale artifact — asserted across a real process
boundary on whichever OS runs (msvcrt on Windows, fcntl on POSIX).
"""

import subprocess
import sys
import time
from pathlib import Path

import pytest

from healthspan.locking import (
    InstanceLock,
    InstanceLockHeldError,
    lock_path,
    read_holder_pid,
)


def test_lock_path_naming(tmp_path: Path) -> None:
    db = tmp_path / "healthspan.db"
    assert lock_path(db) == tmp_path / "healthspan.db.lock"


def test_acquire_release_cycle(tmp_path: Path) -> None:
    lock = InstanceLock(tmp_path / "hs.db")
    assert not lock.held
    lock.acquire()
    assert lock.held
    lock.release()
    assert not lock.held
    lock.acquire()  # re-acquire after release
    assert lock.held
    lock.release()


def test_second_acquire_in_process_is_idempotent(tmp_path: Path) -> None:
    lock = InstanceLock(tmp_path / "hs.db")
    lock.acquire()
    lock.acquire()  # same object: no-op, still held
    assert lock.held
    lock.release()


def test_contended_acquire_refuses(tmp_path: Path) -> None:
    db = tmp_path / "hs.db"
    holder = InstanceLock(db)
    holder.acquire()
    try:
        with pytest.raises(InstanceLockHeldError):
            InstanceLock(db).acquire()
    finally:
        holder.release()


def test_holder_pid_recorded(tmp_path: Path) -> None:
    db = tmp_path / "hs.db"
    lock = InstanceLock(db)
    lock.acquire()
    try:
        assert read_holder_pid(db) is not None
    finally:
        lock.release()


def test_context_manager_releases(tmp_path: Path) -> None:
    db = tmp_path / "hs.db"
    with InstanceLock(db) as lock:
        assert lock.held
    assert InstanceLock(db)  # a fresh lock object can now acquire
    after = InstanceLock(db)
    after.acquire()
    assert after.held
    after.release()


def test_kernel_releases_lock_on_process_kill(tmp_path: Path) -> None:
    """A killed holder leaves no stale lock — the next start succeeds.

    ADR-0042: correctness rests on the kernel releasing the advisory lock on
    process death, with no stale-file cleanup. Exercised across a real
    process boundary (msvcrt on Windows, fcntl on POSIX).
    """
    db = tmp_path / "hs.db"
    child_source = (
        "import time, sys\n"
        "from pathlib import Path\n"
        "from healthspan.locking import InstanceLock\n"
        f"lock = InstanceLock(Path(r{str(db)!r}))\n"
        "lock.acquire()\n"
        "print('LOCKED', flush=True)\n"
        "time.sleep(60)\n"
    )
    proc = subprocess.Popen(  # noqa: S603 - trusted first-party child snippet
        [sys.executable, "-c", child_source],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "LOCKED", "child failed to lock"
        with pytest.raises(InstanceLockHeldError):
            InstanceLock(db).acquire()
    finally:
        proc.kill()
        proc.wait(timeout=10)

    # No stale artifact blocks the next legitimate start. On POSIX the fcntl
    # lock is gone the instant the holder dies; on Windows the byte-range lock
    # is released during process teardown, which can lag a few ms behind
    # proc.wait() returning — so poll briefly rather than racing it.
    reclaimed = InstanceLock(db)
    for _ in range(50):
        try:
            reclaimed.acquire()
            break
        except InstanceLockHeldError:
            time.sleep(0.1)
    assert reclaimed.held, "lock was not released after the holder was killed"
    reclaimed.release()
