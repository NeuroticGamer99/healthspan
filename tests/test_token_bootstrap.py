"""First-start minting of the default token set (ADR-0026, ADR-0050 §1,
ADR-0051): the emptiness check, the eight defaults, keyring delivery, the
one-time MCP secret, and the all-or-nothing failure behavior.
"""

from collections.abc import Callable

import pytest
import sqlcipher3

from healthspan import db, keychain, migrate, token_bootstrap, tokens
from healthspan.config import Config
from healthspan.kdf import DbKey

KEY_BYTES = bytes(range(1, 33))

# The ADR-0026 default-token table (with ADR-0040 `monitor` and ADR-0043
# `annotate`), asserted verbatim so a drift from the ADR fails loudly.
EXPECTED_DEFAULTS = {
    "cli-admin": (
        {"read", "write", "annotate", "import", "events", "jobs", "monitor", "admin"},
        ("external.*", "sync.*"),
    ),
    "cli-plugins": ({"read", "write", "import", "jobs"}, ()),
    "gui": ({"read", "write", "annotate", "import", "jobs", "monitor"}, ()),
    "mcp": ({"read"}, ()),
    "automation-host": (
        {"read", "events", "jobs"},
        ("alert.*", "sync.*", "external.*"),
    ),
    "watch-import": ({"jobs", "import"}, ()),
    "webhook": ({"events"}, ("external.*",)),
    "launcher": ({"supervise"}, ()),
}


def _key() -> DbKey:
    return DbKey(bytearray(KEY_BYTES))


@pytest.fixture
def conn(make_config: Callable[[], Config]) -> sqlcipher3.Connection:
    cfg = make_config()
    db.provision(cfg.database.path, _key())
    migrate.migrate_database(cfg.database.path, _key())
    return db.connect(cfg.database.path, _key())


def test_bootstrap_mints_the_adr_0026_default_set(conn: sqlcipher3.Connection) -> None:
    lines: list[str] = []
    assert token_bootstrap.bootstrap_default_tokens(conn, lines.append) is True
    records = {record.name: record for record in tokens.list_tokens(conn)}
    assert set(records) == set(EXPECTED_DEFAULTS)
    for name, (scopes, namespaces) in EXPECTED_DEFAULTS.items():
        assert records[name].scopes == frozenset(scopes), name
        assert records[name].publish_namespaces == namespaces, name
        assert records[name].revoked is False


def test_bootstrap_delivers_plaintexts_to_the_keyring(
    conn: sqlcipher3.Connection,
) -> None:
    token_bootstrap.bootstrap_default_tokens(conn, lambda _: None)
    for name in EXPECTED_DEFAULTS:
        plaintext = keychain.load_token_plaintext(name)
        assert plaintext is not None
        assert plaintext.startswith(f"hsp_{name}_")
        # The keyring copy is the real credential: it resolves to its row.
        record = tokens.look_up(conn, plaintext)
        assert record is not None
        assert record.name == name


def test_bootstrap_prints_the_mcp_secret_once_and_stores_its_hash(
    conn: sqlcipher3.Connection,
) -> None:
    lines: list[str] = []
    token_bootstrap.bootstrap_default_tokens(conn, lines.append)
    secrets = [
        line.strip() for line in lines if line.strip().startswith("hsp_mcpclient_")
    ]
    assert len(secrets) == 1
    assert keychain.load_mcp_client_hash() == tokens.hash_token(secrets[0])
    # The console output carries no Core-token plaintext — those go only to
    # the keyring; the MCP secret is the single deliberate exception.
    core_plaintexts = [
        keychain.load_token_plaintext(name) for name in EXPECTED_DEFAULTS
    ]
    dump = "\n".join(lines)
    for plaintext in core_plaintexts:
        assert plaintext is not None
        assert plaintext not in dump


def test_bootstrap_is_idempotent(conn: sqlcipher3.Connection) -> None:
    token_bootstrap.bootstrap_default_tokens(conn, lambda _: None)
    before = keychain.load_token_plaintext("cli-admin")
    assert token_bootstrap.bootstrap_default_tokens(conn, lambda _: None) is False
    assert tokens.count_tokens(conn) == len(EXPECTED_DEFAULTS)
    assert keychain.load_token_plaintext("cli-admin") == before


def test_bootstrap_skips_any_nonempty_table(conn: sqlcipher3.Connection) -> None:
    tokens.mint_token(conn, "pre-existing", {"read"})
    assert token_bootstrap.bootstrap_default_tokens(conn, lambda _: None) is False
    assert tokens.count_tokens(conn) == 1


def test_keyring_failure_leaves_the_table_empty_for_the_next_start(
    conn: sqlcipher3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}

    def failing_store(name: str, token: str) -> None:
        calls["n"] += 1
        if calls["n"] == 3:  # fail once, mid-set; the retry pass succeeds
            raise keychain.KeychainError("keyring backend unavailable")
        real_store(name, token)

    real_store = keychain.store_token_plaintext
    monkeypatch.setattr(keychain, "store_token_plaintext", failing_store)
    with pytest.raises(keychain.KeychainError):
        token_bootstrap.bootstrap_default_tokens(conn, lambda _: None)
    # Keyring writes come first (ADR-0051): the table stays empty, so the
    # next start re-mints and overwrites the orphaned entries.
    assert tokens.count_tokens(conn) == 0
    assert token_bootstrap.bootstrap_default_tokens(conn, lambda _: None) is True
    assert tokens.count_tokens(conn) == len(EXPECTED_DEFAULTS)
