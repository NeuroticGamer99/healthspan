"""Thread-affine connection pool behavior (ADR-0037)."""

import threading
from pathlib import Path

import pytest
import sqlcipher3

from healthspan import db, migrate
from healthspan.kdf import DbKey
from healthspan.pool import ConnectionPool, PoolClosedError

KEY = DbKey(bytearray(range(1, 33)))


@pytest.fixture
def pool(tmp_path: Path) -> ConnectionPool:
    path = tmp_path / "healthspan.db"
    db.provision(path, KEY)
    migrate.migrate_database(path, KEY)
    return ConnectionPool(path, KEY)


def test_same_thread_reuses_one_connection(pool: ConnectionPool) -> None:
    assert pool.connection() is pool.connection()


def test_each_thread_gets_its_own_connection(pool: ConnectionPool) -> None:
    mine = pool.connection()
    theirs: list[sqlcipher3.Connection] = []

    def worker() -> None:
        conn = pool.connection()
        theirs.append(conn)
        # The worker owns its connection: it can query, and it is distinct.
        assert conn.execute("SELECT 1").fetchone() == (1,)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    assert len(theirs) == 1
    assert theirs[0] is not mine


def test_cross_thread_use_fails_loudly_at_the_driver(
    pool: ConnectionPool,
) -> None:
    # check_same_thread=True is the affinity enforcement (ADR-0037 rule 2).
    conns: list[sqlcipher3.Connection] = []
    thread = threading.Thread(target=lambda: conns.append(pool.connection()))
    thread.start()
    thread.join()
    with pytest.raises(sqlcipher3.ProgrammingError):
        conns[0].execute("SELECT 1")


def test_ping_reports_reachability(pool: ConnectionPool) -> None:
    assert pool.ping() is True


def test_ping_is_false_when_the_database_is_gone(tmp_path: Path) -> None:
    assert ConnectionPool(tmp_path / "missing.db", KEY).ping() is False


def test_statement_counting(pool: ConnectionPool) -> None:
    before = pool.statements_executed
    pool.connection().execute("SELECT 1").fetchone()
    assert pool.statements_executed > before


def test_close_all_refuses_new_connections(pool: ConnectionPool) -> None:
    pool.connection()
    pool.close_all()
    with pytest.raises(PoolClosedError):
        pool.connection()
    assert pool.ping() is False
