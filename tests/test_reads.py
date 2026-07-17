"""Read/query layer (ADR-0053): keyset pagination, cursors, value fidelity.

The pagination partition property (testing-strategy.md): walking a listing
to exhaustion — any page size, either direction — yields exactly the
current rows, in the resource's deterministic order, each exactly once.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlcipher3
from hypothesis import given, settings
from hypothesis import strategies as st

from healthspan import db, migrate, reads
from healthspan.kdf import DbKey


def _key() -> DbKey:
    return DbKey(bytearray(range(1, 33)))


# Migration 0004 seeds ~64 starter biomarkers (ids 1-64, ADR-0055 §6) and the
# common labs (ids 1-4), so this fixture's own rows use a high id range that
# cannot collide with the seed, now or as the seed catalog grows. 'allergy'
# and 'body_composition' are two of the 19 seeded categories the starter
# catalog leaves empty, so filtering by them below sees only these fixture
# rows, never a seeded biomarker.
_FIXTURE_BIOMARKER_1 = 1001  # category 'allergy'
_FIXTURE_BIOMARKER_2 = 1002  # category 'body_composition'
_FIXTURE_BIOMARKER_3 = 1003  # reserved not_assigned category (id 0)


def _build_db(directory: Path) -> sqlcipher3.Connection:
    path = directory / "healthspan.db"
    db.provision(path, _key())
    migrate.migrate_database(path, _key())
    conn = db.connect(path, _key())
    conn.execute("BEGIN IMMEDIATE")
    # Quest (id 1) and LabCorp (id 2) are seeded by migration 0004 itself
    # (ADR-0055 §6), ahead of Function Health (Quest)/(LabCorp) — no need to
    # (re-)insert them here.
    for biomarker_id, category_name in (
        (_FIXTURE_BIOMARKER_1, "allergy"),
        (_FIXTURE_BIOMARKER_2, "body_composition"),
    ):
        conn.execute(
            "INSERT INTO biomarkers (id, canonical_name, category_id) "
            "VALUES (?, ?, (SELECT id FROM categories WHERE name = ?))",
            (biomarker_id, f"Biomarker {biomarker_id}", category_name),
        )
    conn.execute(
        "INSERT INTO biomarkers (id, canonical_name) VALUES (?, ?)",
        (_FIXTURE_BIOMARKER_3, f"Biomarker {_FIXTURE_BIOMARKER_3}"),
    )
    conn.execute("COMMIT")
    return conn


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlcipher3.Connection]:
    connection = _build_db(tmp_path)
    try:
        yield connection
    finally:
        db.close(connection)


def _insert_draw(conn: sqlcipher3.Connection, draw_utc: str, lab_id: int = 1) -> int:
    conn.execute("BEGIN IMMEDIATE")
    cur = conn.execute(
        "INSERT INTO lab_draws (lab_id, draw_utc) VALUES (?, ?)",
        (lab_id, draw_utc),
    )
    conn.execute("COMMIT")
    assert cur.lastrowid is not None
    return cur.lastrowid


def _insert_result(
    conn: sqlcipher3.Connection,
    lab_draw_id: int,
    biomarker_id: int,
    *,
    value_num: float | None = 100.0,
    comparator: str | None = None,
    value_text: str | None = None,
    unit: str | None = "mg/dL",
    reference_low: float | None = None,
    reference_high: float | None = None,
    reference_text: str | None = None,
) -> int:
    conn.execute("BEGIN IMMEDIATE")
    cur = conn.execute(
        "INSERT INTO lab_results (lab_draw_id, biomarker_id, value_num, "
        "comparator, value_text, unit, reference_low, reference_high, "
        "reference_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            lab_draw_id,
            biomarker_id,
            value_num,
            comparator,
            value_text,
            unit,
            reference_low,
            reference_high,
            reference_text,
        ),
    )
    conn.execute("COMMIT")
    assert cur.lastrowid is not None
    return cur.lastrowid


# --------------------------------------------------------------------------
# Cursor encoding
# --------------------------------------------------------------------------


def test_cursor_round_trip() -> None:
    token = reads.encode_cursor("desc", "2024-03-14T13:30:00Z", 42)
    assert reads.decode_cursor(token, "desc") == ("2024-03-14T13:30:00Z", 42)


@pytest.mark.parametrize(
    "raw",
    [
        "not base64!!",
        "aGVsbG8=",  # base64 of non-JSON
        reads.encode_cursor("desc", "x", 1)[:-4],  # truncated
        "",
    ],
)
def test_malformed_cursor_rejected(raw: str) -> None:
    with pytest.raises(reads.CursorError):
        reads.decode_cursor(raw, "desc")


def test_cursor_direction_bound() -> None:
    token = reads.encode_cursor("desc", "2024-01-01T00:00:00Z", 7)
    with pytest.raises(reads.CursorError, match="order"):
        reads.decode_cursor(token, "asc")


def test_cursor_key_types_enforced() -> None:
    import base64
    import json

    for key in ([1, 2], ["x", "y"], ["x", True], ["x"], "x", None):
        payload = json.dumps({"v": 1, "o": "desc", "k": key})
        token = base64.urlsafe_b64encode(payload.encode()).decode()
        with pytest.raises(reads.CursorError):
            reads.decode_cursor(token, "desc")


def test_cursor_version_enforced() -> None:
    import base64
    import json

    payload = json.dumps({"v": 2, "o": "desc", "k": ["x", 1]})
    token = base64.urlsafe_b64encode(payload.encode()).decode()
    with pytest.raises(reads.CursorError):
        reads.decode_cursor(token, "desc")


# --------------------------------------------------------------------------
# Value-fidelity display string (ADR-0030/0053)
# --------------------------------------------------------------------------


def test_display_preserves_comparator() -> None:
    assert reads.display_value(0.1, "<", None) == "<0.1"
    assert reads.display_value(5.0, ">=", None) == ">=5"


def test_display_exact_and_text_values() -> None:
    assert reads.display_value(98.6, None, None) == "98.6"
    assert reads.display_value(None, None, "positive") == "positive"
    # Numeric wins when both forms are present; the triple still carries both.
    assert reads.display_value(1.2, None, "borderline") == "1.2"


def test_display_integral_without_trailing_zero() -> None:
    assert reads.display_value(100.0, None, None) == "100"
    assert reads.display_value(-3.0, "<", None) == "<-3"


# --------------------------------------------------------------------------
# Listing: order, filters, supersession visibility
# --------------------------------------------------------------------------


def test_draws_newest_first_with_id_tiebreak(conn: sqlcipher3.Connection) -> None:
    a = _insert_draw(conn, "2024-01-01T08:00:00Z")
    b = _insert_draw(conn, "2024-06-01T08:00:00Z")
    # Same instant as b at another lab (the 0003 natural key permits ties
    # only across labs): the id tiebreak decides.
    c = _insert_draw(conn, "2024-06-01T08:00:00Z", lab_id=2)
    page = reads.list_lab_draws(conn, limit=10)
    assert [row["id"] for row in page.items] == [c, b, a]
    assert page.next_cursor is None


def test_draw_filters(conn: sqlcipher3.Connection) -> None:
    early = _insert_draw(conn, "2023-01-01T08:00:00Z")
    mid = _insert_draw(conn, "2024-01-01T08:00:00Z", lab_id=2)
    late = _insert_draw(conn, "2025-01-01T08:00:00Z")
    by_lab = reads.list_lab_draws(conn, lab_id=2, limit=10)
    assert [row["id"] for row in by_lab.items] == [mid]
    window = reads.list_lab_draws(
        conn, draw_from="2023-06-01", draw_to="2024-06-01", limit=10
    )
    assert [row["id"] for row in window.items] == [mid]
    since = reads.list_lab_draws(conn, draw_from="2024-06-01", limit=10)
    assert [row["id"] for row in since.items] == [late]
    assert early not in [row["id"] for row in since.items]


def test_results_embed_draw_context_and_display(
    conn: sqlcipher3.Connection,
) -> None:
    draw = _insert_draw(conn, "2024-03-14T13:30:00Z", lab_id=2)
    result = _insert_result(
        conn,
        draw,
        1,
        value_num=0.1,
        comparator="<",
        unit="ng/mL",
        reference_low=0.5,
        reference_high=4.5,
        reference_text="see note",
    )
    page = reads.list_lab_results(conn, limit=10)
    (row,) = page.items
    assert row["draw_utc"] == "2024-03-14T13:30:00Z"
    assert row["lab_id"] == 2
    assert row["display"] == "<0.1"
    assert (row["value_num"], row["comparator"], row["value_text"]) == (0.1, "<", None)
    assert row["unit"] == "ng/mL"
    # The lab's own reference range travels with the row (ADR-0053 §4).
    assert (row["reference_low"], row["reference_high"]) == (0.5, 4.5)
    assert row["reference_text"] == "see note"
    assert "superseded_by" not in row
    # Get-by-id serializes through the same joined shape as the listing.
    by_id = reads.get_lab_result(conn, result)
    assert by_id == row


def test_result_filters(conn: sqlcipher3.Connection) -> None:
    d1 = _insert_draw(conn, "2024-01-01T08:00:00Z", lab_id=1)
    d2 = _insert_draw(conn, "2024-06-01T08:00:00Z", lab_id=2)
    r1 = _insert_result(conn, d1, _FIXTURE_BIOMARKER_1)
    r2 = _insert_result(conn, d1, _FIXTURE_BIOMARKER_2)
    r3 = _insert_result(conn, d2, _FIXTURE_BIOMARKER_1)
    by_biomarker = reads.list_lab_results(
        conn, biomarker_id=_FIXTURE_BIOMARKER_1, limit=10
    )
    assert [row["id"] for row in by_biomarker.items] == [r3, r1]
    by_draw = reads.list_lab_results(conn, lab_draw_id=d1, limit=10)
    assert {row["id"] for row in by_draw.items} == {r1, r2}
    by_lab = reads.list_lab_results(conn, lab_id=2, limit=10)
    assert [row["id"] for row in by_lab.items] == [r3]
    windowed = reads.list_lab_results(
        conn, biomarker_id=_FIXTURE_BIOMARKER_1, draw_to="2024-03-01", limit=10
    )
    assert [row["id"] for row in windowed.items] == [r1]


def test_superseded_rows_invisible(conn: sqlcipher3.Connection) -> None:
    draw = _insert_draw(conn, "2024-03-14T13:30:00Z")
    old = _insert_result(conn, draw, _FIXTURE_BIOMARKER_1, value_num=100.0)
    new = _insert_result(conn, draw, _FIXTURE_BIOMARKER_2, value_num=105.0)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE lab_results SET superseded_by = ? WHERE id = ?", (new, old))
    conn.execute("COMMIT")
    page = reads.list_lab_results(conn, limit=10)
    assert [row["id"] for row in page.items] == [new]
    assert reads.get_lab_result(conn, old) is None
    got = reads.get_lab_result(conn, new)
    assert got is not None
    assert got["id"] == new


def test_get_absent_returns_none(conn: sqlcipher3.Connection) -> None:
    assert reads.get_lab_draw(conn, 999) is None
    assert reads.get_lab_result(conn, 999) is None
    assert reads.get_lab(conn, 999) is None
    assert reads.get_biomarker(conn, 999) is None
    assert reads.get_category(conn, 999) is None
    assert reads.get_range_framework(conn, 999) is None
    assert reads.get_framework_range(conn, 999) is None


def test_catalog_reads(conn: sqlcipher3.Connection) -> None:
    # Migration 0004 seeds all four common labs (ADR-0055 §6); name asc.
    labs = reads.list_labs(conn, limit=10)
    assert [row["name"] for row in labs.items] == [
        "Function Health (LabCorp)",
        "Function Health (Quest)",
        "LabCorp",
        "Quest",
    ]
    lab = reads.get_lab(conn, 1)
    assert lab is not None
    assert lab["name"] == "Quest"
    # 'allergy' is one of the 19 seeded categories with no starter-catalog
    # biomarker (ADR-0055 §6), so the fixture's own row is the only match.
    allergy = reads.list_biomarkers(conn, category="allergy", limit=10)
    assert [row["id"] for row in allergy.items] == [_FIXTURE_BIOMARKER_1]
    marker = reads.get_biomarker(conn, _FIXTURE_BIOMARKER_3)
    assert marker is not None
    # Unassigned in the fixture -> the reserved not_assigned row (id 0),
    # surfaced by name, not a null category (ADR-0055 §2).
    assert marker["category"] == "not_assigned"
    assert marker["category_id"] == 0


def test_category_filter_is_case_insensitive(conn: sqlcipher3.Connection) -> None:
    lower = reads.list_biomarkers(conn, category="body_composition", limit=10)
    mixed = reads.list_biomarkers(conn, category="Body_Composition", limit=10)
    assert [row["id"] for row in lower.items] == [_FIXTURE_BIOMARKER_2]
    assert [row["id"] for row in mixed.items] == [_FIXTURE_BIOMARKER_2]


def test_category_filter_unknown_name_yields_empty_page(
    conn: sqlcipher3.Connection,
) -> None:
    page = reads.list_biomarkers(conn, category="not-a-real-category", limit=10)
    assert page.items == []
    assert page.next_cursor is None


def test_catalog_cursor_walk(conn: sqlcipher3.Connection) -> None:
    """Catalog listings paginate by name through the same keyset machinery."""
    conn.execute("BEGIN IMMEDIATE")
    for biomarker_id in range(9001, 9007):
        conn.execute(
            "INSERT INTO biomarkers (id, canonical_name) VALUES (?, ?)",
            (biomarker_id, f"Biomarker {biomarker_id}"),
        )
    conn.execute("COMMIT")
    row = conn.execute("SELECT count(*) FROM biomarkers").fetchone()
    assert row is not None
    total = int(row[0])
    names: list[str] = []
    cursor: str | None = None
    for _ in range(200):  # bounded generously: the seed catalog plus 6 inserted
        page = reads.list_biomarkers(conn, limit=5, cursor=cursor)
        assert len(page.items) <= 5
        names.extend(str(row_item["canonical_name"]) for row_item in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert names == sorted(names)
    # The starter catalog (migration 0004) + the 3 _build_db fixture rows +
    # the 6 inserted here, each exactly once.
    assert len(names) == total


# --------------------------------------------------------------------------
# Reference-data resources (ADR-0055/ADR-0057): categories, range_frameworks,
# framework_ranges. Migration 0004 seeds the reserved row plus 19 categories;
# frameworks/ranges are deferred to WI-3, so tests insert a couple directly.
# --------------------------------------------------------------------------


def test_categories_list_and_get(conn: sqlcipher3.Connection) -> None:
    page = reads.list_categories(conn, limit=3)
    assert [row["name"] for row in page.items] == [
        "allergy",
        "autoimmunity",
        "body_composition",
    ]
    reserved = reads.get_category(conn, 0)
    assert reserved is not None
    assert reserved["name"] == "not_assigned"


def test_categories_pagination_partition(conn: sqlcipher3.Connection) -> None:
    all_names = [row["name"] for row in reads.list_categories(conn, limit=100).items]
    names: list[str] = []
    cursor: str | None = None
    for _ in range(50):
        page = reads.list_categories(conn, limit=3, cursor=cursor)
        assert len(page.items) <= 3
        names.extend(str(row["name"]) for row in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert names == all_names
    assert names == sorted(names)


def _clear_seeded_reference_ranges(conn: sqlcipher3.Connection) -> None:
    """Wipe migration 0005's seeded range_frameworks/framework_ranges rows.

    The three tests below assert exact ids and orderings over rows they
    fully control (including a pagination-partition walk, which must not
    silently start depending on how many rows the seed happens to carry —
    ADR-0058 §5 seeded 3 frameworks / ~7 ranges). Clearing first, rather
    than picking ids past the seed's range, keeps each test's own row set
    exactly what it inserts, independent of future seed growth.
    """
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("DELETE FROM framework_ranges")
    conn.execute("DELETE FROM range_frameworks")
    conn.execute("COMMIT")


def _insert_framework(
    conn: sqlcipher3.Connection, framework_id: int, name: str
) -> None:
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO range_frameworks (id, name) VALUES (?, ?)", (framework_id, name)
    )
    conn.execute("COMMIT")


def _insert_range(
    conn: sqlcipher3.Connection,
    range_id: int,
    framework_id: int,
    biomarker_id: int,
    *,
    range_low: float | None = 0.0,
    range_high: float | None = 10.0,
    unit: str = "mg/dL",
    range_text: str | None = None,
) -> None:
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO framework_ranges "
        "(id, framework_id, biomarker_id, range_low, range_high, unit, range_text) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (range_id, framework_id, biomarker_id, range_low, range_high, unit, range_text),
    )
    conn.execute("COMMIT")


def test_range_frameworks_list_and_get(conn: sqlcipher3.Connection) -> None:
    _clear_seeded_reference_ranges(conn)
    _insert_framework(conn, 1, "Lab Standard")
    _insert_framework(conn, 2, "Attia")
    page = reads.list_range_frameworks(conn, limit=10)
    assert [row["name"] for row in page.items] == ["Attia", "Lab Standard"]  # name asc
    got = reads.get_range_framework(conn, 1)
    assert got is not None
    assert got["name"] == "Lab Standard"


def test_framework_ranges_list_get_and_filters(conn: sqlcipher3.Connection) -> None:
    _clear_seeded_reference_ranges(conn)
    _insert_framework(conn, 1, "Lab Standard")
    _insert_framework(conn, 2, "Attia")
    _insert_range(conn, 1, 1, 1)
    _insert_range(conn, 2, 1, 2)
    _insert_range(conn, 3, 2, 1)

    page = reads.list_framework_ranges(conn, limit=10)
    assert [row["id"] for row in page.items] == [1, 2, 3]  # id asc, stable

    by_framework = reads.list_framework_ranges(conn, framework_id=1, limit=10)
    assert [row["id"] for row in by_framework.items] == [1, 2]

    by_biomarker = reads.list_framework_ranges(conn, biomarker_id=1, limit=10)
    assert [row["id"] for row in by_biomarker.items] == [1, 3]

    got = reads.get_framework_range(conn, 1)
    assert got is not None
    assert (got["range_low"], got["range_high"], got["unit"]) == (0.0, 10.0, "mg/dL")


def test_framework_ranges_pagination_partition(conn: sqlcipher3.Connection) -> None:
    _clear_seeded_reference_ranges(conn)
    _insert_framework(conn, 1, "Lab Standard")
    # Distinct biomarker_id per range: the dateless-default unique index
    # (migration 0001) allows only one (framework_id, biomarker_id) row with
    # effective_date NULL.
    for range_id in range(1, 8):
        _insert_range(conn, range_id, 1, range_id)
    ids: list[int] = []
    cursor: str | None = None
    for _ in range(50):
        page = reads.list_framework_ranges(conn, limit=2, cursor=cursor)
        assert len(page.items) <= 2
        ids.extend(int(row["id"]) for row in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert ids == list(range(1, 8))


# --------------------------------------------------------------------------
# Range-comparison enrichment (?framework=, ADR-0005/ADR-0058)
#
# Migration 0005 seeds real, generic-public reference data (no personal
# health data): the `nih_medlineplus_lipid_targets` framework covers Total
# Cholesterol (id 1, range_high=200, range_low NULL) and the
# `ada_standards_of_care` framework covers Glucose (id 7, [70, 99] mg/dL) —
# both used below rather than reinventing synthetic frameworks, since the
# seed itself is exactly the fixture testing-strategy.md asks for: generic,
# defensibly-sourced, no owner health values.
# --------------------------------------------------------------------------


def _biomarker_id(conn: sqlcipher3.Connection, canonical_name: str) -> int:
    row = conn.execute(
        "SELECT id FROM biomarkers WHERE canonical_name = ?", (canonical_name,)
    ).fetchone()
    assert row is not None
    return int(row[0])


def test_framework_enrichment_present_only_with_param(
    conn: sqlcipher3.Connection,
) -> None:
    draw = _insert_draw(conn, "2024-03-14T13:30:00Z")
    total_chol = _biomarker_id(conn, "Total Cholesterol")
    _insert_result(conn, draw, total_chol, value_num=180.0, unit="mg/dL")

    bare = reads.list_lab_results(conn, limit=10)
    (row,) = bare.items
    assert "range_comparison" not in row  # opt-in only (ADR-0053 unshifted)

    enriched = reads.list_lab_results(
        conn, limit=10, framework="nih_medlineplus_lipid_targets"
    )
    (enriched_row,) = enriched.items
    assert enriched_row["range_comparison"]["flag"] == "in_range"
    # Every other field is unaffected by the projection.
    without_comparison = {
        k: v for k, v in enriched_row.items() if k != "range_comparison"
    }
    assert without_comparison == row

    result_id = int(row["id"])
    bare_by_id = reads.get_lab_result(conn, result_id)
    assert bare_by_id is not None
    assert "range_comparison" not in bare_by_id
    by_id = reads.get_lab_result(
        conn, result_id, framework="nih_medlineplus_lipid_targets"
    )
    assert by_id is not None
    assert by_id["range_comparison"]["flag"] == "in_range"


def test_framework_case_insensitive_and_unknown_raises(
    conn: sqlcipher3.Connection,
) -> None:
    draw = _insert_draw(conn, "2024-03-14T13:30:00Z")
    total_chol = _biomarker_id(conn, "Total Cholesterol")
    _insert_result(conn, draw, total_chol, value_num=180.0, unit="mg/dL")

    lower = reads.list_lab_results(
        conn, limit=10, framework="nih_medlineplus_lipid_targets"
    )
    shouted = reads.list_lab_results(
        conn, limit=10, framework="NIH_MEDLINEPLUS_LIPID_TARGETS"
    )
    mixed = reads.list_lab_results(
        conn, limit=10, framework="Nih_MedlinePlus_Lipid_Targets"
    )
    for page in (lower, shouted, mixed):
        assert page.items[0]["range_comparison"]["flag"] == "in_range"
        # The stored canonical name travels through regardless of the
        # caller's own casing (brief §6 / reads._resolve_framework).
        assert page.items[0]["range_comparison"]["framework"] == (
            "nih_medlineplus_lipid_targets"
        )

    with pytest.raises(reads.FrameworkNotFoundError):
        reads.list_lab_results(conn, limit=10, framework="not-a-real-framework")
    # Unknown framework raises even for a row_id that does not exist either —
    # the framework name is resolved before the row lookup (reads.py), so it
    # is never masked into a plain "not found" for either reason.
    with pytest.raises(reads.FrameworkNotFoundError):
        reads.get_lab_result(conn, 999, framework="not-a-real-framework")


def test_framework_cursor_is_a_projection_not_a_filter(
    conn: sqlcipher3.Connection,
) -> None:
    total_chol = _biomarker_id(conn, "Total Cholesterol")
    for month in range(1, 4):
        draw = _insert_draw(conn, f"2024-0{month}-01T08:00:00Z")
        _insert_result(conn, draw, total_chol, value_num=180.0, unit="mg/dL")

    framework = "nih_medlineplus_lipid_targets"

    # A cursor minted WITHOUT the param still walks correctly WITH it.
    page1 = reads.list_lab_results(conn, limit=1)
    assert "range_comparison" not in page1.items[0]
    assert page1.next_cursor is not None
    page2 = reads.list_lab_results(
        conn, limit=1, cursor=page1.next_cursor, framework=framework
    )
    assert page2.items[0]["range_comparison"]["flag"] == "in_range"

    # And the reverse: a cursor minted WITH the param still walks correctly
    # WITHOUT it, landing on the exact same next row (order/rows unaffected).
    page1b = reads.list_lab_results(conn, limit=1, framework=framework)
    assert page1b.items[0]["id"] == page1.items[0]["id"]
    assert page1b.next_cursor is not None
    page2b = reads.list_lab_results(conn, limit=1, cursor=page1b.next_cursor)
    assert "range_comparison" not in page2b.items[0]
    assert page2b.items[0]["id"] == page2.items[0]["id"]

    # A full walk under the param visits every row exactly once, in exactly
    # the same order as an unenriched walk — the independently-established
    # baseline this cross-checks against.
    baseline_ids: list[int] = []
    cursor: str | None = None
    for _ in range(10):
        page = reads.list_lab_results(conn, limit=1, cursor=cursor)
        baseline_ids.extend(int(item["id"]) for item in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break

    walked_ids: list[int] = []
    flags: list[str] = []
    cursor = None
    for _ in range(10):
        page = reads.list_lab_results(conn, limit=1, cursor=cursor, framework=framework)
        walked_ids.extend(int(item["id"]) for item in page.items)
        flags.extend(str(item["range_comparison"]["flag"]) for item in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert walked_ids == baseline_ids
    assert len(walked_ids) == 3
    assert flags == ["in_range"] * 3


def test_framework_flags_in_range_above_below_no_range(
    conn: sqlcipher3.Connection,
) -> None:
    glucose = _biomarker_id(conn, "Glucose")  # ada_standards_of_care: [70, 99] mg/dL
    # One draw per result: the (lab_draw_id, biomarker_id) natural key
    # (migration 0003) forbids two results for the same biomarker on one draw.
    normal = _insert_result(
        conn, _insert_draw(conn, "2024-01-01T08:00:00Z"), glucose, value_num=90.0
    )
    high = _insert_result(
        conn, _insert_draw(conn, "2024-02-01T08:00:00Z"), glucose, value_num=150.0
    )
    low = _insert_result(
        conn, _insert_draw(conn, "2024-03-01T08:00:00Z"), glucose, value_num=50.0
    )
    # The fixture biomarker carries no seeded range in any framework.
    uncovered = _insert_result(
        conn,
        _insert_draw(conn, "2024-04-01T08:00:00Z"),
        _FIXTURE_BIOMARKER_1,
        value_num=1.0,
    )

    page = reads.list_lab_results(conn, limit=10, framework="ada_standards_of_care")
    flags = {int(row["id"]): row["range_comparison"]["flag"] for row in page.items}
    assert flags[normal] == "in_range"
    assert flags[high] == "above"
    assert flags[low] == "below"
    assert flags[uncovered] == "no_range"


def test_framework_preserves_censored_value_fidelity(
    conn: sqlcipher3.Connection,
) -> None:
    # aha_cdc_hscrp_risk_strata: hs-CRP < 1.0 mg/L is the "optimal" band.
    hscrp = _biomarker_id(conn, "hs-CRP")
    draw = _insert_draw(conn, "2024-03-14T13:30:00Z")
    _insert_result(conn, draw, hscrp, value_num=0.1, comparator="<", unit="mg/L")

    page = reads.list_lab_results(conn, limit=10, framework="aha_cdc_hscrp_risk_strata")
    (row,) = page.items
    # Value fidelity (ADR-0030/0053) is untouched by the enrichment.
    assert (row["value_num"], row["comparator"], row["value_text"]) == (0.1, "<", None)
    assert row["display"] == "<0.1"
    comparison = row["range_comparison"]
    assert comparison["flag"] == "in_range"
    assert comparison["range_high"] == 1.0
    assert comparison["unit"] == "mg/L"


def _framework_id(conn: sqlcipher3.Connection, name: str) -> int:
    row = conn.execute(
        "SELECT id FROM range_frameworks WHERE name = ?", (name,)
    ).fetchone()
    assert row is not None
    return int(row[0])


def test_framework_not_comparable_qualitative_result(
    conn: sqlcipher3.Connection,
) -> None:
    # Total Cholesterol has a real numeric target under
    # nih_medlineplus_lipid_targets (range_high=200, floor NULL) -- unlike
    # the fixture biomarker used for `no_range` above, this framework *does*
    # cover this biomarker numerically, so a qualitative result here can only
    # land on `not_comparable` via compare() step 3 (ADR-0030's
    # value_num IS NULL path), never step 1. That is exactly what proves
    # _enrich_range_comparison passes the row's real (value_num, value_text)
    # pair through rather than always treating a present row as numeric.
    total_chol = _biomarker_id(conn, "Total Cholesterol")
    draw = _insert_draw(conn, "2024-03-14T13:30:00Z")
    _insert_result(
        conn,
        draw,
        total_chol,
        value_num=None,
        value_text="Not Detected",
        unit="mg/dL",
    )

    page = reads.list_lab_results(
        conn, limit=10, framework="nih_medlineplus_lipid_targets"
    )
    (row,) = page.items
    # Value fidelity (ADR-0030/0053) is untouched by the enrichment: the
    # qualitative triple and the display string still come through exactly
    # as an unenriched read would produce them.
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

    # get_lab_result serializes through the same enrichment path.
    by_id = reads.get_lab_result(
        conn, int(row["id"]), framework="nih_medlineplus_lipid_targets"
    )
    assert by_id is not None
    assert by_id["range_comparison"]["flag"] == "not_comparable"


def test_framework_not_comparable_range_text_only_target(
    conn: sqlcipher3.Connection,
) -> None:
    # The seed (migration 0005) has no range_text-only row to drive this
    # through; ADR-0005's CHECK permits one (range_low AND range_high both
    # NULL, range_text set), so this inserts one directly rather than
    # reinventing a framework. Vitamin D 25-OH is a real, seeded biomarker
    # the starter catalog leaves uncovered by any framework range (migration
    # 0005 only seeds lipids/A1c/glucose/hs-CRP), so this is the only range
    # row that can resolve for the pair.
    nih = _framework_id(conn, "nih_medlineplus_lipid_targets")
    vit_d = _biomarker_id(conn, "Vitamin D 25-OH")
    _insert_range(
        conn,
        101,
        nih,
        vit_d,
        range_low=None,
        range_high=None,
        unit="ng/mL",
        range_text="See clinician interpretation",
    )
    draw = _insert_draw(conn, "2024-03-14T13:30:00Z")
    _insert_result(conn, draw, vit_d, value_num=35.0, unit="ng/mL")

    page = reads.list_lab_results(
        conn, limit=10, framework="nih_medlineplus_lipid_targets"
    )
    (row,) = page.items
    comparison = row["range_comparison"]
    assert comparison["flag"] == "not_comparable"
    assert comparison["reason"] is not None
    assert comparison["range_low"] is None
    assert comparison["range_high"] is None
    # The target's own range_text still travels through to the client even
    # though it could not be numerically compared (compare()'s `_incomplete`
    # contract).
    assert comparison["range_text"] == "See clinician interpretation"


def test_framework_error_unreconcilable_units_page_survives(
    conn: sqlcipher3.Connection,
) -> None:
    # hs-CRP (aha_cdc_hscrp_risk_strata: <1.0 mg/L) is seeded with NO
    # molar_mass (migration 0005 deliberately omits it) and canonical_unit
    # mg/L. A result reported in mmol/L -- a genuine mass<->substance molar
    # differing unit, not garbage -- cannot be normalized without one, so
    # compare() step 4 catches MissingMolarContextError into a loud `error`
    # flag (never an exception escaping to the caller). A second hs-CRP
    # result with no unit at all hits the sibling step-4 `error` branch
    # (result.unit is None). A third, ordinary hs-CRP result on the same
    # page proves neither bad row drops or blanks its neighbor -- one bad
    # row must never cost the rest of the page (ADR-0058 §3).
    hscrp = _biomarker_id(conn, "hs-CRP")
    ok = _insert_result(
        conn,
        _insert_draw(conn, "2024-01-01T08:00:00Z"),
        hscrp,
        value_num=0.5,
        unit="mg/L",
    )
    molar_mismatch = _insert_result(
        conn,
        _insert_draw(conn, "2024-02-01T08:00:00Z"),
        hscrp,
        value_num=0.01,
        unit="mmol/L",
    )
    no_unit = _insert_result(
        conn,
        _insert_draw(conn, "2024-03-01T08:00:00Z"),
        hscrp,
        value_num=0.5,
        unit=None,
    )

    page = reads.list_lab_results(conn, limit=10, framework="aha_cdc_hscrp_risk_strata")
    assert len(page.items) == 3  # a bad row is flagged, never dropped
    by_id = {int(row["id"]): row["range_comparison"] for row in page.items}

    assert by_id[ok]["flag"] == "in_range"

    assert by_id[molar_mismatch]["flag"] == "error"
    assert by_id[molar_mismatch]["reason"] is not None
    assert "hs-CRP" in by_id[molar_mismatch]["reason"]
    assert "molar_mass" in by_id[molar_mismatch]["reason"]

    assert by_id[no_unit]["flag"] == "error"
    assert by_id[no_unit]["reason"] is not None
    assert "unit" in by_id[no_unit]["reason"]


# --------------------------------------------------------------------------
# Pagination partition property
# --------------------------------------------------------------------------

_DATES = [f"2024-0{month}-01T08:00:00Z" for month in range(1, 6)]


def _walk(
    conn: sqlcipher3.Connection, *, order: str, limit: int
) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    cursor: str | None = None
    for _ in range(100):  # bounded: a cursor bug must not hang the suite
        page = reads.list_lab_draws(conn, order=order, limit=limit, cursor=cursor)
        assert len(page.items) <= limit
        items.extend(page.items)
        cursor = page.next_cursor
        if cursor is None:
            return items
    raise AssertionError("pagination did not terminate")


@settings(max_examples=25, deadline=None)
@given(
    # (date, lab) pairs, unique — the 0003 natural key (lab_id, draw_utc)
    # forbids duplicate current draws; two labs still produce date ties, so
    # the id tiebreak stays exercised.
    draws=st.lists(
        st.tuples(st.sampled_from(_DATES), st.integers(min_value=1, max_value=2)),
        unique=True,
        max_size=10,
    ),
    limit=st.integers(min_value=1, max_value=5),
    order=st.sampled_from(["asc", "desc"]),
)
def test_pagination_partitions_current_rows(
    tmp_path_factory: pytest.TempPathFactory,
    draws: list[tuple[str, int]],
    limit: int,
    order: str,
) -> None:
    conn = _build_db(tmp_path_factory.mktemp("paginate"))
    try:
        expected = sorted(
            ((date, _insert_draw(conn, date, lab_id=lab)) for date, lab in draws),
            reverse=(order == "desc"),
        )
        walked = _walk(conn, order=order, limit=limit)
        assert [(row["draw_utc"], row["id"]) for row in walked] == expected
    finally:
        db.close(conn)
