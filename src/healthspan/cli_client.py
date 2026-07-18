"""Shared plumbing for the CLI's REST clients (ADR-0006).

Every CLI group that talks to the Core Service over HTTP — the ``token``/
``auth``/``mcp`` lifecycle groups (:mod:`healthspan.cli_token`) and the
manual-entry/readback groups (:mod:`healthspan.cli_entry`) — authenticates
the same way: send a named token's plaintext as a bearer credential and turn
every transport or API error into a clean CLI failure
(:func:`healthspan.cli_support.fail`) rather than a traceback.

Two layers, so a multi-request command pays the setup once:

* :func:`send_on` / :func:`request_on` act on an **already-open** client with
  an **already-resolved** token — no config re-read, no keyring hit, no new
  connection per call. A command that issues many requests (``enter`` makes
  ~8-10) resolves the config once, reads the keyring once, opens one client,
  and threads them through these.
* :func:`request` is the one-shot ctx wrapper for the single-request lifecycle
  commands (``token``/``auth``/``mcp``): resolve, open, call, close.

The Core Service must be running; there is no direct-database fallback — the
Phase 2 milestone allows none beyond ``db migrate``/``db backup``/``db
restore`` (ADR-0051).
"""

from collections.abc import Callable
from typing import Any, cast

import httpx
import typer

from healthspan import keychain
from healthspan.cli_support import fail, load_config_or_exit
from healthspan.config import Config

ClientFactory = Callable[[Config], httpx.Client]


def default_client(cfg: Config) -> httpx.Client:
    """The HTTP client for one command invocation (tests substitute this).

    ``trust_env=False`` so an inherited ``HTTP_PROXY``/``HTTPS_PROXY`` cannot
    route this loopback, bearer-token-bearing request through an external proxy
    (httpx trusts those by default, and a stray ``NO_PROXY`` need not exempt
    localhost). The Core Service is always local (ADR-0049 loopback-only), so
    there is nothing a proxy should ever do here.
    """
    return httpx.Client(
        base_url=f"http://{cfg.service.host}:{cfg.service.port}",
        timeout=10.0,
        trust_env=False,
    )


def token_plaintext(token_name: str) -> str:
    """Load ``token_name``'s bearer plaintext from the keyring, or fail loud.

    The guidance distinguishes a default token (minted by ``service start``,
    ADR-0050 §1) from a hand-minted one (the least-privilege ``[cli]
    token_name`` case) — for the latter, ``service start`` mints nothing, so it
    points at ``token create`` and storing the value in the keyring instead.
    """
    try:
        token = keychain.load_token_plaintext(token_name)
    except keychain.KeychainError as exc:
        raise fail(str(exc)) from exc
    if token is None:
        raise fail(
            f"no '{token_name}' token in the OS keyring (entry "
            f"'{keychain.token_entry(token_name)}'). If '{token_name}' is a "
            "default token (cli-admin/gui/mcp/...), run 'healthspan service "
            "start' once — the default set is minted at first start (ADR-0050). "
            "If it is a token you minted yourself, create it with 'healthspan "
            f"token create {token_name}' carrying the scopes that client needs "
            "(read,import for the entry CLI), then store the printed value in "
            f"the keyring entry '{keychain.token_entry(token_name)}'."
        )
    return token


def send_on(
    client: httpx.Client,
    cfg: Config,
    token_name: str,
    token: str,
    method: str,
    path: str,
    **kwargs: Any,
) -> httpx.Response:
    """One call on a live client; transport failures and ``401`` fail loud.

    Returns the raw response for every other status, so a caller that must act
    on a structured ``422``/``409`` (the import preview offering an ``upsert``,
    the framework pre-flight) can inspect it rather than exit. Does no config
    or keyring I/O — the caller resolved those once and passes them in.
    """
    headers = {"Authorization": f"Bearer {token}"}
    try:
        response = client.request(method, path, headers=headers, **kwargs)
    except httpx.ConnectError as exc:
        raise fail(
            f"the Core Service is not reachable at "
            f"http://{cfg.service.host}:{cfg.service.port} ({exc}). Start it "
            "with 'healthspan service start' first - the CLI is a REST client "
            "(ADR-0006)."
        ) from exc
    except httpx.HTTPError as exc:
        raise fail(f"request to the Core Service failed: {exc}") from exc
    if response.status_code == 401:
        raise fail(
            f"the Core Service rejected the '{token_name}' credential (401): "
            f"the '{keychain.token_entry(token_name)}' keyring entry is stale. "
            "If the token was rotated, the rotation response carried the new "
            "value - store it in the keyring entry and retry."
        )
    return response


def request_on(
    client: httpx.Client,
    cfg: Config,
    token_name: str,
    token: str,
    method: str,
    path: str,
    **kwargs: Any,
) -> Any:
    """:func:`send_on` plus the "any ``>=400`` is fatal" rule; returns JSON."""
    response = send_on(client, cfg, token_name, token, method, path, **kwargs)
    if response.status_code >= 400:
        raise fail(
            f"the Core Service answered {response.status_code}: {detail(response)}"
        )
    try:
        return response.json()
    except ValueError as exc:
        raise fail(
            f"the Core Service answered {response.status_code} with a "
            "non-JSON body; is something else listening on its port?"
        ) from exc


def request(
    ctx: typer.Context,
    method: str,
    path: str,
    *,
    token_name: str,
    build_client: ClientFactory,
    **kwargs: Any,
) -> Any:
    """One-shot ctx wrapper: resolve config + token, open a client, call, close.

    For the single-request lifecycle commands; a multi-request command should
    open a client once and use :func:`request_on`/:func:`send_on` instead.
    """
    cfg = load_config_or_exit(ctx)
    token = token_plaintext(token_name)
    with build_client(cfg) as client:
        return request_on(client, cfg, token_name, token, method, path, **kwargs)


def detail(response: httpx.Response) -> str:
    """Best-effort human-readable error detail from a failed response."""
    try:
        body: Any = response.json()
    except ValueError:
        return response.text
    extracted = detail_from_body(body)
    return extracted if extracted is not None else response.text


def detail_from_body(body: Any) -> str | None:
    """The ``detail`` field of an already-parsed error body, or ``None``.

    The single owner of "what an error body's detail is", shared by
    :func:`detail` (from a response) and callers that already hold the parsed
    body (the import path, which keeps the body to act on structured errors).
    """
    if isinstance(body, dict) and "detail" in body:
        return str(cast(dict[str, Any], body)["detail"])
    return None
