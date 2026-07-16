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
    CATEGORIES_PATH,
    FRAMEWORK_RANGES_PATH,
    LAB_DRAWS_PATH,
    LAB_RESULTS_PATH,
    LABS_PATH,
    RANGE_FRAMEWORKS_PATH,
)
from healthspan.config import Config
from healthspan.kdf import DbKey
from healthspan.locking import InstanceLock
from healthspan.pool import ConnectionPool
from healthspan.service import create_app
from healthspan.service_runtime import ServiceRuntime

PAGE_CAP = 3

# Migration 0004 seeds ~64 starter biomarkers (ids 1-64, ADR-0055 §6) and the
# common labs (ids 1-4), so this fixture's own rows use a high id range that
# cannot collide with the seed, now or as the seed catalog grows. 'allergy'
# and 'body_composition' are two of the 19 seeded categories the starter
# catalog leaves empty, so filtering by them sees only these fixture rows.
_FIXTURE_BIOMARKER_1 = 1001  # category 'allergy'
_FIXTURE_BIOMARKER_2 = 1002  # category 'body_composition'
_FIXTURE_BIOMARKER_3 = 1003  # reserved not_assigned category (id 0)


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
        # Quest (id 1) and LabCorp (id 2) are seeded by migration 0004 itself
        # (ADR-0055 §6) — no need to (re-)insert them here.
        for biomarker_id, category_name in (
            (_FIXTURE_BIOMARKER_1, "allergy"),
            (_FIXTURE_BIOMARKER_2, "body_composition"),
        ):
            setup.execute(
                "INSERT INTO biomarkers (id, canonical_name, category_id) "
                "VALUES (?, ?, (SELECT id FROM categories WHERE name = ?))",
                (biomarker_id, f"Biomarker {biomarker_id}", category_name),
            )
        setup.execute(
            "INSERT INTO biomarkers (id, canonical_name) VALUES (?, ?)",
            (_FIXTURE_BIOMARKER_3, f"Biomarker {_FIXTURE_BIOMARKER_3}"),
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

_LIST_PATHS = [
    LAB_DRAWS_PATH,
    LAB_RESULTS_PATH,
    LABS_PATH,
    BIOMARKERS_PATH,
    CATEGORIES_PATH,
    RANGE_FRAMEWORKS_PATH,
    FRAMEWORK_RANGES_PATH,
]


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
    # Migration 0004 seeds all four common labs (ADR-0055 §6); PAGE_CAP=3
    # clips the default page to the first three, name asc.
    labs = _get(harness, LABS_PATH).json()
    assert [row["name"] for row in labs["items"]] == [
        "Function Health (LabCorp)",
        "Function Health (Quest)",
        "LabCorp",
    ]
    lab = _get(harness, f"{LABS_PATH}/1")
    assert lab.status_code == 200
    assert lab.json()["name"] == "Quest"

    # 'allergy' is one of the 19 seeded categories with no starter-catalog
    # biomarker (ADR-0055 §6), so the fixture's own row is the only match.
    allergy = _get(harness, BIOMARKERS_PATH, params={"category": "allergy"}).json()
    assert [row["id"] for row in allergy["items"]] == [_FIXTURE_BIOMARKER_1]
    marker = _get(harness, f"{BIOMARKERS_PATH}/{_FIXTURE_BIOMARKER_3}")
    assert marker.status_code == 200
    # Unassigned -> the reserved not_assigned row (id 0), by name (ADR-0055 §2).
    assert marker.json()["category"] == "not_assigned"
    assert marker.json()["category_id"] == 0


def test_biomarker_category_filter_case_insensitive(harness: Harness) -> None:
    title_case = _get(
        harness, BIOMARKERS_PATH, params={"category": "Body_Composition"}
    ).json()
    lower_case = _get(
        harness, BIOMARKERS_PATH, params={"category": "body_composition"}
    ).json()
    assert [row["id"] for row in title_case["items"]] == [_FIXTURE_BIOMARKER_2]
    assert [row["id"] for row in lower_case["items"]] == [_FIXTURE_BIOMARKER_2]

    unknown = _get(
        harness, BIOMARKERS_PATH, params={"category": "not-a-real-category"}
    ).json()
    assert unknown["items"] == []
    assert unknown["next_cursor"] is None


# --------------------------------------------------------------------------
# Reference-data endpoints (categories, range-frameworks, framework-ranges)
# --------------------------------------------------------------------------


def test_categories_list_and_get(harness: Harness) -> None:
    page = _get(harness, CATEGORIES_PATH).json()
    # PAGE_CAP=3: alphabetically first three of the migration 0004 seed.
    assert [row["name"] for row in page["items"]] == [
        "allergy",
        "autoimmunity",
        "body_composition",
    ]
    assert page["next_cursor"] is not None
    reserved = _get(harness, f"{CATEGORIES_PATH}/0")
    assert reserved.status_code == 200
    assert reserved.json()["name"] == "not_assigned"


def test_categories_pagination_walk_to_exhaustion(harness: Harness) -> None:
    seen: list[str] = []
    params: dict[str, str] = {}
    for _ in range(20):
        page = _get(harness, CATEGORIES_PATH, params=params).json()
        seen.extend(row["name"] for row in page["items"])
        if page["next_cursor"] is None:
            break
        params = {"cursor": page["next_cursor"]}
    assert seen == sorted(seen)
    assert len(seen) == 20  # reserved + 19 seeded categories


def test_range_frameworks_and_framework_ranges_empty_until_wi3(
    harness: Harness,
) -> None:
    # Migration 0004 seeds no frameworks/ranges (deferred to WI-3); the
    # endpoints ship now and answer empty pages rather than 404.
    frameworks = _get(harness, RANGE_FRAMEWORKS_PATH).json()
    assert frameworks == {"items": [], "next_cursor": None}
    ranges = _get(harness, FRAMEWORK_RANGES_PATH).json()
    assert ranges == {"items": [], "next_cursor": None}
    assert _get(harness, f"{RANGE_FRAMEWORKS_PATH}/1").status_code == 404
    assert _get(harness, f"{FRAMEWORK_RANGES_PATH}/1").status_code == 404


def test_framework_ranges_filters_over_http(harness: Harness) -> None:
    # Seed a couple of frameworks/ranges directly (migration seeding of these
    # tables is deferred to WI-3).
    conn = db.connect(harness.db_path, _key())
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO range_frameworks (id, name) VALUES (1, 'Lab Standard')"
        )
        conn.execute("INSERT INTO range_frameworks (id, name) VALUES (2, 'Attia')")
        conn.execute(
            "INSERT INTO framework_ranges "
            "(id, framework_id, biomarker_id, range_low, range_high, unit) "
            "VALUES (1, 1, 1, 0, 10, 'mg/dL')"
        )
        conn.execute(
            "INSERT INTO framework_ranges "
            "(id, framework_id, biomarker_id, range_low, range_high, unit) "
            "VALUES (2, 1, 2, 0, 5, 'mIU/L')"
        )
        conn.execute(
            "INSERT INTO framework_ranges "
            "(id, framework_id, biomarker_id, range_low, range_high, unit) "
            "VALUES (3, 2, 1, 0, 8, 'mg/dL')"
        )
        conn.execute("COMMIT")
    finally:
        db.close(conn)

    by_framework = _get(
        harness, FRAMEWORK_RANGES_PATH, params={"framework_id": "1"}
    ).json()
    assert [row["id"] for row in by_framework["items"]] == [1, 2]

    by_biomarker = _get(
        harness, FRAMEWORK_RANGES_PATH, params={"biomarker_id": "1"}
    ).json()
    assert [row["id"] for row in by_biomarker["items"]] == [1, 3]

    got = _get(harness, f"{FRAMEWORK_RANGES_PATH}/1")
    assert got.status_code == 200
    assert got.json()["unit"] == "mg/dL"
