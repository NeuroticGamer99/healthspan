"""The bulk import endpoint ``POST /v1/import`` (ADR-0004/0052).

Scope gating (``import``), the structured success and ``422`` validation
responses, the ``?dry_run=true`` query parameter, and audit ``actor``
stamping from the authenticating token.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from healthspan import db, migrate, tokens
from healthspan.api_import import IMPORT_PATH
from healthspan.config import Config
from healthspan.kdf import DbKey
from healthspan.locking import InstanceLock
from healthspan.pool import ConnectionPool
from healthspan.service import create_app
from healthspan.service_runtime import ServiceRuntime


def _key() -> DbKey:
    return DbKey(bytearray(range(1, 33)))


@dataclass
class Harness:
    client: Any  # TestClient; typed Any under pyright strict (WI-1 gotcha)
    app: FastAPI
    db_path: Path
    import_token: str
    reader_token: str


@pytest.fixture
def harness(make_config: Callable[[], Config]) -> Iterator[Harness]:
    cfg = make_config()
    db.provision(cfg.database.path, _key())
    migrate.migrate_database(cfg.database.path, _key())
    setup = db.connect(cfg.database.path, _key())
    try:
        import_token = tokens.mint_token(setup, "watch-import", {"import"})
        reader_token = tokens.mint_token(setup, "reader", {"read"})
        setup.execute("BEGIN IMMEDIATE")
        # Migration 0004 (Phase 3 WI-2) now seeds its own catalog (labs,
        # biomarkers) on every fresh database; replace it with this fixed
        # test catalog rather than add to it, to avoid colliding on id 1
        # and on the natural-key unique indexes.
        setup.execute("DELETE FROM biomarker_aliases")
        setup.execute("DELETE FROM biomarkers")
        setup.execute("DELETE FROM labs")
        setup.execute("INSERT INTO labs (id, name) VALUES (1, 'Quest')")
        for biomarker_id in (1, 2, 3):
            setup.execute(
                "INSERT INTO biomarkers (id, canonical_name) VALUES (?, ?)",
                (biomarker_id, f"Biomarker {biomarker_id}"),
            )
        setup.execute("COMMIT")
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
            db_path=cfg.database.path,
            import_token=import_token,
            reader_token=reader_token,
        )


def _post(
    client: Any,
    token: str | None,
    body: dict[str, object],
    *,
    dry_run: bool = False,
) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    params = {"dry_run": "true"} if dry_run else {}
    response: httpx.Response = client.post(
        IMPORT_PATH, headers=headers, json=body, params=params
    )
    return response


def _panel(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "source": "manual",
        "conflict_policy": "reject",
        "lab_draws": [
            {"id": 1, "lab_id": 1, "draw_utc": "2024-03-14T13:30:00Z", "fasting": 1}
        ],
        "lab_results": [
            {"id": 1, "lab_draw_id": 1, "biomarker_id": 1, "value_num": 100.0},
            {"id": 2, "lab_draw_id": 1, "biomarker_id": 2, "value_num": 55.0},
        ],
    }
    body.update(overrides)
    return body


def _count(db_path: Path, sql: str) -> int:
    conn = db.connect(db_path, _key())
    try:
        row = conn.execute(sql).fetchone()
        return int(row[0]) if row is not None else 0
    finally:
        db.close(conn)


# --------------------------------------------------------------------------
# Scope gating
# --------------------------------------------------------------------------


def test_import_requires_authentication(harness: Harness) -> None:
    response = _post(harness.client, None, _panel())
    assert response.status_code == 401
    assert response.json()["detail"] == "authentication failed"


def test_import_requires_the_import_scope(harness: Harness) -> None:
    response = _post(harness.client, harness.reader_token, _panel())
    assert response.status_code == 403
    assert "import" in response.json()["detail"]


def test_import_succeeds_with_the_import_scope(harness: Harness) -> None:
    response = _post(harness.client, harness.import_token, _panel())
    assert response.status_code == 200


# --------------------------------------------------------------------------
# Success response shape
# --------------------------------------------------------------------------


def test_import_success_response_shape(harness: Harness) -> None:
    response = _post(harness.client, harness.import_token, _panel())
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body["batch_id"], int)
    assert body["dry_run"] is False
    assert body["conflict_policy"] == "reject"
    assert body["summary"]["lab_draws"]["rows_inserted"] == 1
    assert body["summary"]["lab_results"]["rows_inserted"] == 2
    assert _count(harness.db_path, "SELECT count(*) FROM lab_results_current") == 2


def test_import_dry_run_query_param_writes_nothing(harness: Harness) -> None:
    response = _post(harness.client, harness.import_token, _panel(), dry_run=True)
    assert response.status_code == 200
    body = response.json()
    assert body["batch_id"] is None
    assert body["dry_run"] is True
    assert body["summary"]["lab_results"]["rows_inserted"] == 2
    assert _count(harness.db_path, "SELECT count(*) FROM lab_results") == 0


# --------------------------------------------------------------------------
# Validation errors are a structured 422
# --------------------------------------------------------------------------


def test_import_validation_error_is_structured_422(harness: Harness) -> None:
    body = _panel(
        lab_results=[
            {"id": 1, "lab_draw_id": 1, "biomarker_id": 99, "value_num": 1.0},
        ]
    )
    response = _post(harness.client, harness.import_token, body)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, list)
    assert detail[0]["table"] == "lab_results"
    assert detail[0]["row_index"] == 0
    assert "does not exist in biomarkers" in detail[0]["message"]
    assert _count(harness.db_path, "SELECT count(*) FROM import_batches") == 0


def test_conflict_policy_is_required(harness: Harness) -> None:
    body = _panel()
    del body["conflict_policy"]
    response = _post(harness.client, harness.import_token, body)
    assert response.status_code == 422  # pydantic: missing required field


def test_unknown_top_level_key_is_rejected(harness: Harness) -> None:
    response = _post(harness.client, harness.import_token, _panel(cgm_readings=[]))
    assert response.status_code == 422  # extra='forbid'


# --------------------------------------------------------------------------
# Audit actor is the authenticating token name
# --------------------------------------------------------------------------


def test_import_audit_actor_is_the_token_name(harness: Harness) -> None:
    response = _post(harness.client, harness.import_token, _panel())
    assert response.status_code == 200
    conn = db.connect(harness.db_path, _key())
    try:
        actors = conn.execute(
            "SELECT DISTINCT actor FROM audit_log WHERE operation = 'import'"
        ).fetchall()
    finally:
        db.close(conn)
    assert [str(a[0]) for a in actors] == ["watch-import"]


# --------------------------------------------------------------------------
# Catalog tables (Phase 3 WI-2, ADR-0055/0054) are wired through the route
# --------------------------------------------------------------------------


def test_import_accepts_catalog_tables(harness: Harness) -> None:
    body = _panel(
        lab_draws=[],
        lab_results=[],
        categories=[{"name": "new_category", "description": None}],
        labs=[{"name": "New Lab", "description": None}],
        biomarkers=[{"canonical_name": "New Marker"}],
        biomarker_aliases=[{"biomarker_id": 1, "alias": "New Marker Alt"}],
    )
    response = _post(harness.client, harness.import_token, body)
    assert response.status_code == 200
    summary = response.json()["summary"]
    assert summary["categories"]["rows_inserted"] == 1
    assert summary["labs"]["rows_inserted"] == 1
    assert summary["biomarkers"]["rows_inserted"] == 1
    assert summary["biomarker_aliases"]["rows_inserted"] == 1
    assert (
        _count(
            harness.db_path,
            "SELECT count(*) FROM categories WHERE name = 'new_category'",
        )
        == 1
    )
    alias_row = db.connect(harness.db_path, _key())
    try:
        row = alias_row.execute(
            "SELECT alias_normalized, created_utc FROM biomarker_aliases "
            "WHERE alias = 'New Marker Alt'"
        ).fetchone()
    finally:
        db.close(alias_row)
    assert row is not None
    assert row[0] == "new marker alt"  # server-derived, never client input
    assert row[1] is not None


def test_import_catalog_update_audits_as_update_not_correct(
    harness: Harness,
) -> None:
    first = _post(
        harness.client,
        harness.import_token,
        _panel(
            lab_draws=[],
            lab_results=[],
            categories=[{"name": "new_category", "description": "old"}],
        ),
    )
    assert first.status_code == 200

    second = _post(
        harness.client,
        harness.import_token,
        _panel(
            lab_draws=[],
            lab_results=[],
            categories=[{"name": "new_category", "description": "new"}],
            conflict_policy="upsert",
        ),
    )
    assert second.status_code == 200
    assert second.json()["summary"]["categories"]["rows_corrected"] == 1
    conn = db.connect(harness.db_path, _key())
    try:
        ops = {
            str(row[0])
            for row in conn.execute(
                "SELECT operation FROM audit_log WHERE table_name = 'categories'"
            ).fetchall()
        }
    finally:
        db.close(conn)
    assert "correct" not in ops
    assert "update" in ops


# --------------------------------------------------------------------------
# lab_results.biomarker_name resolution over the HTTP route (ADR-0054 §4)
# --------------------------------------------------------------------------


def test_import_resolves_biomarker_name_over_http(harness: Harness) -> None:
    body = _panel(
        lab_results=[
            {
                "id": 1,
                "lab_draw_id": 1,
                "biomarker_name": "Biomarker 1",
                "value_num": 100.0,
            },
        ]
    )
    response = _post(harness.client, harness.import_token, body)
    assert response.status_code == 200
    assert _count(harness.db_path, "SELECT count(*) FROM lab_results_current") == 1
    conn = db.connect(harness.db_path, _key())
    try:
        row = conn.execute("SELECT biomarker_id FROM lab_results_current").fetchone()
    finally:
        db.close(conn)
    assert row is not None
    assert int(row[0]) == 1


def test_import_rejects_unresolved_biomarker_name_over_http(
    harness: Harness,
) -> None:
    body = _panel(
        lab_results=[
            {
                "id": 1,
                "lab_draw_id": 1,
                "biomarker_name": "Not A Real Marker",
                "value_num": 100.0,
            },
        ]
    )
    response = _post(harness.client, harness.import_token, body)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any(
        d["table"] == "lab_results" and "did not resolve" in d["message"]
        for d in detail
    )


def test_import_rejects_both_biomarker_id_and_name_over_http(
    harness: Harness,
) -> None:
    body = _panel(
        lab_results=[
            {
                "id": 1,
                "lab_draw_id": 1,
                "biomarker_id": 1,
                "biomarker_name": "Biomarker 1",
                "value_num": 100.0,
            },
        ]
    )
    response = _post(harness.client, harness.import_token, body)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any(
        d["table"] == "lab_results" and "exactly one of biomarker_id" in d["message"]
        for d in detail
    )
