"""Token store: format, minting, hashed storage, verification (ADR-0026)."""

import hashlib
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlcipher3

from healthspan import db, migrate, tokens
from healthspan.kdf import DbKey

KEY = DbKey(bytearray(range(1, 33)))


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlcipher3.Connection]:
    path = tmp_path / "healthspan.db"
    db.provision(path, KEY)
    migrate.migrate_database(path, KEY)
    connection = db.connect(path, KEY)
    try:
        yield connection
    finally:
        db.close(connection)


# --------------------------------------------------------------------------
# Format and advisory-name parsing
# --------------------------------------------------------------------------


def test_minted_token_has_the_adr_0026_shape(conn: sqlcipher3.Connection) -> None:
    token = tokens.mint_token(conn, "gui", {"read", "write"})
    assert token.startswith("hsp_gui_")
    secret = token[len("hsp_gui_") :]
    assert len(secret) >= 43  # 32 random bytes, base64url, unpadded
    assert tokens.parse_name(token) == "gui"


def test_parse_name_survives_underscores_in_the_secret() -> None:
    # base64url secrets contain '_' and '-'; the name ends at the first '_'.
    assert tokens.parse_name("hsp_cli-admin_Ab_c-9_x") == "cli-admin"
    assert tokens.parse_name("hsp_job:1234_secret") == "job:1234"


def test_parse_name_rejects_unparseable_shapes() -> None:
    assert tokens.parse_name("Bearer nonsense") is None
    assert tokens.parse_name("hsp_") is None
    assert tokens.parse_name("hsp_noseparator") is None
    assert tokens.parse_name("hsp_name_") is None  # empty secret
    assert tokens.parse_name("hsp_UPPER_secret") is None
    assert tokens.parse_name("mcp_gui_secret") is None


# --------------------------------------------------------------------------
# Minting rules
# --------------------------------------------------------------------------


def test_mint_rejects_names_the_format_cannot_carry(
    conn: sqlcipher3.Connection,
) -> None:
    with pytest.raises(tokens.TokenError, match="ambiguous"):
        tokens.mint_token(conn, "cli_admin", {"read"})
    with pytest.raises(tokens.TokenError):
        tokens.mint_token(conn, "Gui", {"read"})
    with pytest.raises(tokens.TokenError):
        tokens.mint_token(conn, "", {"read"})


def test_mint_rejects_unknown_or_empty_scopes(conn: sqlcipher3.Connection) -> None:
    with pytest.raises(tokens.TokenError, match="unknown scope"):
        tokens.mint_token(conn, "gui", {"read", "reed"})
    with pytest.raises(tokens.TokenError, match="at least one scope"):
        tokens.mint_token(conn, "gui", set())


def test_mint_rejects_duplicate_names(conn: sqlcipher3.Connection) -> None:
    tokens.mint_token(conn, "gui", {"read"})
    with pytest.raises(tokens.TokenError, match="gui"):
        tokens.mint_token(conn, "gui", {"read"})


def test_publish_namespaces_require_the_events_scope(
    conn: sqlcipher3.Connection,
) -> None:
    with pytest.raises(tokens.TokenError, match="events"):
        tokens.mint_token(conn, "webhook", {"read"}, publish_namespaces=("external.*",))
    token = tokens.mint_token(
        conn, "webhook", {"events"}, publish_namespaces=("external.*",)
    )
    record = tokens.look_up(conn, token)
    assert record is not None
    assert record.publish_namespaces == ("external.*",)


def test_job_binding_round_trips(conn: sqlcipher3.Connection) -> None:
    conn.execute(
        "INSERT INTO jobs (id, job_type, status, submitted_utc) "
        "VALUES (3, 'import', 'running', '2026-07-12T00:00:00Z')"
    )
    token = tokens.mint_token(conn, "job:abc", {"jobs", "import"}, job_id=3)
    record = tokens.look_up(conn, token)
    assert record is not None
    assert record.job_id == 3


# --------------------------------------------------------------------------
# Hashed-only storage (ADR-0026: a stolen tokens table reveals nothing usable)
# --------------------------------------------------------------------------


def test_only_the_hash_is_stored(conn: sqlcipher3.Connection) -> None:
    token = tokens.mint_token(conn, "gui", {"read"}, authorship="self")
    rows = conn.execute("SELECT * FROM tokens").fetchall()
    assert len(rows) == 1
    assert token not in [str(value) for value in rows[0]]
    hash_row = conn.execute("SELECT token_hash FROM tokens").fetchone()
    assert hash_row is not None
    assert hash_row[0] == hashlib.sha256(token.encode()).hexdigest()


# --------------------------------------------------------------------------
# Verification and lifecycle
# --------------------------------------------------------------------------


def test_look_up_round_trip(conn: sqlcipher3.Connection) -> None:
    token = tokens.mint_token(
        conn, "mcp-analyst", {"read", "annotate"}, authorship="ai"
    )
    record = tokens.look_up(conn, token)
    assert record is not None
    assert record.name == "mcp-analyst"
    assert record.scopes == frozenset({"read", "annotate"})
    assert record.authorship == "ai"
    assert record.revoked is False
    assert record.last_used_utc is None


def test_look_up_rejects_wrong_and_unknown_credentials(
    conn: sqlcipher3.Connection,
) -> None:
    token = tokens.mint_token(conn, "gui", {"read"})
    assert tokens.look_up(conn, token + "x") is None
    assert tokens.look_up(conn, "hsp_gui_completelywrong") is None
    assert tokens.look_up(conn, "") is None


def test_revoke_is_immediate_and_visible(conn: sqlcipher3.Connection) -> None:
    token = tokens.mint_token(conn, "gui", {"read"})
    assert tokens.revoke_token(conn, "gui") is True
    record = tokens.look_up(conn, token)
    assert record is not None
    assert record.revoked is True
    revoked_row = conn.execute("SELECT revoked_utc FROM tokens").fetchone()
    assert revoked_row is not None
    assert revoked_row[0] is not None
    assert tokens.revoke_token(conn, "gui") is False  # already revoked
    assert tokens.revoke_token(conn, "ghost") is False


def test_record_ok_audits_and_touches_last_used(
    conn: sqlcipher3.Connection,
) -> None:
    token = tokens.mint_token(conn, "gui", {"read"})
    record = tokens.look_up(conn, token)
    assert record is not None
    tokens.record_ok(
        conn, record, source_addr="127.0.0.1", endpoint="/v1/metrics", method="GET"
    )
    audit = conn.execute(
        "SELECT token_name, source_addr, endpoint, method, outcome FROM auth_audit"
    ).fetchall()
    assert audit == [("gui", "127.0.0.1", "/v1/metrics", "GET", "ok")]
    refreshed = tokens.look_up(conn, token)
    assert refreshed is not None
    assert refreshed.last_used_utc is not None
