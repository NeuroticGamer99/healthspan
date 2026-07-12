"""First-start minting of the default token set (ADR-0026, ADR-0050 §1).

When the Core Service starts and finds the ``tokens`` table empty, it mints
the eight default tokens — hashes into the table, plaintexts into the
holders' keyring entries — and the MCP client-facing secret (hash into the
MCP keyring entry, plaintext shown exactly once on the operator's console).
A non-empty table is never re-minted, so the check is idempotent and covers
fresh installs and pre-0002 databases alike.

Ordering (ADR-0051): keyring writes come *first*, then one all-or-nothing
database transaction. A failure between the two leaves orphaned keyring
entries and an empty table — the next start overwrites them and tries
again. The reverse order could commit hashes whose plaintexts were lost,
a locked-out state the emptiness check would never repair.

The MCP plaintext goes to the console channel the caller provides — never
through the logging pipeline, whose output may be redirected or shipped.
"""

from collections.abc import Callable

import sqlcipher3

from healthspan import keychain, tokens

# The ADR-0026 default token set (extended by ADR-0040 `monitor` and
# ADR-0043 `annotate`). Publish-namespace allowlists per ADR-0026: the
# webhook may publish only `external.*`; the Automation Host adds `alert.*`
# and `sync.*`; cli-admin gets the non-reserved namespaces for scripting
# minus `alert.*`, which ADR-0026 confines to the Automation Host and Core
# internals (ADR-0051).
DEFAULT_TOKEN_SPECS: tuple[tokens.TokenSpec, ...] = (
    tokens.TokenSpec(
        "cli-admin",
        frozenset(
            {
                "read",
                "write",
                "annotate",
                "import",
                "events",
                "jobs",
                "monitor",
                "admin",
            }
        ),
        publish_namespaces=("external.*", "sync.*"),
    ),
    tokens.TokenSpec("cli-plugins", frozenset({"read", "write", "import", "jobs"})),
    tokens.TokenSpec(
        "gui",
        frozenset({"read", "write", "annotate", "import", "jobs", "monitor"}),
    ),
    tokens.TokenSpec("mcp", frozenset({"read"})),
    tokens.TokenSpec(
        "automation-host",
        frozenset({"read", "events", "jobs"}),
        publish_namespaces=("alert.*", "sync.*", "external.*"),
    ),
    tokens.TokenSpec("watch-import", frozenset({"jobs", "import"})),
    tokens.TokenSpec(
        "webhook", frozenset({"events"}), publish_namespaces=("external.*",)
    ),
    tokens.TokenSpec("launcher", frozenset({"supervise"})),
)

MCP_CLIENT_NAME_SEGMENT = "mcpclient"


def generate_mcp_client_secret() -> str:
    """``hsp_mcpclient_<secret>`` — Core-token shape, never a Core token."""
    return tokens.format_token(MCP_CLIENT_NAME_SEGMENT, tokens.generate_secret())


def bootstrap_default_tokens(
    conn: sqlcipher3.Connection, console: Callable[[str], None]
) -> bool:
    """Mint the default set if the ``tokens`` table is empty (ADR-0050 §1).

    Returns whether minting happened. ``console`` receives the one-time MCP
    client-secret printout and the confirmation lines — it must reach the
    operator's terminal, not the log stream. Raises
    :class:`~healthspan.keychain.KeychainError` or
    :class:`~healthspan.tokens.TokenError` on failure; the caller aborts
    startup, and the table stays empty so the next start re-mints.
    """
    if tokens.count_tokens(conn) > 0:
        return False

    minted = [
        (spec, tokens.format_token(spec.name, tokens.generate_secret()))
        for spec in DEFAULT_TOKEN_SPECS
    ]
    mcp_client_secret = generate_mcp_client_secret()

    # Keyring first (ADR-0051 ordering — see the module docstring).
    for spec, token in minted:
        keychain.store_token_plaintext(spec.name, token)
    keychain.store_mcp_client_hash(tokens.hash_token(mcp_client_secret))
    tokens.store_tokens(conn, minted)

    names = ", ".join(spec.name for spec, _ in minted)
    console(
        f"Minted the default token set ({names}); each plaintext is stored "
        "in its holder's OS keyring entry (service 'healthspan', "
        "'token:<name>')."
    )
    console(
        "MCP client-facing secret (shown once — paste it into your AI "
        "client's configuration as a static 'Authorization: Bearer' header):"
    )
    console(f"  {mcp_client_secret}")
    return True
