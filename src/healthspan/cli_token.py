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

from healthspan import keychain
from healthspan.api_tokens import (
    RESET_LIMITS_PATH,
    ROTATE_MCP_SECRET_PATH,
    TOKENS_PATH,
)
from healthspan.cli_support import fail, load_config_or_exit
from healthspan.config import Config

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
    return httpx.Client(
        base_url=f"http://{cfg.service.host}:{cfg.service.port}", timeout=10.0
    )


def _admin_token() -> str:
    try:
        token = keychain.load_token_plaintext("cli-admin")
    except keychain.KeychainError as exc:
        raise fail(str(exc)) from exc
    if token is None:
        raise fail(
            "no cli-admin token in the OS keyring (entry 'token:cli-admin'). "
            "The default token set is minted at the first 'healthspan service "
            "start' (ADR-0050); start the service once, then retry."
        )
    return token


def _request(ctx: typer.Context, method: str, path: str, **kwargs: Any) -> Any:
    """One authenticated call; API and transport errors become CLI failures."""
    cfg = load_config_or_exit(ctx)
    headers = {"Authorization": f"Bearer {_admin_token()}"}
    try:
        with _build_client(cfg) as client:
            response = client.request(method, path, headers=headers, **kwargs)
    except httpx.ConnectError as exc:
        raise fail(
            f"the Core Service is not reachable at "
            f"http://{cfg.service.host}:{cfg.service.port} ({exc}). Start it "
            "with 'healthspan service start' - token management goes through "
            "its REST API (ADR-0026/0051)."
        ) from exc
    except httpx.HTTPError as exc:
        raise fail(f"request to the Core Service failed: {exc}") from exc
    if response.status_code == 401:
        raise fail(
            "the Core Service rejected the cli-admin credential (401): the "
            "'token:cli-admin' keyring entry is stale. If the token was "
            "rotated, the rotation response carried the new value - store it "
            "in the keyring entry and retry."
        )
    if response.status_code >= 400:
        detail = _detail(response)
        raise fail(f"the Core Service answered {response.status_code}: {detail}")
    return response.json()


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text
    if isinstance(body, dict) and "detail" in body:
        return str(body["detail"])  # pyright: ignore[reportUnknownArgumentType]
    return response.text


def _parse_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


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
    _request(ctx, "POST", f"{TOKENS_PATH}/{name}/revoke")
    typer.echo(f"Token '{name}' revoked.")


@token_app.command("rotate")
def token_rotate(ctx: typer.Context, name: str) -> None:
    """Revoke and reissue under the same name and scopes; print once."""
    result = _request(ctx, "POST", f"{TOKENS_PATH}/{name}/rotate")
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
