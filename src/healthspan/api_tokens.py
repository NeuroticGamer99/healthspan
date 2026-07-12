"""Token administration endpoints (ADR-0026 lifecycle, ADR-0051).

The REST half of ``healthspan token …``, ``auth reset-limits``, and ``mcp
rotate-client-secret`` — every route requires ``admin``, so every use is a
visible, audited act (the verify dependency writes the ``auth_audit`` row).
Token *values* appear exactly once, in the mint/rotate response that
carries the new plaintext to its holder; list responses carry metadata
only, never values or hashes.

Self-lockout guard (ADR-0051): revoking the token that authenticates the
current request is refused with ``409`` — ``cli-admin`` is the only default
``admin`` holder, bootstrap never re-mints into a non-empty table, and no
direct-database escape hatch exists. Rotation is the sanctioned path for a
compromised ``cli-admin``.
"""

from typing import cast

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from healthspan import keychain, token_bootstrap, tokens
from healthspan.api_security import AuthFailureRateLimiter, require
from healthspan.logging_setup import get_logger
from healthspan.service_runtime import ServiceRuntime

router = APIRouter()

TOKENS_PATH = "/v1/tokens"
RESET_LIMITS_PATH = "/v1/auth/reset-limits"
ROTATE_MCP_SECRET_PATH = "/v1/mcp/rotate-client-secret"  # noqa: S105 - a path

_log = get_logger("healthspan.api_tokens")


class TokenCreateBody(BaseModel):
    """Mint request: a name, its scopes, optionally an events allowlist."""

    name: str
    scopes: list[str]
    publish_namespaces: list[str] = []


def _summary(record: tokens.TokenRecord) -> dict[str, object]:
    """Token metadata for responses — never values, never hashes."""
    return {
        "name": record.name,
        "scopes": sorted(record.scopes),
        "authorship": record.authorship,
        "publish_namespaces": list(record.publish_namespaces),
        "created_utc": record.created_utc,
        "last_used_utc": record.last_used_utc,
        "revoked": record.revoked,
    }


@router.get(TOKENS_PATH, dependencies=[require("admin")])
def list_tokens(request: Request) -> dict[str, object]:
    """Names, scopes, created, last-used, status — never values (ADR-0026)."""
    runtime = cast(ServiceRuntime, request.app.state.runtime)
    records = tokens.list_tokens(runtime.pool.connection())
    return {"tokens": [_summary(record) for record in records]}


@router.post(TOKENS_PATH, dependencies=[require("admin")], status_code=201)
def create_token(request: Request, body: TokenCreateBody) -> dict[str, object]:
    """Mint a named token; the plaintext appears in this response only."""
    runtime = cast(ServiceRuntime, request.app.state.runtime)
    conn = runtime.pool.connection()
    if tokens.find_by_name(conn, body.name) is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"token '{body.name}' already exists (a revoked name stays "
                "reserved as its revocation record; rotate it to reissue)"
            ),
        )
    try:
        token = tokens.mint_token(
            conn,
            body.name,
            set(body.scopes),
            publish_namespaces=tuple(body.publish_namespaces),
        )
    except tokens.TokenError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _log.info("token minted", name=body.name, scopes=sorted(set(body.scopes)))
    return {"name": body.name, "scopes": sorted(set(body.scopes)), "token": token}


@router.post(TOKENS_PATH + "/{name}/revoke", dependencies=[require("admin")])
def revoke_token(request: Request, name: str) -> dict[str, object]:
    """Immediate revocation (no grace overlap, ADR-0026); idempotent."""
    requester = cast(tokens.TokenRecord, request.state.token)
    if requester.name == name:
        raise HTTPException(
            status_code=409,
            detail=(
                f"refusing to revoke '{name}': it authenticates this request, "
                "and no other path could ever reissue admin capability "
                "(rotate it instead, ADR-0051)"
            ),
        )
    runtime = cast(ServiceRuntime, request.app.state.runtime)
    conn = runtime.pool.connection()
    record = tokens.find_by_name(conn, name)
    if record is None:
        raise HTTPException(status_code=404, detail=f"no token named '{name}'")
    if not record.revoked:
        tokens.revoke_token(conn, name)
        _log.info("token revoked", name=name)
    return {"name": name, "revoked": True}


@router.post(TOKENS_PATH + "/{name}/rotate", dependencies=[require("admin")])
def rotate_token(request: Request, name: str) -> dict[str, object]:
    """Reissue under the same name/scopes; update the keyring if local.

    The new plaintext appears in this response only. If a keyring entry
    ``token:<name>`` exists (every default token's does, from bootstrap),
    it is updated in place (ADR-0026); a keychain failure does not undo the
    rotation — the response still carries the plaintext, with
    ``keyring_updated`` false so the caller knows to store it.
    """
    runtime = cast(ServiceRuntime, request.app.state.runtime)
    conn = runtime.pool.connection()
    token = tokens.rotate_token(conn, name)
    if token is None:
        raise HTTPException(status_code=404, detail=f"no token named '{name}'")
    keyring_updated = False
    try:
        if keychain.load_token_plaintext(name) is not None:
            keychain.store_token_plaintext(name, token)
            keyring_updated = True
    except keychain.KeychainError:
        _log.warning("keyring entry not updated after rotation", name=name)
    _log.info("token rotated", name=name, keyring_updated=keyring_updated)
    return {"name": name, "token": token, "keyring_updated": keyring_updated}


@router.post(RESET_LIMITS_PATH, dependencies=[require("admin")])
def reset_limits(request: Request) -> dict[str, object]:
    """Clear all auth-failure limiter state (ADR-0026 rule 4).

    Always reachable: valid admin credentials are never throttled.
    """
    limiter = cast(AuthFailureRateLimiter, request.app.state.auth_limiter)
    limiter.reset()
    _log.info("auth rate-limiter state cleared")
    return {"reset": True}


@router.post(ROTATE_MCP_SECRET_PATH, dependencies=[require("admin")])
def rotate_mcp_client_secret(request: Request) -> dict[str, object]:
    """Rotate the MCP client-facing secret (ADR-0026); plaintext shown once.

    Not a Core token — the keyring holds its SHA-256 as the verifier-side
    record, so the rotation is a keyring-hash replacement (the same write
    bootstrap performs). Routed through Core rather than done client-side
    so the act lands in ``auth_audit`` like every admin action (ADR-0051).
    The MCP Server reads the hash at startup: restart it to take effect.
    """
    secret = token_bootstrap.generate_mcp_client_secret()
    try:
        keychain.store_mcp_client_hash(tokens.hash_token(secret))
    except keychain.KeychainError as exc:
        raise HTTPException(
            status_code=500,
            detail="could not update the MCP client-secret keyring entry",
        ) from exc
    _log.info("mcp client-facing secret rotated")
    return {"secret": secret, "restart_required": True}
