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
from collections.abc import Generator, Iterable
from contextlib import contextmanager
from dataclasses import dataclass, field

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


@dataclass(frozen=True)
class TokenSpec:
    """What to mint: a name, its scopes, and the optional attributes."""

    name: str
    scopes: frozenset[str]
    authorship: str = "self"
    publish_namespaces: tuple[str, ...] = field(default=())
    job_id: int | None = None


def format_token(name: str, secret: str) -> str:
    """``hsp_<name>_<secret>`` (ADR-0026 token format)."""
    return f"{TOKEN_PREFIX}{name}_{secret}"


def generate_secret() -> str:
    """A 32-byte cryptographically random base64url secret (ADR-0026)."""
    return secrets.token_urlsafe(32)


def valid_name(name: str) -> bool:
    """Whether ``name`` is a legal token name (the mint-time rule).

    The charset is URL-safe by construction, so callers embedding a valid
    name in a request path never need escaping; an invalid name is rejected
    before it can be misread as path structure.
    """
    return _NAME.fullmatch(name) is not None


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


def _validate_spec(spec: TokenSpec) -> None:
    if not valid_name(spec.name):
        raise TokenError(
            f"invalid token name {spec.name!r}: lowercase letters, digits, '-' "
            "and ':' only (an underscore would make the token format ambiguous)"
        )
    if not spec.scopes:
        raise TokenError("a token must carry at least one scope (ADR-0026)")
    unknown = set(spec.scopes) - SCOPES
    if unknown:
        raise TokenError(
            f"unknown scope(s) {sorted(unknown)!r}; valid: {sorted(SCOPES)}"
        )
    if spec.publish_namespaces and "events" not in spec.scopes:
        raise TokenError(
            "publish_namespaces is the events-scope allowlist; it is "
            "meaningless without the 'events' scope (ADR-0026)"
        )


def store_tokens(
    conn: sqlcipher3.Connection, minted: Iterable[tuple[TokenSpec, str]]
) -> None:
    """Insert pre-generated tokens' hashes in one all-or-nothing transaction.

    The bootstrap path (ADR-0050 §1) mints the whole default set atomically:
    a partially-minted table would never be re-minted (the emptiness check
    is the idempotence guard), so it must be impossible. Callers generate
    plaintexts first — delivering them (keyring, one-time print) is theirs.
    """
    pairs = list(minted)
    for spec, _ in pairs:
        _validate_spec(spec)
    try:
        with write_transaction(conn):
            for spec, token in pairs:
                conn.execute(
                    "INSERT INTO tokens (name, token_hash, scopes, authorship, "
                    "publish_namespaces, job_id, created_utc) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        spec.name,
                        hash_token(token),
                        " ".join(sorted(spec.scopes)),
                        spec.authorship,
                        " ".join(spec.publish_namespaces) or None,
                        spec.job_id,
                        utc_now_iso(),
                    ),
                )
    except sqlcipher3.IntegrityError as exc:
        names = ", ".join(spec.name for spec, _ in pairs)
        raise TokenError(f"could not mint token(s) {names!r}: {exc}") from exc


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
    spec = TokenSpec(
        name=name,
        scopes=frozenset(scopes),
        authorship=authorship,
        publish_namespaces=publish_namespaces,
        job_id=job_id,
    )
    token = format_token(name, generate_secret())
    store_tokens(conn, [(spec, token)])
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
        _RECORD_SELECT + " WHERE token_hash = ?",
        (computed,),
    ).fetchone()
    if row is None or not secrets.compare_digest(str(row[2]), computed):
        return None
    return _record_from_row(row)


_RECORD_SELECT = (
    "SELECT id, name, token_hash, scopes, authorship, publish_namespaces, "
    "job_id, created_utc, last_used_utc, revoked FROM tokens"
)


def _record_from_row(row: tuple[object, ...]) -> TokenRecord:
    return TokenRecord(
        id=int(str(row[0])),
        name=str(row[1]),
        scopes=frozenset(str(row[3]).split()),
        authorship=str(row[4]),
        publish_namespaces=tuple(str(row[5]).split()) if row[5] else (),
        job_id=int(str(row[6])) if row[6] is not None else None,
        created_utc=str(row[7]),
        last_used_utc=str(row[8]) if row[8] is not None else None,
        revoked=bool(row[9]),
    )


def find_by_name(conn: sqlcipher3.Connection, name: str) -> TokenRecord | None:
    """The token row for a name (revoked included), or ``None`` if unknown."""
    row = conn.execute(_RECORD_SELECT + " WHERE name = ?", (name,)).fetchone()
    return None if row is None else _record_from_row(row)


def list_tokens(conn: sqlcipher3.Connection) -> list[TokenRecord]:
    """Every token row, by name — metadata only, hashes never leave here."""
    rows = conn.execute(_RECORD_SELECT + " ORDER BY name").fetchall()
    return [_record_from_row(row) for row in rows]


def count_tokens(conn: sqlcipher3.Connection) -> int:
    """How many token rows exist (the bootstrap emptiness check, ADR-0050)."""
    row = conn.execute("SELECT count(*) FROM tokens").fetchone()
    return int(str(row[0])) if row is not None else 0


class LastAdminError(TokenError):
    """Revocation refused: it would leave no live ``admin`` credential."""


def revoke_token(conn: sqlcipher3.Connection, name: str) -> bool:
    """Revoke immediately (no grace overlap, ADR-0026); False if unknown.

    Refuses (``LastAdminError``) to revoke the last live token carrying
    ``admin`` (ADR-0051): bootstrap never re-mints into a non-empty table
    and no direct-database escape hatch exists, so losing the final admin
    credential is irreversible. The check runs inside the same
    ``BEGIN IMMEDIATE`` transaction as the update, so two concurrent
    requests revoking each other's admin tokens serialize — the second one
    is refused, and at least one live admin credential always survives.
    """
    with write_transaction(conn):
        target = conn.execute(
            "SELECT scopes FROM tokens WHERE name = ? AND revoked = 0", (name,)
        ).fetchone()
        if target is not None and "admin" in str(target[0]).split():
            others = conn.execute(
                "SELECT count(*) FROM tokens WHERE revoked = 0 AND name != ? "
                "AND (' ' || scopes || ' ') LIKE '% admin %'",
                (name,),
            ).fetchone()
            if others is None or int(str(others[0])) == 0:
                raise LastAdminError(
                    f"refusing to revoke '{name}': it is the last live token "
                    "carrying the 'admin' scope, and no path could reissue "
                    "admin capability (rotate it instead, ADR-0051)"
                )
        cursor = conn.execute(
            "UPDATE tokens SET revoked = 1, revoked_utc = ? "
            "WHERE name = ? AND revoked = 0",
            (utc_now_iso(), name),
        )
    return cursor.rowcount > 0


def rotate_token(conn: sqlcipher3.Connection, name: str) -> str | None:
    """Reissue a token under its name; return the new plaintext, once.

    ADR-0026's "revoke + reissue same name/scopes" realized as an atomic
    in-place hash replacement (ADR-0051): ``tokens.name`` is UNIQUE, so a
    revoked row and its replacement cannot coexist, and swapping the hash
    in one UPDATE makes the old value dead and the new one live with no
    window between. Scopes and attributes are untouched; ``last_used_utc``
    resets (the new credential has never been used); a revoked token
    rotates back to live — rotation is the sanctioned reissue path for a
    revoked name. ``None`` if the name is unknown.
    """
    token = format_token(name, generate_secret())
    with write_transaction(conn):
        cursor = conn.execute(
            "UPDATE tokens SET token_hash = ?, created_utc = ?, "
            "last_used_utc = NULL, revoked = 0, revoked_utc = NULL "
            "WHERE name = ?",
            (hash_token(token), utc_now_iso(), name),
        )
    return token if cursor.rowcount > 0 else None


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
