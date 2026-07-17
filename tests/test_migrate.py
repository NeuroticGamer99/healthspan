"""Migration runner mechanics (ADR-0009/0035) and the ``db migrate`` CLI.

Covers the runner's contract independently of migration 0001's contents:
discovery/ordering, the statement splitter (trigger-body aware), forward
application, runner-level idempotency, mid-file atomicity, the pre-commit
foreign_key_check, and failure recovery.
"""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from healthspan import db, migrate
from healthspan.cli import app
from healthspan.kdf import DbKey

KEY = DbKey(bytearray(range(1, 33)))  # non-zero (all-zero reads as zeroized)

runner = CliRunner()
PASSPHRASE = "a perfectly reasonable passphrase"


def _provisioned(tmp_path: Path) -> Path:
    path = tmp_path / "healthspan.db"
    db.provision(path, KEY)
    return path


# --- discovery / ordering / validation -----------------------------------


def test_packaged_migrations_present_and_named() -> None:
    migrations = migrate.discover_migrations()
    assert [m.version for m in migrations] == [1, 2, 3, 4, 5]
    assert migrations[0].filename == "0001_initial_schema.sql"
    assert "CREATE TABLE audit_log" in migrations[0].sql
    assert migrations[1].filename == "0002_tokens_and_auth_audit.sql"
    assert "CREATE TABLE tokens" in migrations[1].sql
    assert migrations[2].filename == "0003_import_conflict_keys.sql"
    assert "ux_lab_results_natural_key" in migrations[2].sql
    assert migrations[3].filename == "0004_categories_and_aliases.sql"
    assert "CREATE TABLE categories" in migrations[3].sql
    assert migrations[4].filename == "0005_molar_mass_and_frameworks.sql"
    assert "ADD COLUMN molar_mass" in migrations[4].sql


def test_discover_orders_by_version(tmp_path: Path) -> None:
    (tmp_path / "0002_second.sql").write_text("SELECT 2;", encoding="utf-8")
    (tmp_path / "0001_first.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("ignored", encoding="utf-8")  # non-.sql skipped
    migrations = migrate.discover_migrations(tmp_path)
    assert [m.version for m in migrations] == [1, 2]
    assert migrations[0].filename == "0001_first.sql"


def test_target_version_is_the_highest_shipped() -> None:
    assert migrate.target_version() == 5


def test_target_version_from_an_explicit_corpus(tmp_path: Path) -> None:
    (tmp_path / "0001_a.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "0003_c.sql").write_text("SELECT 3;", encoding="utf-8")
    migrations = migrate.discover_migrations(tmp_path)
    assert migrate.target_version(migrations) == 3


def test_target_version_of_empty_corpus_is_none(tmp_path: Path) -> None:
    assert migrate.target_version(migrate.discover_migrations(tmp_path)) is None


def test_discover_rejects_malformed_filename(tmp_path: Path) -> None:
    (tmp_path / "oops.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(migrate.MigrationError, match="naming convention"):
        migrate.discover_migrations(tmp_path)


def test_discover_rejects_duplicate_version(tmp_path: Path) -> None:
    (tmp_path / "0001_a.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "0001_b.sql").write_text("SELECT 2;", encoding="utf-8")
    with pytest.raises(migrate.MigrationError, match="duplicate"):
        migrate.discover_migrations(tmp_path)


# --- statement splitter ---------------------------------------------------


def test_split_keeps_trigger_body_whole() -> None:
    sql = (
        "CREATE TABLE x (a INTEGER) STRICT;\n"
        "CREATE TRIGGER trg AFTER INSERT ON x BEGIN\n"
        "  INSERT INTO y (a) VALUES (new.a);\n"
        "  INSERT INTO z (b) VALUES (new.a);\n"
        "END;\n"
        "CREATE TABLE last (b INTEGER) STRICT;\n"
    )
    statements = migrate.split_statements(sql)
    assert len(statements) == 3
    assert statements[1].startswith("CREATE TRIGGER")
    # The two inner semicolons did not split the trigger: the whole body,
    # up to and including END;, is one statement.
    assert statements[1].rstrip().endswith("END;")
    assert statements[1].count("VALUES") == 2


def test_split_rejects_unterminated_statement() -> None:
    with pytest.raises(migrate.MigrationError, match="unterminated"):
        migrate.split_statements("CREATE TABLE x (a INTEGER) STRICT")


def test_split_allows_trailing_comment() -> None:
    statements = migrate.split_statements(
        "CREATE TABLE x (a INTEGER) STRICT;\n-- a closing note\n"
    )
    assert len(statements) == 1


# --- forward application, ledger, idempotency -----------------------------


def test_fresh_database_applies_all_migrations(tmp_path: Path) -> None:
    path = _provisioned(tmp_path)
    run = migrate.migrate_database(path, KEY)
    assert run.applied == (1, 2, 3, 4, 5)
    assert run.final_version == 5
    conn = db.connect(path, KEY)
    try:
        rows = conn.execute("SELECT version, filename FROM schema_version").fetchall()
        assert rows == [
            (1, "0001_initial_schema.sql"),
            (2, "0002_tokens_and_auth_audit.sql"),
            (3, "0003_import_conflict_keys.sql"),
            (4, "0004_categories_and_aliases.sql"),
            (5, "0005_molar_mass_and_frameworks.sql"),
        ]
        assert db.schema_version(conn) == 5
        # Runtime connections enforce foreign keys (ADR-0035 pragma table).
        assert conn.execute("PRAGMA foreign_keys").fetchone() == (1,)
    finally:
        db.close(conn)


def test_runner_applies_0003_incrementally_over_an_0002_database(
    tmp_path: Path,
) -> None:
    # A database at version 2, later migrated by a build shipping 0003: only
    # 0003 runs, and its natural-key indexes appear (ADR-0035 incremental apply,
    # ADR-0052 migration 0003). Scoped to versions <=3 so this stays a focused
    # 0003 incremental test even as later migrations (0004+) ship.
    path = _provisioned(tmp_path)
    all_migrations = migrate.discover_migrations()
    through_0002 = [m for m in all_migrations if m.version <= 2]
    through_0003 = [m for m in all_migrations if m.version <= 3]
    migrate.migrate_database(path, KEY, through_0002)
    run = migrate.migrate_database(path, KEY, through_0003)
    assert run.applied == (3,)
    assert run.final_version == 3
    conn = db.connect(path, KEY)
    try:
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' "
                "AND name LIKE 'ux_%_natural_key'"
            ).fetchall()
        }
        assert indexes == {"ux_lab_draws_natural_key", "ux_lab_results_natural_key"}
    finally:
        db.close(conn)


def test_runner_is_idempotent(tmp_path: Path) -> None:
    path = _provisioned(tmp_path)
    migrate.migrate_database(path, KEY)
    second = migrate.migrate_database(path, KEY)
    assert second.applied == ()
    assert second.final_version == 5
    conn = db.connect(path, KEY)
    try:
        # No per-file idempotent SQL; the ledger is the only skip mechanism.
        assert conn.execute("SELECT count(*) FROM schema_version").fetchone() == (5,)
    finally:
        db.close(conn)


# --- atomicity, foreign-key check, recovery -------------------------------


def test_mid_file_failure_rolls_back_the_whole_file(tmp_path: Path) -> None:
    """The test that catches the driver's implicit-commit-around-DDL: a
    valid CREATE followed by a failing statement must leave *nothing* and
    no ledger row (ADR-0035)."""
    path = _provisioned(tmp_path)
    conn = db.connect_for_migration(path, KEY)
    broken = migrate.Migration(
        version=1,
        filename="0001_broken.sql",
        sql=(
            "CREATE TABLE first_ok (a INTEGER) STRICT;\n"
            "INSERT INTO does_not_exist VALUES (1);\n"
        ),
    )
    try:
        with pytest.raises(migrate.MigrationError):
            migrate.apply_migrations(conn, [broken])
        assert conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE name = 'first_ok'"
        ).fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM schema_version").fetchone() == (0,)
    finally:
        db.close(conn)


def test_foreign_key_violation_is_rejected(tmp_path: Path) -> None:
    """foreign_keys is OFF during the migration (the violating INSERT
    succeeds), so the pre-commit foreign_key_check is what rejects it."""
    path = _provisioned(tmp_path)
    conn = db.connect_for_migration(path, KEY)
    bad_fk = migrate.Migration(
        version=1,
        filename="0001_fk.sql",
        sql=(
            "CREATE TABLE parent (id INTEGER PRIMARY KEY) STRICT;\n"
            "CREATE TABLE child (pid INTEGER REFERENCES parent(id)) STRICT;\n"
            "INSERT INTO child (pid) VALUES (999);\n"
        ),
    )
    try:
        with pytest.raises(migrate.MigrationError, match="foreign_key_check"):
            migrate.apply_migrations(conn, [bad_fk])
        assert conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE name IN ('parent', 'child')"
        ).fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM schema_version").fetchone() == (0,)
    finally:
        db.close(conn)


def test_broken_migration_leaves_predecessors_and_recovers(tmp_path: Path) -> None:
    path = _provisioned(tmp_path)
    conn = db.connect_for_migration(path, KEY)
    v1 = migrate.Migration(1, "0001_a.sql", "CREATE TABLE t1 (a INTEGER) STRICT;")
    v2_broken = migrate.Migration(
        2,
        "0002_b.sql",
        "CREATE TABLE t2 (a INTEGER) STRICT;\nINSERT INTO nope VALUES (1);",
    )
    v2_good = migrate.Migration(2, "0002_b.sql", "CREATE TABLE t2 (a INTEGER) STRICT;")
    try:
        with pytest.raises(migrate.MigrationError):
            migrate.apply_migrations(conn, [v1, v2_broken])
        # v1 committed and stands; v2 rolled back entirely.
        ledger = conn.execute("SELECT version FROM schema_version").fetchall()
        assert {r[0] for r in ledger} == {1}
        assert conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE name = 't1'"
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE name = 't2'"
        ).fetchone() == (0,)
        # Recovery: a fixed v2 applies; v1 is skipped by the ledger.
        run = migrate.apply_migrations(conn, [v1, v2_good])
        assert run.applied == (2,)
        assert run.final_version == 2
        assert conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE name = 't2'"
        ).fetchone() == (1,)
    finally:
        db.close(conn)


def test_apply_migrations_refuses_a_newer_schema_than_shipped(tmp_path: Path) -> None:
    """A database migrated by a newer build must be refused, not silently
    reported as up to date at a version this build cannot understand."""
    path = _provisioned(tmp_path)
    migrate.migrate_database(path, KEY)  # brings it to the real latest version
    conn = db.connect_for_migration(path, KEY)
    try:
        conn.execute(
            "INSERT INTO schema_version (version, filename, applied_at) "
            "VALUES (99, '0099_from_the_future.sql', '2099-01-01T00:00:00Z')"
        )
        with pytest.raises(migrate.MigrationError, match="newer than this build"):
            migrate.apply_migrations(conn, migrate.discover_migrations())
    finally:
        db.close(conn)


def test_migrate_missing_database_fails(tmp_path: Path) -> None:
    with pytest.raises(db.DatabaseError, match="does not exist"):
        migrate.migrate_database(tmp_path / "absent.db", KEY)


def test_migrate_wrong_key_rejected(tmp_path: Path) -> None:
    path = _provisioned(tmp_path)
    wrong = DbKey(bytearray([0x42] * 32))
    with pytest.raises(db.DatabaseError, match="wrong passphrase"):
        migrate.migrate_database(path, wrong)


def test_connect_for_migration_skips_the_runtime_pragma_set(tmp_path: Path) -> None:
    """The runner controls foreign_keys itself, so the migration connection
    leaves it OFF (the driver default) — in deliberate contrast to db.connect,
    which applies the runtime pragma set and turns foreign_keys ON."""
    path = _provisioned(tmp_path)
    migration_conn = db.connect_for_migration(path, KEY)
    try:
        assert migration_conn.execute("PRAGMA foreign_keys").fetchone() == (0,)
    finally:
        db.close(migration_conn)
    runtime_conn = db.connect(path, KEY)
    try:
        assert runtime_conn.execute("PRAGMA foreign_keys").fetchone() == (1,)
    finally:
        db.close(runtime_conn)


# --- CLI ------------------------------------------------------------------


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        'config_version = 1\n\n[database]\npath = "healthspan.db"\n\n'
        '[backup]\ndirectory = "backups"\n',
        encoding="utf-8",
    )
    return path


def _invoke(config_file: Path, args: list[str], input_text: str):
    return runner.invoke(app, ["--config", str(config_file), *args], input=input_text)


def _init(config_file: Path):
    return _invoke(config_file, ["init"], f"{PASSPHRASE}\n{PASSPHRASE}\n")


def test_cli_migrate_applies_then_reports_up_to_date(config_file: Path) -> None:
    assert _init(config_file).exit_code == 0
    first = _invoke(config_file, ["db", "migrate"], f"{PASSPHRASE}\n")
    assert first.exit_code == 0, first.output
    assert "Applied 5 migration(s) [1, 2, 3, 4, 5]" in first.output
    assert "version 5" in first.output
    second = _invoke(config_file, ["db", "migrate"], f"{PASSPHRASE}\n")
    assert second.exit_code == 0, second.output
    assert "already up to date" in second.output


def test_cli_migrate_before_init_fails_cleanly(config_file: Path) -> None:
    result = _invoke(config_file, ["db", "migrate"], f"{PASSPHRASE}\n")
    assert result.exit_code == 1
    # The specific failure: no key-parameter sidecar to unlock the database.
    assert "sidecar" in result.output
    assert "Traceback" not in result.output


def test_cli_migrate_wrong_passphrase_fails_cleanly(config_file: Path) -> None:
    assert _init(config_file).exit_code == 0
    result = _invoke(config_file, ["db", "migrate"], "not the right passphrase\n")
    assert result.exit_code == 1
    assert "wrong passphrase" in result.output  # the specific unlock failure
    assert "Traceback" not in result.output
