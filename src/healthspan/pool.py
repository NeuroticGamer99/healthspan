"""Thread-affine connection pool for the Core Service (ADR-0037).

One connection per worker thread, created lazily on first use and keyed once
from the key the service retains for its lifetime (ADR-0028/INV-1). All
connections come from the single factory in :mod:`healthspan.db`, so the
recorded pragma set and ``isolation_level=None`` discipline apply uniformly;
``check_same_thread=True`` stays on as the affinity enforcement — a
connection leaking across threads fails loudly at the driver.

Callers are repository-style synchronous functions running on the AnyIO
worker threadpool (capped at 8 in :mod:`healthspan.service`, so the pool
never holds more than 8 + 1 connections). The pool is never touched from
the event-loop thread.
"""

import threading
from contextlib import suppress
from pathlib import Path

import sqlcipher3

from healthspan import db
from healthspan.kdf import DbKey


class PoolClosedError(Exception):
    """The pool has shut down; no new connections may be created."""


class ConnectionPool:
    """Lazily-created, thread-local SQLCipher connections (ADR-0037 rule 2)."""

    def __init__(self, database_path: Path, key: DbKey) -> None:
        self._database_path = database_path
        self._key = key
        self._local = threading.local()
        self._lock = threading.Lock()
        self._all: list[sqlcipher3.Connection] = []
        self._closed = False
        self._statements = 0

    def connection(self) -> sqlcipher3.Connection:
        """This thread's connection, created and keyed on first use."""
        conn: sqlcipher3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        with self._lock:
            if self._closed:
                raise PoolClosedError("connection pool is closed")
        conn = db.connect(self._database_path, self._key)
        conn.set_trace_callback(self._count_statement)
        with self._lock:
            self._all.append(conn)
        self._local.conn = conn
        return conn

    def ping(self) -> bool:
        """Whether this thread can reach the database (``SELECT 1``)."""
        try:
            return self.connection().execute("SELECT 1").fetchone() == (1,)
        # PEP 758 (Python 3.14): an `except` with no `as` may omit the
        # parentheses around its exception tuple. This is valid — the
        # project floor is 3.14 — and `ruff format` actively strips the
        # parens, so this bare form is the formatter-stable one. (It reads
        # like the Python 2 `except A, e:` syntax but is unrelated.)
        except sqlcipher3.Error, db.DatabaseError, PoolClosedError:
            return False

    @property
    def statements_executed(self) -> int:
        """Statements run through pool connections (observability.md metrics)."""
        with self._lock:
            return self._statements

    def _count_statement(self, _statement: str) -> None:
        with self._lock:
            self._statements += 1

    def close_all(self) -> None:
        """Stop handing out connections and close what the driver permits.

        ``check_same_thread=True`` refuses cross-thread closes — that is the
        affinity enforcement working, not a defect — so connections created
        on worker threads are released by deallocation when the pool is
        dropped (and unconditionally at process exit). What matters here is
        that no *new* connection can be created once the key is about to be
        zeroized.
        """
        with self._lock:
            self._closed = True
            remaining, self._all = self._all, []
        for conn in remaining:
            with suppress(sqlcipher3.Error):
                db.close(conn)
        self._local = threading.local()
