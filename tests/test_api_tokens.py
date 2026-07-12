"""Token administration endpoints (ADR-0026 lifecycle, ADR-0051):
mint/list/revoke/rotate, the self-revocation guard, limiter reset, and
MCP client-secret rotation.
"""

import hashlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from healthspan import db, keychain, migrate, tokens
from healthspan.api_health import HEALTH_DETAIL_PATH
from healthspan.api_security import AuthFailureRateLimiter
from healthspan.api_tokens import (
    RESET_LIMITS_PATH,
    ROTATE_MCP_SECRET_PATH,
    TOKENS_PATH,
)
from healthspan.config import Config
from healthspan.kdf import DbKey
from healthspan.locking import InstanceLock
from healthspan.pool import ConnectionPool
from healthspan.service import create_app
from healthspan.service_runtime import ServiceRuntime

KEY_BYTES = bytes(range(1, 33))


def _key() -> DbKey:
    return DbKey(bytearray(KEY_BYTES))


@dataclass
class Harness:
    client: Any  # TestClient; typed Any under pyright strict (WI-1 gotcha)
    app: FastAPI
    db_path: Path
    admin_token: str
    monitor_token: str


@pytest.fixture
def harness(make_config: Callable[[], Config]) -> Iterator[Harness]:
    cfg = make_config()
    db.provision(cfg.database.path, _key())
    migrate.migrate_database(cfg.database.path, _key())
    setup = db.connect(cfg.database.path, _key())
    try:
        admin_token = tokens.mint_token(setup, "admin-probe", {"admin"})
        monitor_token = tokens.mint_token(setup, "monitor-probe", {"monitor"})
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
        schema_version=2,
    )
    application = create_app(runtime)
    with TestClient(application) as client:
        yield Harness(
            client=client,
            app=application,
            db_path=cfg.database.path,
            admin_token=admin_token,
            monitor_token=monitor_token,
        )


def _call(
    client: Any,
    method: str,
    path: str,
    token: str,
    json: dict[str, object] | None = None,
) -> httpx.Response:
    response: httpx.Response = client.request(
        method, path, headers={"Authorization": f"Bearer {token}"}, json=json
    )
    return response


def _lookup(db_path: Path, token: str) -> tokens.TokenRecord | None:
    conn = db.connect(db_path, _key())
    try:
        return tokens.look_up(conn, token)
    finally:
        db.close(conn)


# --------------------------------------------------------------------------
# Scope gating: every administration route requires `admin`
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", TOKENS_PATH),
        ("POST", TOKENS_PATH),
        ("POST", f"{TOKENS_PATH}/gui/revoke"),
        ("POST", f"{TOKENS_PATH}/gui/rotate"),
        ("POST", RESET_LIMITS_PATH),
        ("POST", ROTATE_MCP_SECRET_PATH),
    ],
)
def test_admin_scope_required(harness: Harness, method: str, path: str) -> None:
    denied = _call(harness.client, method, path, harness.monitor_token)
    assert denied.status_code == 403
    assert "admin" in denied.json()["detail"]


# --------------------------------------------------------------------------
# Mint and list
# --------------------------------------------------------------------------


def test_create_mints_a_working_token_and_prints_it_once(harness: Harness) -> None:
    response = _call(
        harness.client,
        "POST",
        TOKENS_PATH,
        harness.admin_token,
        json={"name": "backup-probe", "scopes": ["monitor"]},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "backup-probe"
    assert body["token"].startswith("hsp_backup-probe_")
    # The minted credential authenticates immediately.
    assert (
        _call(harness.client, "GET", HEALTH_DETAIL_PATH, body["token"]).status_code
        == 200
    )


def test_create_rejects_duplicates_bad_scopes_and_bad_names(harness: Harness) -> None:
    taken = _call(
        harness.client,
        "POST",
        TOKENS_PATH,
        harness.admin_token,
        json={"name": "monitor-probe", "scopes": ["read"]},
    )
    assert taken.status_code == 409
    assert "monitor-probe" in taken.json()["detail"]

    bad_scope = _call(
        harness.client,
        "POST",
        TOKENS_PATH,
        harness.admin_token,
        json={"name": "fresh", "scopes": ["omnipotent"]},
    )
    assert bad_scope.status_code == 400

    bad_name = _call(
        harness.client,
        "POST",
        TOKENS_PATH,
        harness.admin_token,
        json={"name": "under_score", "scopes": ["read"]},
    )
    assert bad_name.status_code == 400


def test_list_carries_metadata_and_never_values(harness: Harness) -> None:
    response = _call(harness.client, "GET", TOKENS_PATH, harness.admin_token)
    assert response.status_code == 200
    names = [row["name"] for row in response.json()["tokens"]]
    assert names == ["admin-probe", "monitor-probe"]
    dump = response.text
    assert harness.admin_token not in dump
    assert harness.monitor_token not in dump
    assert tokens.hash_token(harness.admin_token) not in dump


# --------------------------------------------------------------------------
# Revoke: immediate, idempotent, and never the requester's own token
# --------------------------------------------------------------------------


def test_revoke_is_immediate_and_idempotent(harness: Harness) -> None:
    first = _call(
        harness.client,
        "POST",
        f"{TOKENS_PATH}/monitor-probe/revoke",
        harness.admin_token,
    )
    assert first.status_code == 200
    assert (
        _call(
            harness.client, "GET", HEALTH_DETAIL_PATH, harness.monitor_token
        ).status_code
        == 401
    )
    again = _call(
        harness.client,
        "POST",
        f"{TOKENS_PATH}/monitor-probe/revoke",
        harness.admin_token,
    )
    assert again.status_code == 200


def test_revoke_unknown_name_is_404(harness: Harness) -> None:
    response = _call(
        harness.client, "POST", f"{TOKENS_PATH}/ghost/revoke", harness.admin_token
    )
    assert response.status_code == 404


def test_self_revocation_is_refused(harness: Harness) -> None:
    response = _call(
        harness.client,
        "POST",
        f"{TOKENS_PATH}/admin-probe/revoke",
        harness.admin_token,
    )
    assert response.status_code == 409
    assert "rotate" in response.json()["detail"]
    # Still live: the refusal really did protect the credential.
    assert (
        _call(harness.client, "GET", TOKENS_PATH, harness.admin_token).status_code
        == 200
    )


# --------------------------------------------------------------------------
# Rotate: old value dead, new value live, same name and scopes
# --------------------------------------------------------------------------


def test_rotate_swaps_the_credential_in_place(harness: Harness) -> None:
    response = _call(
        harness.client,
        "POST",
        f"{TOKENS_PATH}/monitor-probe/rotate",
        harness.admin_token,
    )
    assert response.status_code == 200
    body = response.json()
    new_token = body["token"]
    assert new_token.startswith("hsp_monitor-probe_")
    assert body["keyring_updated"] is False  # no keyring entry existed
    assert (
        _call(
            harness.client, "GET", HEALTH_DETAIL_PATH, harness.monitor_token
        ).status_code
        == 401
    )
    assert (
        _call(harness.client, "GET", HEALTH_DETAIL_PATH, new_token).status_code == 200
    )
    record = _lookup(harness.db_path, new_token)
    assert record is not None
    assert record.scopes == frozenset({"monitor"})
    assert record.last_used_utc is not None  # touched by the GET above


def test_rotate_updates_an_existing_keyring_entry(harness: Harness) -> None:
    keychain.store_token_plaintext("monitor-probe", harness.monitor_token)
    response = _call(
        harness.client,
        "POST",
        f"{TOKENS_PATH}/monitor-probe/rotate",
        harness.admin_token,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["keyring_updated"] is True
    assert keychain.load_token_plaintext("monitor-probe") == body["token"]


def test_rotate_survives_a_keychain_failure(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ADR-0051 §3: a keychain failure does not undo the committed rotation —
    # the response still carries the plaintext, flagged keyring_updated=false.
    keychain.store_token_plaintext("monitor-probe", harness.monitor_token)

    def broken_store(name: str, token: str) -> None:
        raise keychain.KeychainError("keyring backend unavailable")

    monkeypatch.setattr(keychain, "store_token_plaintext", broken_store)
    response = _call(
        harness.client,
        "POST",
        f"{TOKENS_PATH}/monitor-probe/rotate",
        harness.admin_token,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["keyring_updated"] is False
    # The rotation itself committed: old value dead, response value live.
    assert (
        _call(
            harness.client, "GET", HEALTH_DETAIL_PATH, harness.monitor_token
        ).status_code
        == 401
    )
    assert (
        _call(harness.client, "GET", HEALTH_DETAIL_PATH, body["token"]).status_code
        == 200
    )


def test_rotate_reissues_a_revoked_name(harness: Harness) -> None:
    _call(
        harness.client,
        "POST",
        f"{TOKENS_PATH}/monitor-probe/revoke",
        harness.admin_token,
    )
    response = _call(
        harness.client,
        "POST",
        f"{TOKENS_PATH}/monitor-probe/rotate",
        harness.admin_token,
    )
    assert response.status_code == 200
    reissued = response.json()["token"]
    assert _call(harness.client, "GET", HEALTH_DETAIL_PATH, reissued).status_code == 200


def test_rotate_unknown_name_is_404(harness: Harness) -> None:
    response = _call(
        harness.client, "POST", f"{TOKENS_PATH}/ghost/rotate", harness.admin_token
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------
# Limiter reset and MCP client-secret rotation
# --------------------------------------------------------------------------


def test_reset_limits_clears_an_armed_backoff(harness: Harness) -> None:
    harness.app.state.auth_limiter = AuthFailureRateLimiter(
        failure_threshold=1, clock=lambda: 0.0
    )
    bad = "hsp_ghost_nope"
    for _ in range(3):
        _call(harness.client, "GET", HEALTH_DETAIL_PATH, bad)
    assert _call(harness.client, "GET", HEALTH_DETAIL_PATH, bad).status_code == 429
    reset = _call(harness.client, "POST", RESET_LIMITS_PATH, harness.admin_token)
    assert reset.status_code == 200
    assert reset.json() == {"reset": True}
    assert _call(harness.client, "GET", HEALTH_DETAIL_PATH, bad).status_code == 401


def test_rotate_mcp_client_secret_replaces_the_keyring_hash(
    harness: Harness,
) -> None:
    keychain.store_mcp_client_hash("old-hash")
    response = _call(
        harness.client, "POST", ROTATE_MCP_SECRET_PATH, harness.admin_token
    )
    assert response.status_code == 200
    body = response.json()
    assert body["restart_required"] is True
    secret = body["secret"]
    assert secret.startswith("hsp_mcpclient_")
    stored = keychain.load_mcp_client_hash()
    assert stored == hashlib.sha256(secret.encode("utf-8")).hexdigest()
    assert stored != "old-hash"


def test_admin_actions_log_no_credential_material(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    # The mint/rotate log lines carry names and booleans only (security.md
    # logging rules): no plaintext from any admin response may reach the
    # process's stdout/stderr streams, where the structured logs go.
    created = _call(
        harness.client,
        "POST",
        TOKENS_PATH,
        harness.admin_token,
        json={"name": "log-probe", "scopes": ["read"]},
    ).json()["token"]
    rotated = _call(
        harness.client,
        "POST",
        f"{TOKENS_PATH}/monitor-probe/rotate",
        harness.admin_token,
    ).json()["token"]
    mcp_secret = _call(
        harness.client, "POST", ROTATE_MCP_SECRET_PATH, harness.admin_token
    ).json()["secret"]
    captured = capsys.readouterr()
    streams = captured.out + captured.err
    for secret in (created, rotated, mcp_secret, harness.admin_token):
        assert secret not in streams
