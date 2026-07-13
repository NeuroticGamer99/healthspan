"""The read/query endpoints (ADR-0053): scope gating, page cap, round-trip.

The harness runs with a deliberately small ``service.page_cap`` (3) so cap
enforcement — the single Core-REST bound every client inherits
(api-reference.md, MCP tool-convention rule 3) — is exercised cheaply,
clamping included.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from healthspan import db, migrate, tokens
from healthspan.api_import import IMPORT_PATH
from healthspan.api_read import (
    BIOMARKERS_PATH,
    LAB_DRAWS_PATH,
    LAB_RESULTS_PATH,
    LABS_PATH,
)
from healthspan.config import Config
from healthspan.kdf import DbKey
from healthspan.locking import InstanceLock
from healthspan.pool import ConnectionPool
from healthspan.service import create_app
from healthspan.service_runtime import ServiceRuntime

PAGE_CAP = 3


def _key() -> DbKey:
    return DbKey(bytearray(range(1, 33)))


@dataclass
class Harness:
    client: Any  # TestClient; typed Any under pyright strict (WI-1 gotcha)
    app: FastAPI
    db_path: Path
    read_token: str
    import_token: str


@pytest.fixture
def harness(make_config: Callable[[], Config]) -> Iterator[Harness]:
    cfg = make_config()
    cfg = replace(cfg, service=replace(cfg.service, page_cap=PAGE_CAP))
    db.provision(cfg.database.path, _key())
    migrate.migrate_database(cfg.database.path, _key())
    setup = db.connect(cfg.database.path, _key())
    try:
        read_token = tokens.mint_token(setup, "reader", {"read"})
        import_token = tokens.mint_token(setup, "watch-import", {"import"})
        setup.execute("BEGIN IMMEDIATE")
        setup.execute("INSERT INTO labs (id, name) VALUES (1, 'Quest')")
        setup.execute("INSERT INTO labs (id, name) VALUES (2, 'LabCorp')")
        for biomarker_id, category in ((1, "lipids"), (2, "thyroid"), (3, None)):
            setup.execute(
                "INSERT INTO biomarkers (id, canonical_name, category) "
                "VALUES (?, ?, ?)",
                (biomarker_id, f"Biomarker {biomarker_id}", category),
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
            read_token=read_token,
            import_token=import_token,
        )


def _get(
    harness: Harness,
    path: str,
    *,
    token: str | None = None,
    params: dict[str, str] | None = None,
) -> httpx.Response:
    bearer = token if token is not None else harness.read_token
    response: httpx.Response = harness.client.get(
        path, headers={"Authorization": f"Bearer {bearer}"}, params=params or {}
    )
    return response


def _import_panel(
    harness: Harness,
    draws: list[dict[str, object]],
    results: list[dict[str, object]],
    *,
    policy: str = "reject",
) -> httpx.Response:
    response: httpx.Response = harness.client.post(
        IMPORT_PATH,
        headers={"Authorization": f"Bearer {harness.import_token}"},
        json={
            "source": "manual",
            "conflict_policy": policy,
            "lab_draws": draws,
            "lab_results": results,
        },
    )
    assert response.status_code == 200, response.text
    return response


def _draw(handle: int, draw_utc: str, **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {"id": handle, "lab_id": 1, "draw_utc": draw_utc}
    row.update(overrides)
    return row


# --------------------------------------------------------------------------
# Scope gating
# --------------------------------------------------------------------------

_LIST_PATHS = [LAB_DRAWS_PATH, LAB_RESULTS_PATH, LABS_PATH, BIOMARKERS_PATH]


@pytest.mark.parametrize("path", _LIST_PATHS)
def test_read_requires_authentication(harness: Harness, path: str) -> None:
    response: httpx.Response = harness.client.get(path)
    assert response.status_code == 401


@pytest.mark.parametrize("path", _LIST_PATHS)
def test_read_requires_read_scope(harness: Harness, path: str) -> None:
    response = _get(harness, path, token=harness.import_token)
    assert response.status_code == 403


def test_get_by_id_scope_gated(harness: Harness) -> None:
    response = _get(harness, f"{LABS_PATH}/1", token=harness.import_token)
    assert response.status_code == 403


# --------------------------------------------------------------------------
# Write-then-read round-trip (the Phase 2 milestone loop)
# --------------------------------------------------------------------------


def test_import_then_read_round_trip(harness: Harness) -> None:
    _import_panel(
        harness,
        draws=[_draw(1, "2024-03-14T13:30:00Z", fasting=1, lab_id=2)],
        results=[
            {
                "id": 1,
                "lab_draw_id": 1,
                "biomarker_id": 1,
                "value_num": 0.1,
                "comparator": "<",
                "unit": "ng/mL",
            },
            {
                "id": 2,
                "lab_draw_id": 1,
                "biomarker_id": 2,
                "value_text": "positive",
            },
        ],
    )
    draws = _get(harness, LAB_DRAWS_PATH).json()
    (draw,) = draws["items"]
    assert draw["draw_utc"] == "2024-03-14T13:30:00Z"
    assert draw["fasting"] == 1
    assert "superseded_by" not in draw

    censored = _get(harness, LAB_RESULTS_PATH, params={"biomarker_id": "1"}).json()
    (row,) = censored["items"]
    # Value fidelity (ADR-0030/0031): the triple, the UCUM unit, and a
    # display string that never degrades "<0.1" to a bare 0.1.
    assert (row["value_num"], row["comparator"], row["value_text"]) == (0.1, "<", None)
    assert row["unit"] == "ng/mL"
    assert row["display"] == "<0.1"
    assert row["draw_utc"] == "2024-03-14T13:30:00Z"  # embedded draw context
    assert row["lab_id"] == 2

    qualitative = _get(harness, LAB_RESULTS_PATH, params={"biomarker_id": "2"}).json()
    assert qualitative["items"][0]["display"] == "positive"

    by_id = _get(harness, f"{LAB_DRAWS_PATH}/{draw['id']}")
    assert by_id.status_code == 200
    assert by_id.json()["id"] == draw["id"]

    result_by_id = _get(harness, f"{LAB_RESULTS_PATH}/{row['id']}")
    assert result_by_id.status_code == 200
    assert result_by_id.json() == row  # same joined shape as the listing


def test_get_absent_row_is_404(harness: Harness) -> None:
    for path in _LIST_PATHS:
        response = _get(harness, f"{path}/999")
        assert response.status_code == 404, path


def test_upsert_supersession_visibility(harness: Harness) -> None:
    """A re-imported changed value supersedes; readers see only the successor."""
    panel: list[dict[str, object]] = [
        {"id": 1, "lab_draw_id": 1, "biomarker_id": 1, "value_num": 100.0}
    ]
    _import_panel(harness, [_draw(1, "2024-03-14T13:30:00Z")], panel)
    old = _get(harness, LAB_RESULTS_PATH).json()["items"][0]

    panel[0]["value_num"] = 105.0
    _import_panel(harness, [_draw(1, "2024-03-14T13:30:00Z")], panel, policy="upsert")
    items = _get(harness, LAB_RESULTS_PATH).json()["items"]
    (current,) = items
    assert current["value_num"] == 105.0
    assert current["id"] != old["id"]
    assert _get(harness, f"{LAB_RESULTS_PATH}/{old['id']}").status_code == 404
    assert _get(harness, f"{LAB_RESULTS_PATH}/{current['id']}").status_code == 200


# --------------------------------------------------------------------------
# Page cap and cursor behavior over HTTP
# --------------------------------------------------------------------------


def _import_n_draws(harness: Harness, count: int) -> None:
    _import_panel(
        harness,
        draws=[
            _draw(handle, f"2024-01-{handle:02d}T08:00:00Z")
            for handle in range(1, count + 1)
        ],
        results=[],
    )


def test_page_cap_bounds_default_and_oversized_limits(harness: Harness) -> None:
    _import_n_draws(harness, 5)
    default = _get(harness, LAB_DRAWS_PATH).json()
    assert len(default["items"]) == PAGE_CAP
    assert default["next_cursor"] is not None
    clamped = _get(harness, LAB_DRAWS_PATH, params={"limit": "100"}).json()
    assert len(clamped["items"]) == PAGE_CAP  # clamp, not error (ADR-0053)
    smaller = _get(harness, LAB_DRAWS_PATH, params={"limit": "2"}).json()
    assert len(smaller["items"]) == 2


def test_cursor_walk_to_exhaustion(harness: Harness) -> None:
    _import_n_draws(harness, 5)
    seen: list[str] = []
    params: dict[str, str] = {}
    for _ in range(10):
        page = _get(harness, LAB_DRAWS_PATH, params=params).json()
        seen.extend(row["draw_utc"] for row in page["items"])
        if page["next_cursor"] is None:
            break
        params = {"cursor": page["next_cursor"]}
    assert seen == sorted(seen, reverse=True)  # newest first
    assert len(seen) == 5


def test_order_asc_walks_oldest_first(harness: Harness) -> None:
    _import_n_draws(harness, 4)
    page = _get(harness, LAB_DRAWS_PATH, params={"order": "asc"}).json()
    dates = [row["draw_utc"] for row in page["items"]]
    assert dates == sorted(dates)


def test_invalid_limit_and_cursor_are_422(harness: Harness) -> None:
    assert _get(harness, LAB_DRAWS_PATH, params={"limit": "0"}).status_code == 422
    assert (
        _get(harness, LAB_DRAWS_PATH, params={"cursor": "garbage"}).status_code == 422
    )
    assert (
        _get(harness, LAB_DRAWS_PATH, params={"order": "sideways"}).status_code == 422
    )


def test_cursor_rejected_under_flipped_order(harness: Harness) -> None:
    _import_n_draws(harness, 4)
    first = _get(harness, LAB_DRAWS_PATH).json()
    assert first["next_cursor"] is not None
    flipped = _get(
        harness,
        LAB_DRAWS_PATH,
        params={"cursor": first["next_cursor"], "order": "asc"},
    )
    assert flipped.status_code == 422


# --------------------------------------------------------------------------
# Catalog endpoints
# --------------------------------------------------------------------------


def test_catalog_list_and_get(harness: Harness) -> None:
    labs = _get(harness, LABS_PATH).json()
    assert [row["name"] for row in labs["items"]] == ["LabCorp", "Quest"]
    lab = _get(harness, f"{LABS_PATH}/1")
    assert lab.status_code == 200
    assert lab.json()["name"] == "Quest"

    lipids = _get(harness, BIOMARKERS_PATH, params={"category": "lipids"}).json()
    assert [row["id"] for row in lipids["items"]] == [1]
    marker = _get(harness, f"{BIOMARKERS_PATH}/3")
    assert marker.status_code == 200
    assert marker.json()["category"] is None
