"""Named scoped bearer tokens: the server-side store (ADR-0026).

The database half of the credential model: minting writes ``SHA-256(token)``
into the ``tokens`` table (the plaintext is returned once to the caller and
never stored here), verification hashes the presented value and compares
with :func:`secrets.compare_digest`, and every authentication outcome lands
in the append-only ``auth_audit`` table — token *names* only, never values.

Policy (which outcome maps to which HTTP response, rate limiting) lives in
:mod:`healthspan.api_security`; this module is the storage contract. All
functions take an open connection from the ADR-0037 pool and follow the
repository write discipline: explicit ``BEGIN IMMEDIATE`` transactions.
"""

import hashlib
import re
import secrets
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass

import sqlcipher3

from healthspan.keyparams import utc_now_iso

TOKEN_PREFIX = "hsp_"  # noqa: S105 - scanner-recognition prefix, not a credential

# The nine flat scopes (ADR-0026, extended by ADR-0040 `monitor` and
# ADR-0043 `annotate`). No hierarchy, no wildcards.
SCOPES = frozenset(
    {
        "read",
        "write",
        "annotate",
        "import",
        "events",
        "jobs",
        "monitor",
        "supervise",
        "admin",
    }
)

# Audit token_name for credentials that resolve to no token row (ADR-0026).
INVALID_NAME = "invalid"

# auth_audit outcomes (ADR-0026; the CHECK constraint mirrors this set).
OUTCOME_OK = "ok"
OUTCOME_DENIED_SCOPE = "denied:scope"
OUTCOME_DENIED_INVALID = "denied:invalid"
OUTCOME_DENIED_REVOKED = "denied:revoked"
OUTCOME_RATE_LIMITED = "rate-limited"

# Token names never contain '_': the secret segment of `hsp_<name>_<secret>`
# is base64url (which uses both '-' and '_'), so the first '_' after the
# prefix must end the name for the format to parse unambiguously. ':' is
# allowed for the ephemeral `job:<uuid>` convention (ADR-0026).
_NAME = re.compile(r"^[a-z0-9][a-z0-9:-]*$")


class TokenError(Exception):
    """A token could not be minted or altered."""


@dataclass(frozen=True)
class TokenRecord:
    """One verified row of the ``tokens`` table (no secret material)."""

    id: int
    name: str
    scopes: frozenset[str]
    authorship: str
    publish_namespaces: tuple[str, ...]
    job_id: int | None
    created_utc: str
    last_used_utc: str | None
    revoked: bool


def format_token(name: str, secret: str) -> str:
    """``hsp_<name>_<secret>`` (ADR-0026 token format)."""
    return f"{TOKEN_PREFIX}{name}_{secret}"


def parse_name(presented: str) -> str | None:
    """The advisory name segment, or ``None`` if the shape does not parse.

    Advisory only (ADR-0026): identification for audit rows and rate-limiter
    buckets. Authorization derives solely from the server-side record.
    """
    if not presented.startswith(TOKEN_PREFIX):
        return None
    name, separator, secret = presented[len(TOKEN_PREFIX) :].partition("_")
    if not separator or not secret or _NAME.fullmatch(name) is None:
        return None
    return name


def hash_token(presented: str) -> str:
    """SHA-256 hex of the full token string — the only stored form."""
    return hashlib.sha256(presented.encode("utf-8")).hexdigest()


@contextmanager
def write_transaction(conn: sqlcipher3.Connection) -> Generator[None]:
    """Repository write discipline: ``BEGIN IMMEDIATE`` up front (ADR-0037)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def mint_token(
    conn: sqlcipher3.Connection,
    name: str,
    scopes: frozenset[str] | set[str],
    *,
    authorship: str = "self",
    publish_namespaces: tuple[str, ...] = (),
    job_id: int | None = None,
) -> str:
    """Mint a token: hash into the store, return the plaintext exactly once.

    The caller owns delivering the plaintext to its holder (keyring entry,
    one-time print); it never touches the database again.
    """
    if _NAME.fullmatch(name) is None:
        raise TokenError(
            f"invalid token name {name!r}: lowercase letters, digits, '-' and "
            "':' only (an underscore would make the token format ambiguous)"
        )
    if not scopes:
        raise TokenError("a token must carry at least one scope (ADR-0026)")
    unknown = set(scopes) - SCOPES
    if unknown:
        raise TokenError(
            f"unknown scope(s) {sorted(unknown)!r}; valid: {sorted(SCOPES)}"
        )
    if publish_namespaces and "events" not in scopes:
        raise TokenError(
            "publish_namespaces is the events-scope allowlist; it is "
            "meaningless without the 'events' scope (ADR-0026)"
        )
    secret = secrets.token_urlsafe(32)  # 32 random bytes, base64url (ADR-0026)
    token = format_token(name, secret)
    try:
        with write_transaction(conn):
            conn.execute(
                "INSERT INTO tokens (name, token_hash, scopes, authorship, "
                "publish_namespaces, job_id, created_utc) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    name,
                    hash_token(token),
                    " ".join(sorted(scopes)),
                    authorship,
                    " ".join(publish_namespaces) or None,
                    job_id,
                    utc_now_iso(),
                ),
            )
    except sqlcipher3.IntegrityError as exc:
        raise TokenError(f"could not mint token {name!r}: {exc}") from exc
    return token


def look_up(conn: sqlcipher3.Connection, presented: str) -> TokenRecord | None:
    """The token row a presented credential resolves to, or ``None``.

    Hashes the presented value and compares digests
    (:func:`secrets.compare_digest`, ADR-0026). Returns revoked rows —
    revoked-vs-invalid is a policy distinction the caller audits differently
    while answering identically (uniform 401).
    """
    computed = hash_token(presented)
    row = conn.execute(
        "SELECT id, name, token_hash, scopes, authorship, publish_namespaces, "
        "job_id, created_utc, last_used_utc, revoked "
        "FROM tokens WHERE token_hash = ?",
        (computed,),
    ).fetchone()
    if row is None or not secrets.compare_digest(str(row[2]), computed):
        return None
    return TokenRecord(
        id=row[0],
        name=row[1],
        scopes=frozenset(str(row[3]).split()),
        authorship=row[4],
        publish_namespaces=tuple(str(row[5]).split()) if row[5] else (),
        job_id=row[6],
        created_utc=row[7],
        last_used_utc=row[8],
        revoked=bool(row[9]),
    )


def revoke_token(conn: sqlcipher3.Connection, name: str) -> bool:
    """Revoke immediately (no grace overlap, ADR-0026); False if unknown."""
    with write_transaction(conn):
        cursor = conn.execute(
            "UPDATE tokens SET revoked = 1, revoked_utc = ? "
            "WHERE name = ? AND revoked = 0",
            (utc_now_iso(), name),
        )
    return cursor.rowcount > 0


def record_outcome(
    conn: sqlcipher3.Connection,
    *,
    token_name: str,
    source_addr: str,
    endpoint: str,
    method: str,
    outcome: str,
) -> None:
    """Append one auth_audit row (names and metadata only, never values)."""
    with write_transaction(conn):
        _insert_audit(conn, token_name, source_addr, endpoint, method, outcome)


def record_ok(
    conn: sqlcipher3.Connection,
    record: TokenRecord,
    *,
    source_addr: str,
    endpoint: str,
    method: str,
) -> None:
    """Audit a successful authentication and touch ``last_used_utc``, atomically."""
    now = utc_now_iso()
    with write_transaction(conn):
        _insert_audit(conn, record.name, source_addr, endpoint, method, OUTCOME_OK)
        conn.execute(
            "UPDATE tokens SET last_used_utc = ? WHERE id = ?", (now, record.id)
        )


def _insert_audit(
    conn: sqlcipher3.Connection,
    token_name: str,
    source_addr: str,
    endpoint: str,
    method: str,
    outcome: str,
) -> None:
    conn.execute(
        "INSERT INTO auth_audit (occurred_utc, token_name, source_addr, "
        "endpoint, method, outcome) VALUES (?, ?, ?, ?, ?, ?)",
        (utc_now_iso(), token_name, source_addr, endpoint, method, outcome),
    )
