"""Endpoint authentication and scope enforcement over a real database
(ADR-0026/0040): the 401/403/429 matrix, uniform denials, audit rows, and
the `monitor`-scoped health-detail and metrics endpoints.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlcipher3
from fastapi import FastAPI
from fastapi.testclient import TestClient

from healthspan import db, migrate, tokens
from healthspan.api_health import HEALTH_DETAIL_PATH, LIVENESS_PATH
from healthspan.api_metrics import METRICS_PATH
from healthspan.api_security import AuthFailureRateLimiter, assert_all_routes_declared
from healthspan.config import Config
from healthspan.kdf import DbKey
from healthspan.locking import InstanceLock
from healthspan.pool import ConnectionPool
from healthspan.service import create_app
from healthspan.service_runtime import ServiceRuntime

KEY_BYTES = bytes(range(1, 33))


def _key() -> DbKey:
    # Fresh instance per use: the app lifespan zeroizes the runtime's copy.
    return DbKey(bytearray(KEY_BYTES))


@dataclass
class Harness:
    # Typed Any: starlette's TestClient is partially unknown under pyright
    # strict (the WI-1 gotcha); every use goes through _get, which returns a
    # fully-typed httpx.Response.
    client: Any
    app: FastAPI
    runtime: ServiceRuntime
    db_path: Path
    monitor_token: str
    reader_token: str


@pytest.fixture
def harness(make_config: Callable[[], Config]) -> Iterator[Harness]:
    cfg = make_config()
    db.provision(cfg.database.path, _key())
    migrate.migrate_database(cfg.database.path, _key())
    setup = db.connect(cfg.database.path, _key())
    try:
        monitor_token = tokens.mint_token(setup, "monitor-probe", {"monitor"})
        reader_token = tokens.mint_token(setup, "reader", {"read"})
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
    with TestClient(application) as client:
        yield Harness(
            client=client,
            app=application,
            runtime=runtime,
            db_path=cfg.database.path,
            monitor_token=monitor_token,
            reader_token=reader_token,
        )


def _get(client: Any, path: str, token: str | None = None) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    response: httpx.Response = client.get(path, headers=headers)
    return response


def _audit_rows(db_path: Path) -> list[tuple[str, str, str, str]]:
    conn = db.connect(db_path, _key())
    try:
        return conn.execute(
            "SELECT token_name, endpoint, method, outcome FROM auth_audit ORDER BY id"
        ).fetchall()
    finally:
        db.close(conn)


def _store_conn(db_path: Path) -> sqlcipher3.Connection:
    return db.connect(db_path, _key())


# --------------------------------------------------------------------------
# 401: uniform denial for missing, invalid, and revoked credentials
# --------------------------------------------------------------------------


def test_missing_credential_is_401_and_audited(harness: Harness) -> None:
    response = _get(harness.client, HEALTH_DETAIL_PATH)
    assert response.status_code == 401
    assert response.json() == {"detail": "authentication failed"}
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert _audit_rows(harness.db_path) == [
        ("invalid", HEALTH_DETAIL_PATH, "GET", "denied:invalid")
    ]


def test_malformed_and_unknown_credentials_are_401(harness: Harness) -> None:
    for bad in ("nonsense", "hsp_ghost_notarealsecret"):
        response = _get(harness.client, HEALTH_DETAIL_PATH, token=bad)
        assert response.status_code == 401
        assert response.json() == {"detail": "authentication failed"}


def test_revoked_token_answer_is_indistinguishable_from_invalid(
    harness: Harness,
) -> None:
    conn = _store_conn(harness.db_path)
    try:
        assert tokens.revoke_token(conn, "monitor-probe")
    finally:
        db.close(conn)
    revoked = _get(harness.client, HEALTH_DETAIL_PATH, token=harness.monitor_token)
    unknown = _get(harness.client, HEALTH_DETAIL_PATH, token="hsp_ghost_secret")
    # Uniform denial (ADR-0026): status, body, and challenge all identical.
    assert revoked.status_code == unknown.status_code == 401
    assert revoked.json() == unknown.json()
    assert revoked.headers["WWW-Authenticate"] == unknown.headers["WWW-Authenticate"]
    # ...while the audit trail records which was which.
    outcomes = [row[3] for row in _audit_rows(harness.db_path)]
    assert outcomes == ["denied:revoked", "denied:invalid"]


# --------------------------------------------------------------------------
# 403: authenticated but out of scope
# --------------------------------------------------------------------------


def test_missing_scope_is_403_naming_token_and_scope(harness: Harness) -> None:
    response = _get(harness.client, HEALTH_DETAIL_PATH, token=harness.reader_token)
    assert response.status_code == 403
    detail = response.json()["detail"]
    assert "reader" in detail
    assert "monitor" in detail
    assert harness.reader_token not in detail  # never echo the credential
    assert _audit_rows(harness.db_path) == [
        ("reader", HEALTH_DETAIL_PATH, "GET", "denied:scope")
    ]


# --------------------------------------------------------------------------
# 200: authorized requests, audit, and last-used
# --------------------------------------------------------------------------


def test_health_detail_shape_and_values(harness: Harness) -> None:
    response = _get(harness.client, HEALTH_DETAIL_PATH, token=harness.monitor_token)
    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {
        "status",
        "version",
        "schema_version",
        "db_connected",
        "uptime_seconds",
    }
    assert body["status"] == "healthy"
    assert body["schema_version"] == 3
    assert body["db_connected"] is True
    assert body["uptime_seconds"] >= 0


def test_health_detail_reports_unhealthy_when_not_ready(harness: Harness) -> None:
    # The endpoint reports, it does not gate: a not-ready service still
    # answers 200 to an authenticated monitor, with the honest status word.
    harness.runtime.ready = False
    response = _get(harness.client, HEALTH_DETAIL_PATH, token=harness.monitor_token)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "unhealthy"
    assert body["db_connected"] is True  # the database itself is reachable


def test_metrics_shape_and_request_counting(harness: Harness) -> None:
    assert _get(harness.client, LIVENESS_PATH).status_code == 200
    response = _get(harness.client, METRICS_PATH, token=harness.monitor_token)
    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {
        "requests_total",
        "requests_by_status",
        "active_jobs",
        "db_query_count",
        "uptime_seconds",
    }
    assert body["requests_by_status"].get("200", 0) >= 1  # the liveness hit
    assert body["requests_total"] >= 1
    assert body["active_jobs"] == 0  # constant until the job system (Phase 4)
    assert body["db_query_count"] >= 1  # the token verification queries


def test_success_is_audited_and_last_used_touched(harness: Harness) -> None:
    assert (
        _get(harness.client, METRICS_PATH, token=harness.monitor_token).status_code
        == 200
    )
    assert _audit_rows(harness.db_path) == [
        ("monitor-probe", METRICS_PATH, "GET", "ok")
    ]
    conn = _store_conn(harness.db_path)
    try:
        record = tokens.look_up(conn, harness.monitor_token)
    finally:
        db.close(conn)
    assert record is not None
    assert record.last_used_utc is not None


def test_no_credential_material_ever_reaches_the_audit_table(
    harness: Harness,
) -> None:
    _get(harness.client, HEALTH_DETAIL_PATH, token=harness.monitor_token)
    _get(harness.client, HEALTH_DETAIL_PATH, token=harness.reader_token)
    _get(harness.client, HEALTH_DETAIL_PATH, token="hsp_forged_secretvalue")
    conn = _store_conn(harness.db_path)
    try:
        rows = conn.execute("SELECT * FROM auth_audit").fetchall()
    finally:
        db.close(conn)
    dump = " ".join(str(value) for row in rows for value in row)
    for secret in (harness.monitor_token, harness.reader_token, "secretvalue"):
        assert secret not in dump


# --------------------------------------------------------------------------
# Route-declaration invariants survive the new endpoints
# --------------------------------------------------------------------------


def test_liveness_is_still_the_only_public_route(harness: Harness) -> None:
    assert assert_all_routes_declared(harness.app) == [LIVENESS_PATH]
    assert _get(harness.client, LIVENESS_PATH).status_code == 200  # no credential


def test_detail_and_metrics_answer_only_with_monitor_scope(
    harness: Harness,
) -> None:
    for path in (HEALTH_DETAIL_PATH, METRICS_PATH):
        assert _get(harness.client, path).status_code == 401
        assert _get(harness.client, path, token=harness.reader_token).status_code == 403
        assert (
            _get(harness.client, path, token=harness.monitor_token).status_code == 200
        )


# --------------------------------------------------------------------------
# 429: the auth-failure rate limiter (ADR-0026 rules 1-4)
# --------------------------------------------------------------------------


def _freeze_limiter(harness: Harness, threshold: int = 5) -> None:
    """Swap in a limiter with a frozen clock: armed backoff never expires."""
    harness.app.state.auth_limiter = AuthFailureRateLimiter(
        failure_threshold=threshold, clock=lambda: 0.0
    )


def test_repeated_failures_answer_429_with_retry_after(harness: Harness) -> None:
    _freeze_limiter(harness, threshold=2)
    bad = "hsp_ghost_notarealsecret"
    for _ in range(3):  # two free failures, the third arms the backoff
        assert _get(harness.client, HEALTH_DETAIL_PATH, token=bad).status_code == 401
    throttled = _get(harness.client, HEALTH_DETAIL_PATH, token=bad)
    assert throttled.status_code == 429
    assert throttled.json() == {"detail": "too many failed authentication attempts"}
    assert int(throttled.headers["Retry-After"]) >= 1
    outcomes = [row[3] for row in _audit_rows(harness.db_path)]
    assert outcomes == ["denied:invalid"] * 3 + ["rate-limited"]


def test_valid_credential_is_never_throttled(harness: Harness) -> None:
    _freeze_limiter(harness, threshold=1)
    # Trip both the monitor-probe bucket and the invalid bucket at this
    # address, then some — the limiter must still wave the valid token
    # through (ADR-0026 rule 1).
    for _ in range(4):
        _get(harness.client, HEALTH_DETAIL_PATH, token="hsp_monitor-probe_wrong")
        _get(harness.client, HEALTH_DETAIL_PATH, token="garbage")
    assert (
        _get(
            harness.client, HEALTH_DETAIL_PATH, token=harness.monitor_token
        ).status_code
        == 200
    )


def test_a_valid_success_does_not_launder_an_attackers_failures(
    harness: Harness,
) -> None:
    # The loopback-launder scenario (ADR-0051 §1): a co-resident attacker
    # brute-forces under a live token's advisory name; the real token's
    # owner then authenticates successfully. That success must NOT clear the
    # attacker's accumulated failures — otherwise every legit poll resets the
    # wall. Pre-fix (clear-on-success) the final attempt was a fresh 401;
    # now the armed bucket survives the success and answers 429.
    _freeze_limiter(harness, threshold=1)
    forged = "hsp_monitor-probe_wrongsecret"  # parses to the live name
    for _ in range(2):  # one free failure, the second arms the bucket
        assert _get(harness.client, HEALTH_DETAIL_PATH, token=forged).status_code == 401
    # The real owner authenticates successfully in between.
    assert (
        _get(
            harness.client, HEALTH_DETAIL_PATH, token=harness.monitor_token
        ).status_code
        == 200
    )
    # The attacker's bucket under 'monitor-probe' is still armed.
    assert _get(harness.client, HEALTH_DETAIL_PATH, token=forged).status_code == 429


def test_revoked_token_throttles_and_audits_under_its_real_name(
    harness: Harness,
) -> None:
    _freeze_limiter(harness, threshold=1)
    conn = _store_conn(harness.db_path)
    try:
        assert tokens.revoke_token(conn, "reader")
    finally:
        db.close(conn)
    responses = [
        _get(harness.client, HEALTH_DETAIL_PATH, token=harness.reader_token)
        for _ in range(3)
    ]
    assert [r.status_code for r in responses] == [401, 401, 429]
    # The 429 discloses nothing about token state; the audit row records the
    # server-side name, never the advisory one of an unrecognized credential.
    rows = _audit_rows(harness.db_path)
    assert rows[-1] == ("reader", HEALTH_DETAIL_PATH, "GET", "rate-limited")


def test_missing_scope_is_not_an_authentication_failure(harness: Harness) -> None:
    _freeze_limiter(harness, threshold=1)
    # 403s are authenticated requests: repeating them must never trip the
    # failure limiter (the limiter throttles failures only, rule 1).
    for _ in range(5):
        assert (
            _get(
                harness.client, HEALTH_DETAIL_PATH, token=harness.reader_token
            ).status_code
            == 403
        )
