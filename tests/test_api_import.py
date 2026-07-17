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

from healthspan import db, imports, migrate, tokens
from healthspan.api_import import IMPORT_PATH, ImportRequest
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
        # and on the natural-key unique indexes. Every table referencing
        # `biomarkers` must be cleared first: 0004 seeds biomarker_aliases,
        # and 0005 (Phase 3 WI-3) seeds framework_ranges.
        setup.execute("DELETE FROM biomarker_aliases")
        setup.execute("DELETE FROM framework_ranges")
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


def _scalar(harness: Harness, sql: str, params: tuple[object, ...]) -> Any:
    """One scalar read straight from the harness's database.

    The import endpoint returns summaries, not rows, so asserting what actually
    landed (or did not) means looking at the database itself.
    """
    conn = db.connect(harness.db_path, _key())
    try:
        row = conn.execute(sql, params).fetchone()
        assert row is not None
        return row[0]
    finally:
        db.close(conn)


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


def test_import_request_covers_the_whole_registry() -> None:
    """Every engine-registered table has an ImportRequest field, and vice versa.

    `ImportRequest` is an allowlist (`extra='forbid'`), so a table registered in
    `imports.TABLES` but missing here is not importable over HTTP at all: the
    endpoint rejects it as an unknown field while the engine would have accepted
    it, and the two layers disagree with nothing to say so. That is exactly how
    `range_frameworks`/`framework_ranges` were briefly half-wired — added to the
    engine registry, invisible to the route. Pin the correspondence so the next
    table addition cannot land half-done.
    """
    fields = set(ImportRequest.model_fields)
    metadata = {"source", "adapter_id", "adapter_version", "note", "conflict_policy"}
    assert fields - metadata == set(imports.TABLES)


def test_import_order_covers_the_whole_registry_exactly_once() -> None:
    """IMPORT_ORDER is the third place a table name must appear, and the one
    that silently drops rows when it disagrees.

    `import_data` builds its payload by iterating `IMPORT_ORDER`, so a table
    present in both `TABLES` and `ImportRequest` but absent from `IMPORT_ORDER`
    is accepted by the endpoint and then never applied — a 200 reporting no
    rows, which is worse than the 422 a missing model field would give. A
    duplicate entry would apply a table twice. Neither is caught by pinning
    `ImportRequest` alone.
    """
    assert set(imports.IMPORT_ORDER) == set(imports.TABLES)
    assert len(imports.IMPORT_ORDER) == len(set(imports.IMPORT_ORDER))
    # CATALOG_TABLES is the source IMPORT_ORDER derives from; it must be a
    # prefix, so every catalog FK target is applied before its dependents.
    assert imports.IMPORT_ORDER[: len(imports.CATALOG_TABLES)] == imports.CATALOG_TABLES


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
# Range frameworks / framework ranges over HTTP (ADR-0058 §6) — the whole
# point of making them importable is that the owner can fill a `no_range`
# gap without a migration, so prove it through the real endpoint.
# --------------------------------------------------------------------------


def test_import_range_framework_then_its_ranges_over_http(harness: Harness) -> None:
    framework = _post(
        harness.client,
        harness.import_token,
        {
            "source": "manual",
            "conflict_policy": "upsert",
            "range_frameworks": [
                {
                    "name": "generic_targets",
                    "description": "A generic framework",
                    "source_url": "https://example.org/targets",
                }
            ],
        },
    )
    assert framework.status_code == 200
    assert framework.json()["summary"]["range_frameworks"]["rows_inserted"] == 1

    # A second batch: the framework must already be stored before its ranges
    # can name it (ADR-0057 §9 same-batch constraint, inherited unchanged).
    framework_id = _scalar(
        harness, "SELECT id FROM range_frameworks WHERE name = ?", ("generic_targets",)
    )
    row = {
        "framework_id": framework_id,
        "biomarker_id": 1,
        "range_high": 100.0,
        "unit": "mg/dL",
    }
    first = _post(
        harness.client,
        harness.import_token,
        {"source": "manual", "conflict_policy": "upsert", "framework_ranges": [row]},
    )
    assert first.status_code == 200
    assert first.json()["summary"]["framework_ranges"]["rows_inserted"] == 1

    # The dateless default reconciles on re-import rather than colliding with
    # its own partial unique index (the nullable-key `IS` match, ADR-0058 §6).
    again = _post(
        harness.client,
        harness.import_token,
        {"source": "manual", "conflict_policy": "upsert", "framework_ranges": [row]},
    )
    assert again.status_code == 200
    assert again.json()["summary"]["framework_ranges"]["rows_unchanged"] == 1
    assert _scalar(harness, "SELECT count(*) FROM framework_ranges", ()) == 1


def test_import_rejects_a_timestamped_effective_date_over_http(
    harness: Harness,
) -> None:
    # Not a Pydantic `extra_forbidden`: the table is a registered field, so this
    # must reach the engine and come back in the endpoint's own structured
    # {table, row_index, message} error shape (ADR-0058 §6).
    _post(
        harness.client,
        harness.import_token,
        {
            "source": "manual",
            "conflict_policy": "upsert",
            "range_frameworks": [{"name": "generic_targets"}],
        },
    )
    framework_id = _scalar(
        harness, "SELECT id FROM range_frameworks WHERE name = ?", ("generic_targets",)
    )
    response = _post(
        harness.client,
        harness.import_token,
        {
            "source": "manual",
            "conflict_policy": "upsert",
            "framework_ranges": [
                {
                    "framework_id": framework_id,
                    "biomarker_id": 1,
                    "range_high": 100.0,
                    "unit": "mg/dL",
                    "effective_date": "2025-01-01T00:00:00Z",
                }
            ],
        },
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    # A flat list of {table, row_index, message} — the engine's own error shape,
    # NOT pydantic's {type, loc, msg, input}. That distinction is the point: it
    # proves the payload reached the engine rather than bouncing off
    # ImportRequest's extra='forbid' allowlist as an unknown field.
    assert isinstance(detail, list)
    assert detail[0]["table"] == "framework_ranges"
    assert detail[0]["row_index"] == 0
    assert "effective_date" in detail[0]["message"]
    assert _scalar(harness, "SELECT count(*) FROM framework_ranges", ()) == 0


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
