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


def _biomarker_id(harness: Harness, canonical_name: str) -> int:
    """Look up a migration-0004/0005-seeded biomarker's id by name."""
    conn = db.connect(harness.db_path, _key())
    try:
        row = conn.execute(
            "SELECT id FROM biomarkers WHERE canonical_name = ?", (canonical_name,)
        ).fetchone()
        assert row is not None
        return int(row[0])
    finally:
        db.close(conn)


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


def test_range_frameworks_and_framework_ranges_seeded(harness: Harness) -> None:
    # Migration 0005 seeds 3 range_frameworks / 7 framework_ranges rows
    # (ADR-0058 §5) — frameworks/ranges are no longer empty by the time this
    # WI ships, so this asserts the seeded reference data is actually
    # reachable through the same generic list/get surface as any other
    # catalog resource, page cap and pagination included.
    frameworks = _get(harness, RANGE_FRAMEWORKS_PATH).json()
    # PAGE_CAP=3 exactly covers the 3 seeded frameworks, name asc.
    assert [row["name"] for row in frameworks["items"]] == [
        "ada_standards_of_care",
        "aha_cdc_hscrp_risk_strata",
        "nih_medlineplus_lipid_targets",
    ]
    assert frameworks["next_cursor"] is None

    got = _get(harness, f"{RANGE_FRAMEWORKS_PATH}/1")
    assert got.status_code == 200
    body = got.json()
    assert body["name"] == "nih_medlineplus_lipid_targets"
    assert body["source_url"].startswith("https://")  # every seeded row is cited

    # 7 seeded framework_ranges rows, PAGE_CAP=3: the default page clips, and
    # a full cursor walk recovers all 7 exactly once, id asc.
    first_page = _get(harness, FRAMEWORK_RANGES_PATH).json()
    assert len(first_page["items"]) == PAGE_CAP
    assert first_page["next_cursor"] is not None

    ids: list[int] = []
    params: dict[str, str] = {}
    for _ in range(10):
        page = _get(harness, FRAMEWORK_RANGES_PATH, params=params).json()
        ids.extend(int(row["id"]) for row in page["items"])
        if page["next_cursor"] is None:
            break
        params = {"cursor": page["next_cursor"]}
    assert ids == list(range(1, 8))

    assert _get(harness, f"{RANGE_FRAMEWORKS_PATH}/999").status_code == 404
    assert _get(harness, f"{FRAMEWORK_RANGES_PATH}/999").status_code == 404


def test_framework_ranges_filters_over_http(harness: Harness) -> None:
    # Seed a couple of frameworks/ranges directly, alongside migration
    # 0005's own seed (ADR-0058 §5). Framework/range ids are left to
    # autoincrement and captured via lastrowid rather than hardcoded, so
    # this test never collides with however many rows the seed carries;
    # the fixture biomarkers (disjoint from the seeded catalog range,
    # ADR-0055 §6) keep it independent of the seed's own framework_ranges
    # coverage too.
    conn = db.connect(harness.db_path, _key())
    try:
        conn.execute("BEGIN IMMEDIATE")
        fw1_cur = conn.execute(
            "INSERT INTO range_frameworks (name) VALUES ('Test Standard')"
        )
        fw2_cur = conn.execute(
            "INSERT INTO range_frameworks (name) VALUES ('Test Alternative')"
        )
        fw1, fw2 = fw1_cur.lastrowid, fw2_cur.lastrowid
        assert fw1 is not None
        assert fw2 is not None
        r1_cur = conn.execute(
            "INSERT INTO framework_ranges "
            "(framework_id, biomarker_id, range_low, range_high, unit) "
            "VALUES (?, ?, 0, 10, 'mg/dL')",
            (fw1, _FIXTURE_BIOMARKER_1),
        )
        r2_cur = conn.execute(
            "INSERT INTO framework_ranges "
            "(framework_id, biomarker_id, range_low, range_high, unit) "
            "VALUES (?, ?, 0, 5, 'mIU/L')",
            (fw1, _FIXTURE_BIOMARKER_2),
        )
        r3_cur = conn.execute(
            "INSERT INTO framework_ranges "
            "(framework_id, biomarker_id, range_low, range_high, unit) "
            "VALUES (?, ?, 0, 8, 'mg/dL')",
            (fw2, _FIXTURE_BIOMARKER_1),
        )
        r1, r2, r3 = r1_cur.lastrowid, r2_cur.lastrowid, r3_cur.lastrowid
        assert r1 is not None
        assert r2 is not None
        assert r3 is not None
        conn.execute("COMMIT")
    finally:
        db.close(conn)

    by_framework = _get(
        harness, FRAMEWORK_RANGES_PATH, params={"framework_id": str(fw1)}
    ).json()
    assert [row["id"] for row in by_framework["items"]] == [r1, r2]

    by_biomarker = _get(
        harness,
        FRAMEWORK_RANGES_PATH,
        params={"biomarker_id": str(_FIXTURE_BIOMARKER_1)},
    ).json()
    assert [row["id"] for row in by_biomarker["items"]] == [r1, r3]

    got = _get(harness, f"{FRAMEWORK_RANGES_PATH}/{r1}")
    assert got.status_code == 200
    assert got.json()["unit"] == "mg/dL"


# --------------------------------------------------------------------------
# Range-comparison enrichment (?framework=, ADR-0005/ADR-0058)
#
# Reuses migration 0005's own generic, defensibly-sourced seed data (no
# personal health data) rather than inventing synthetic frameworks:
# `nih_medlineplus_lipid_targets` covers Total Cholesterol (range_high=200,
# range_low NULL), `ada_standards_of_care` covers Glucose ([70, 99] mg/dL),
# and `aha_cdc_hscrp_risk_strata` covers hs-CRP (range_high=1.0 mg/L).
# --------------------------------------------------------------------------


def test_framework_enrichment_present_only_with_param(harness: Harness) -> None:
    total_chol = _biomarker_id(harness, "Total Cholesterol")
    _import_panel(
        harness,
        draws=[_draw(1, "2024-03-14T13:30:00Z")],
        results=[
            {
                "id": 1,
                "lab_draw_id": 1,
                "biomarker_id": total_chol,
                "value_num": 180.0,
                "unit": "mg/dL",
            }
        ],
    )

    bare = _get(harness, LAB_RESULTS_PATH).json()
    (row,) = bare["items"]
    assert "range_comparison" not in row  # opt-in only; ADR-0053 shape unshifted

    enriched = _get(
        harness,
        LAB_RESULTS_PATH,
        params={"framework": "nih_medlineplus_lipid_targets"},
    ).json()
    (enriched_row,) = enriched["items"]
    assert enriched_row["range_comparison"]["flag"] == "in_range"
    without_comparison = {
        k: v for k, v in enriched_row.items() if k != "range_comparison"
    }
    assert without_comparison == row

    bare_by_id = _get(harness, f"{LAB_RESULTS_PATH}/{row['id']}")
    assert "range_comparison" not in bare_by_id.json()
    enriched_by_id = _get(
        harness,
        f"{LAB_RESULTS_PATH}/{row['id']}",
        params={"framework": "nih_medlineplus_lipid_targets"},
    )
    assert enriched_by_id.json()["range_comparison"]["flag"] == "in_range"


def test_framework_unknown_is_422_and_case_insensitive_match_works(
    harness: Harness,
) -> None:
    total_chol = _biomarker_id(harness, "Total Cholesterol")
    _import_panel(
        harness,
        draws=[_draw(1, "2024-03-14T13:30:00Z")],
        results=[
            {
                "id": 1,
                "lab_draw_id": 1,
                "biomarker_id": total_chol,
                "value_num": 180.0,
                "unit": "mg/dL",
            }
        ],
    )

    for name in (
        "nih_medlineplus_lipid_targets",
        "NIH_MEDLINEPLUS_LIPID_TARGETS",
        "Nih_MedlinePlus_Lipid_Targets",
    ):
        response = _get(harness, LAB_RESULTS_PATH, params={"framework": name})
        assert response.status_code == 200
        assert response.json()["items"][0]["range_comparison"]["flag"] == "in_range"

    unknown_list = _get(
        harness, LAB_RESULTS_PATH, params={"framework": "not-a-real-framework"}
    )
    assert unknown_list.status_code == 422

    unknown_get = _get(
        harness,
        f"{LAB_RESULTS_PATH}/1",
        params={"framework": "not-a-real-framework"},
    )
    assert unknown_get.status_code == 422

    # A malformed cursor and an unknown framework are both 422s (ADR-0053 /
    # ADR-0058), but for distinct reasons — the endpoint still validates the
    # framework even when the cursor itself is fine.
    assert (
        _get(harness, LAB_RESULTS_PATH, params={"cursor": "garbage"}).status_code == 422
    )


def test_framework_cursor_is_a_projection_not_a_filter(harness: Harness) -> None:
    total_chol = _biomarker_id(harness, "Total Cholesterol")
    _import_panel(
        harness,
        draws=[_draw(handle, f"2024-0{handle}-01T08:00:00Z") for handle in (1, 2, 3)],
        results=[
            {
                "id": handle,
                "lab_draw_id": handle,
                "biomarker_id": total_chol,
                "value_num": 180.0,
                "unit": "mg/dL",
            }
            for handle in (1, 2, 3)
        ],
    )
    framework = "nih_medlineplus_lipid_targets"

    # A cursor minted WITHOUT the param still walks correctly WITH it.
    page1 = _get(harness, LAB_RESULTS_PATH, params={"limit": "1"}).json()
    assert "range_comparison" not in page1["items"][0]
    assert page1["next_cursor"] is not None
    page2 = _get(
        harness,
        LAB_RESULTS_PATH,
        params={"limit": "1", "cursor": page1["next_cursor"], "framework": framework},
    ).json()
    assert page2["items"][0]["range_comparison"]["flag"] == "in_range"

    # And the reverse: a cursor minted WITH the param still walks correctly
    # WITHOUT it, landing on the exact same next row.
    page1b = _get(
        harness, LAB_RESULTS_PATH, params={"limit": "1", "framework": framework}
    ).json()
    assert page1b["items"][0]["id"] == page1["items"][0]["id"]
    assert page1b["next_cursor"] is not None
    page2b = _get(
        harness,
        LAB_RESULTS_PATH,
        params={"limit": "1", "cursor": page1b["next_cursor"]},
    ).json()
    assert "range_comparison" not in page2b["items"][0]
    assert page2b["items"][0]["id"] == page2["items"][0]["id"]

    # A full walk under the param visits every row exactly once.
    ids: list[int] = []
    params: dict[str, str] = {"limit": "1", "framework": framework}
    for _ in range(10):
        page = _get(harness, LAB_RESULTS_PATH, params=params).json()
        ids.extend(int(row["id"]) for row in page["items"])
        if page["next_cursor"] is None:
            break
        params = {"limit": "1", "framework": framework, "cursor": page["next_cursor"]}
    assert len(ids) == 3
    assert len(set(ids)) == 3


def test_framework_flags_via_http(harness: Harness) -> None:
    glucose = _biomarker_id(harness, "Glucose")  # ada_standards_of_care: [70, 99] mg/dL
    _import_panel(
        harness,
        draws=[
            _draw(handle, f"2024-0{handle}-01T08:00:00Z") for handle in (1, 2, 3, 4)
        ],
        results=[
            {
                "id": 1,
                "lab_draw_id": 1,
                "biomarker_id": glucose,
                "value_num": 90.0,
                "unit": "mg/dL",
            },
            {
                "id": 2,
                "lab_draw_id": 2,
                "biomarker_id": glucose,
                "value_num": 150.0,
                "unit": "mg/dL",
            },
            {
                "id": 3,
                "lab_draw_id": 3,
                "biomarker_id": glucose,
                "value_num": 50.0,
                "unit": "mg/dL",
            },
            {  # no seeded range for this biomarker, in any framework
                "id": 4,
                "lab_draw_id": 4,
                "biomarker_id": _FIXTURE_BIOMARKER_1,
                "value_num": 1.0,
                "unit": "mg/dL",
            },
        ],
    )

    # PAGE_CAP=3 clips the default page; walk to exhaustion to see all four.
    flags: dict[int, str] = {}
    params: dict[str, str] = {"framework": "ada_standards_of_care"}
    for _ in range(10):
        page = _get(harness, LAB_RESULTS_PATH, params=params).json()
        for row in page["items"]:
            flags[int(row["id"])] = row["range_comparison"]["flag"]
        if page["next_cursor"] is None:
            break
        params = {"framework": "ada_standards_of_care", "cursor": page["next_cursor"]}

    assert flags[1] == "in_range"
    assert flags[2] == "above"
    assert flags[3] == "below"
    assert flags[4] == "no_range"


def test_framework_censored_value_fidelity_http(harness: Harness) -> None:
    hscrp = _biomarker_id(harness, "hs-CRP")  # aha_cdc_hscrp_risk_strata: < 1.0 mg/L
    _import_panel(
        harness,
        draws=[_draw(1, "2024-03-14T13:30:00Z")],
        results=[
            {
                "id": 1,
                "lab_draw_id": 1,
                "biomarker_id": hscrp,
                "value_num": 0.1,
                "comparator": "<",
                "unit": "mg/L",
            }
        ],
    )
    page = _get(
        harness, LAB_RESULTS_PATH, params={"framework": "aha_cdc_hscrp_risk_strata"}
    ).json()
    (row,) = page["items"]
    assert (row["value_num"], row["comparator"], row["value_text"]) == (0.1, "<", None)
    assert row["display"] == "<0.1"
    comparison = row["range_comparison"]
    assert comparison["flag"] == "in_range"
    assert comparison["unit"] == "mg/L"
    assert comparison["range_high"] == 1.0


def _framework_id(harness: Harness, name: str) -> int:
    conn = db.connect(harness.db_path, _key())
    try:
        row = conn.execute(
            "SELECT id FROM range_frameworks WHERE name = ?", (name,)
        ).fetchone()
        assert row is not None
        return int(row[0])
    finally:
        db.close(conn)


def test_framework_not_comparable_qualitative_result_http(harness: Harness) -> None:
    # Total Cholesterol has a real numeric target under
    # nih_medlineplus_lipid_targets (range_high=200, floor NULL): a
    # qualitative result here can only reach `not_comparable` via
    # compare()'s value_num-IS-NULL step (ADR-0030), never the `no_range`
    # short-circuit, so this proves the HTTP-driven read path (api_read.py ->
    # reads.py -> ranges.compare) carries the real (value_num, value_text)
    # pair through, not just the pure function in isolation.
    total_chol = _biomarker_id(harness, "Total Cholesterol")
    _import_panel(
        harness,
        draws=[_draw(1, "2024-03-14T13:30:00Z")],
        results=[
            {
                "id": 1,
                "lab_draw_id": 1,
                "biomarker_id": total_chol,
                "value_text": "Not Detected",
                "unit": "mg/dL",
            }
        ],
    )

    page = _get(
        harness,
        LAB_RESULTS_PATH,
        params={"framework": "nih_medlineplus_lipid_targets"},
    )
    assert page.status_code == 200
    (row,) = page.json()["items"]
    assert (row["value_num"], row["comparator"], row["value_text"]) == (
        None,
        None,
        "Not Detected",
    )
    assert row["display"] == "Not Detected"
    comparison = row["range_comparison"]
    assert comparison["flag"] == "not_comparable"
    assert comparison["reason"] is not None
    assert "qualitative" in comparison["reason"]

    by_id = _get(
        harness,
        f"{LAB_RESULTS_PATH}/{row['id']}",
        params={"framework": "nih_medlineplus_lipid_targets"},
    )
    assert by_id.status_code == 200
    assert by_id.json()["range_comparison"]["flag"] == "not_comparable"


def test_framework_not_comparable_range_text_only_target_http(
    harness: Harness,
) -> None:
    # The seed (migration 0005) has no range_text-only row to drive this
    # through over HTTP either; ADR-0005's CHECK permits one (range_low AND
    # range_high both NULL, range_text set), so this inserts one directly,
    # the same way test_framework_ranges_filters_over_http seeds its own
    # rows. Vitamin D 25-OH is real, seeded, and left uncovered by every
    # migration-0005 framework range, so this is the only row that can
    # resolve for the pair.
    nih = _framework_id(harness, "nih_medlineplus_lipid_targets")
    vit_d = _biomarker_id(harness, "Vitamin D 25-OH")
    conn = db.connect(harness.db_path, _key())
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO framework_ranges "
            "(framework_id, biomarker_id, range_low, range_high, unit, range_text) "
            "VALUES (?, ?, NULL, NULL, ?, ?)",
            (nih, vit_d, "ng/mL", "See clinician interpretation"),
        )
        conn.execute("COMMIT")
    finally:
        db.close(conn)

    _import_panel(
        harness,
        draws=[_draw(1, "2024-03-14T13:30:00Z")],
        results=[
            {
                "id": 1,
                "lab_draw_id": 1,
                "biomarker_id": vit_d,
                "value_num": 35.0,
                "unit": "ng/mL",
            }
        ],
    )

    page = _get(
        harness,
        LAB_RESULTS_PATH,
        params={"framework": "nih_medlineplus_lipid_targets"},
    )
    assert page.status_code == 200
    (row,) = page.json()["items"]
    comparison = row["range_comparison"]
    assert comparison["flag"] == "not_comparable"
    assert comparison["reason"] is not None
    assert comparison["range_low"] is None
    assert comparison["range_high"] is None
    assert comparison["range_text"] == "See clinician interpretation"


def test_framework_error_unreconcilable_units_returns_full_page_http(
    harness: Harness,
) -> None:
    # hs-CRP (aha_cdc_hscrp_risk_strata: <1.0 mg/L) has no seeded molar_mass
    # and canonical_unit mg/L. A result reported in mmol/L -- a genuine
    # mass<->substance molar-differing unit -- cannot be normalized without
    # one, so compare() step 4 catches MissingMolarContextError into a loud
    # `error` flag. A second hs-CRP result carries no unit at all, hitting
    # the sibling step-4 `error` branch. Critically: the request must still
    # return 200 with a full page -- `error` is loud but not fatal
    # (ADR-0058 §3) -- and a third, ordinary hs-CRP result on the same page
    # must keep its correct flag, proving one bad row never blanks or drops
    # the rest of the page.
    hscrp = _biomarker_id(harness, "hs-CRP")
    _import_panel(
        harness,
        draws=[_draw(handle, f"2024-0{handle}-01T08:00:00Z") for handle in (1, 2, 3)],
        results=[
            {
                "id": 1,
                "lab_draw_id": 1,
                "biomarker_id": hscrp,
                "value_num": 0.5,
                "unit": "mg/L",
            },
            {
                "id": 2,
                "lab_draw_id": 2,
                "biomarker_id": hscrp,
                "value_num": 0.01,
                "unit": "mmol/L",
            },
            {
                "id": 3,
                "lab_draw_id": 3,
                "biomarker_id": hscrp,
                "value_num": 0.5,
            },  # no "unit" key at all -> NULL
        ],
    )

    response = _get(
        harness, LAB_RESULTS_PATH, params={"framework": "aha_cdc_hscrp_risk_strata"}
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 3  # a bad row is flagged, never dropped

    # The import payload's "id" is a batch-local handle (imports.py), not the
    # server-assigned row id, so rows are identified by their own
    # (value_num, unit) pair rather than assuming handle == id.
    by_key = {
        (row["value_num"], row["unit"]): row["range_comparison"]
        for row in body["items"]
    }
    assert by_key[(0.5, "mg/L")]["flag"] == "in_range"

    mismatch = by_key[(0.01, "mmol/L")]
    assert mismatch["flag"] == "error"
    assert mismatch["reason"] is not None
    assert "hs-CRP" in mismatch["reason"]
    assert "molar_mass" in mismatch["reason"]

    no_unit = by_key[(0.5, None)]
    assert no_unit["flag"] == "error"
    assert no_unit["reason"] is not None
    assert "unit" in no_unit["reason"]
