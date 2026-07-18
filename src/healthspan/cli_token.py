"""Token lifecycle CLI: ``token``, ``auth``, and ``mcp`` groups (ADR-0026).

Thin REST clients over the ``admin``-scoped endpoints in
:mod:`healthspan.api_tokens`, authenticating with the ``cli-admin`` token
from its keyring entry — ongoing credential management goes through the
Core Service so every act is scope-checked and audited (ADR-0051); only
bootstrap minting is direct (ADR-0050 §1). The Core Service must be
running; there is no direct-database fallback — the Phase 2 milestone
allows none beyond ``db migrate``/``db backup``/``db restore``.
"""

from typing import Annotated, Any

import httpx
import typer

from healthspan import cli_client, tokens
from healthspan.api_tokens import (
    RESET_LIMITS_PATH,
    ROTATE_MCP_SECRET_PATH,
    TOKENS_PATH,
)
from healthspan.cli_support import fail
from healthspan.config import Config

ADMIN_TOKEN_NAME = "cli-admin"  # noqa: S105 - token name, not a credential

token_app = typer.Typer(
    help="Manage named scoped tokens (requires the Core Service running).",
    no_args_is_help=True,
)
auth_app = typer.Typer(
    help="Authentication operations (requires the Core Service running).",
    no_args_is_help=True,
)
mcp_app = typer.Typer(
    help="MCP Server credential operations (requires the Core Service running).",
    no_args_is_help=True,
)

_SHOWN_ONCE = (
    "This value is shown once and stored nowhere else - copy it now if the "
    "holder is not using the keyring entry."
)


def _build_client(cfg: Config) -> httpx.Client:
    """The HTTP client for one command invocation (tests substitute this)."""
    return cli_client.default_client(cfg)


def _request(ctx: typer.Context, method: str, path: str, **kwargs: Any) -> Any:
    """One authenticated ``cli-admin`` call over the shared REST client.

    Delegates to :func:`cli_client.request`, binding the ``cli-admin`` token
    name and this module's ``_build_client`` seam (which the tests
    monkeypatch), so the token/auth/mcp groups share the one client
    implementation with :mod:`healthspan.cli_entry`.
    """
    return cli_client.request(
        ctx,
        method,
        path,
        token_name=ADMIN_TOKEN_NAME,
        build_client=_build_client,
        **kwargs,
    )


def _parse_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _path_name(name: str) -> str:
    """Validate a name destined for a URL path segment.

    The token-name charset (``tokens.valid_name``) is URL-safe by
    construction, so a valid name embeds verbatim; anything else — path
    separators, query markers, uppercase — is rejected here with the mint
    rule's vocabulary rather than sent on to be misread as URL structure.
    """
    if not tokens.valid_name(name):
        raise fail(
            f"invalid token name {name!r}: lowercase letters, digits, '-' and ':' only"
        )
    return name


@token_app.command("create")
def token_create(
    ctx: typer.Context,
    name: str,
    scopes: Annotated[
        str,
        typer.Option("--scopes", help="Comma-separated scope list, e.g. read,write."),
    ],
    publish_namespaces: Annotated[
        str,
        typer.Option(
            "--publish-namespaces",
            help="Comma-separated events-scope namespace allowlist, e.g. external.*",
        ),
    ] = "",
) -> None:
    """Mint a named token; the value is printed once (ADR-0026)."""
    body: dict[str, object] = {"name": name, "scopes": _parse_csv(scopes)}
    namespaces = _parse_csv(publish_namespaces)
    if namespaces:
        body["publish_namespaces"] = namespaces
    result = _request(ctx, "POST", TOKENS_PATH, json=body)
    typer.echo(
        f"Token '{result['name']}' created with scopes: {', '.join(result['scopes'])}"
    )
    typer.echo(f"  {result['token']}")
    typer.echo(_SHOWN_ONCE)


@token_app.command("list")
def token_list(ctx: typer.Context) -> None:
    """Names, scopes, created, last-used, status — never values."""
    result = _request(ctx, "GET", TOKENS_PATH)
    rows: list[dict[str, Any]] = result["tokens"]
    if not rows:
        typer.echo(
            "No tokens exist yet; the default set is minted at the first "
            "'healthspan service start' (ADR-0050)."
        )
        return
    for row in rows:
        status = "revoked" if row["revoked"] else "active"
        last_used = row["last_used_utc"] or "never"
        typer.echo(
            f"{row['name']}  [{status}]  scopes: {' '.join(row['scopes'])}  "
            f"created: {row['created_utc']}  last used: {last_used}"
        )


@token_app.command("revoke")
def token_revoke(ctx: typer.Context, name: str) -> None:
    """Revoke immediately; the name stays reserved (rotate to reissue)."""
    _request(ctx, "POST", f"{TOKENS_PATH}/{_path_name(name)}/revoke")
    typer.echo(f"Token '{name}' revoked.")


@token_app.command("rotate")
def token_rotate(ctx: typer.Context, name: str) -> None:
    """Revoke and reissue under the same name and scopes; print once."""
    result = _request(ctx, "POST", f"{TOKENS_PATH}/{_path_name(name)}/rotate")
    typer.echo(f"Token '{name}' rotated.")
    typer.echo(f"  {result['token']}")
    if result["keyring_updated"]:
        typer.echo(f"The keyring entry 'token:{name}' was updated in place.")
    else:
        typer.echo(_SHOWN_ONCE)


@auth_app.command("reset-limits")
def auth_reset_limits(ctx: typer.Context) -> None:
    """Clear auth-failure rate-limiter state (ADR-0026 rule 4)."""
    _request(ctx, "POST", RESET_LIMITS_PATH)
    typer.echo("Auth-failure rate limiter cleared.")


@mcp_app.command("rotate-client-secret")
def mcp_rotate_client_secret(ctx: typer.Context) -> None:
    """Rotate the MCP client-facing secret; print once (ADR-0026)."""
    result = _request(ctx, "POST", ROTATE_MCP_SECRET_PATH)
    typer.echo("MCP client-facing secret rotated.")
    typer.echo(f"  {result['secret']}")
    typer.echo(
        "Paste it into your AI client's configuration (static "
        "'Authorization: Bearer' header) and restart the MCP Server."
    )
