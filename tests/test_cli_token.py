"""The token/auth/mcp lifecycle CLI (ADR-0026, ADR-0051): REST clients over
the admin endpoints, authenticating from the ``token:cli-admin`` keyring
entry, with actionable failures when the service or credential is absent.
"""

import socket
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Self

import httpx
import keyring
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from healthspan import cli_token, db, keychain, migrate, token_bootstrap, tokens
from healthspan.cli import app as cli_app
from healthspan.config import load_config
from healthspan.kdf import DbKey
from healthspan.locking import InstanceLock
from healthspan.pool import ConnectionPool
from healthspan.service import create_app
from healthspan.service_runtime import ServiceRuntime

runner = CliRunner()
KEY_BYTES = bytes(range(1, 33))


def _key() -> DbKey:
    return DbKey(bytearray(KEY_BYTES))


class _PortalClient(TestClient):
    """A TestClient whose context-manager protocol is a no-op.

    The CLI opens and closes its HTTP client per invocation; entering the
    real TestClient context would rerun the app lifespan and tear the
    shared runtime down on exit.
    """

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


@dataclass
class CliEnv:
    config_path: Path
    app: FastAPI
    db_path: Path


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[CliEnv]:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        'config_version = 1\n\n[database]\npath = "hs.db"\n', encoding="utf-8"
    )
    cfg = load_config(flag=config_path)
    db.provision(cfg.database.path, _key())
    migrate.migrate_database(cfg.database.path, _key())
    setup = db.connect(cfg.database.path, _key())
    try:
        token_bootstrap.bootstrap_default_tokens(setup, lambda _: None)
    finally:
        db.close(setup)
    lock = InstanceLock(cfg.database.path)
    lock.acquire()
    key = _key()
    runtime = ServiceRuntime(
        cfg=cfg,
        key=key,
        lock=lock,
        pool=ConnectionPool(cfg.database.path, key),
        schema_version=3,
    )
    application = create_app(runtime)
    with TestClient(application):

        def portal_client(_cfg: object) -> _PortalClient:
            return _PortalClient(application)

        monkeypatch.setattr(cli_token, "_build_client", portal_client)
        yield CliEnv(
            config_path=config_path, app=application, db_path=cfg.database.path
        )


def _invoke(env: CliEnv, *args: str, expect: int = 0) -> str:
    result = runner.invoke(cli_app, ["--config", str(env.config_path), *args])
    assert result.exit_code == expect, result.output
    return result.output


def test_token_list_shows_the_default_set_and_no_values(cli_env: CliEnv) -> None:
    output = _invoke(cli_env, "token", "list")
    for name in ("cli-admin", "gui", "mcp", "webhook", "launcher"):
        assert name in output
    assert "[active]" in output
    admin_plaintext = keychain.load_token_plaintext("cli-admin")
    assert admin_plaintext is not None
    assert admin_plaintext not in output


def test_token_create_prints_the_value_once(cli_env: CliEnv) -> None:
    output = _invoke(cli_env, "token", "create", "ci-probe", "--scopes", "read,monitor")
    assert "hsp_ci-probe_" in output
    assert "shown once" in output


def test_token_create_duplicate_reports_the_conflict(cli_env: CliEnv) -> None:
    output = _invoke(cli_env, "token", "create", "gui", "--scopes", "read", expect=1)
    assert "409" in output
    assert "already exists" in output


def test_token_revoke_and_the_self_revocation_guard(cli_env: CliEnv) -> None:
    assert "revoked" in _invoke(cli_env, "token", "revoke", "webhook")
    output = _invoke(cli_env, "token", "revoke", "cli-admin", expect=1)
    assert "409" in output
    assert "rotate" in output


def test_path_significant_names_are_rejected_before_any_request(
    cli_env: CliEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The token-name charset is URL-safe by construction, so the CLI
    # rejects anything else client-side — a name like 'a/b' must never be
    # sent where it would be misread as path structure. The monkeypatch
    # proves no request is attempted (the assertion is load-bearing).
    def no_request(*args: object, **kwargs: object) -> object:
        pytest.fail("an invalid name must be rejected before any request")

    monkeypatch.setattr(cli_token, "_build_client", no_request)
    for bad in ("a/b", "a?x", "a#y", "UPPER"):
        output = _invoke(cli_env, "token", "revoke", bad, expect=1)
        assert "invalid token name" in output


def test_token_rotate_updates_the_keyring_entry(cli_env: CliEnv) -> None:
    before = keychain.load_token_plaintext("gui")
    output = _invoke(cli_env, "token", "rotate", "gui")
    assert "updated in place" in output
    after = keychain.load_token_plaintext("gui")
    assert after is not None
    assert after != before
    assert after in output  # the printed value is the stored value


def test_auth_reset_limits_round_trips(cli_env: CliEnv) -> None:
    assert "cleared" in _invoke(cli_env, "auth", "reset-limits")


def test_mcp_rotate_client_secret_prints_once_and_stores_hash(
    cli_env: CliEnv,
) -> None:
    output = _invoke(cli_env, "mcp", "rotate-client-secret")
    secret = next(
        line.strip()
        for line in output.splitlines()
        if line.strip().startswith("hsp_mcpclient_")
    )
    assert keychain.load_mcp_client_hash() == tokens.hash_token(secret)
    assert "restart" in output


def test_missing_cli_admin_entry_gives_bootstrap_guidance(cli_env: CliEnv) -> None:
    keyring.delete_password(keychain.SERVICE, keychain.token_entry("cli-admin"))
    output = _invoke(cli_env, "token", "list", expect=1)
    assert "token:cli-admin" in output
    assert "service start" in output


def test_non_json_success_body_is_a_clean_error(
    cli_env: CliEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A 200 whose body is not JSON (something else on the port, a proxy)
    # must become the CLI's error channel, never a JSONDecodeError traceback.
    def not_json(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not the Core Service</html>")

    def fake_client(_cfg: object) -> httpx.Client:
        return httpx.Client(
            transport=httpx.MockTransport(not_json), base_url="http://testserver"
        )

    monkeypatch.setattr(cli_token, "_build_client", fake_client)
    output = _invoke(cli_env, "token", "list", expect=1)
    assert "non-JSON" in output


def test_unreachable_service_points_at_service_start(tmp_path: Path) -> None:
    # No monkeypatched client here: a real connection attempt against a
    # port nothing listens on.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'config_version = 1\n\n[database]\npath = "hs.db"\n\n'
        f"[service]\nport = {free_port}\n",
        encoding="utf-8",
    )
    keychain.store_token_plaintext("cli-admin", "hsp_cli-admin_placeholder")
    result = runner.invoke(cli_app, ["--config", str(config_path), "token", "list"])
    assert result.exit_code == 1, result.output
    assert "not reachable" in result.output
    assert "service start" in result.output
