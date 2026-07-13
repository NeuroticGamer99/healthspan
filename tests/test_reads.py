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


def _build_db(directory: Path) -> sqlcipher3.Connection:
    path = directory / "healthspan.db"
    db.provision(path, _key())
    migrate.migrate_database(path, _key())
    conn = db.connect(path, _key())
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO labs (id, name) VALUES (1, 'Quest')")
    conn.execute("INSERT INTO labs (id, name) VALUES (2, 'LabCorp')")
    for biomarker_id, category in ((1, "lipids"), (2, "thyroid"), (3, None)):
        conn.execute(
            "INSERT INTO biomarkers (id, canonical_name, category) VALUES (?, ?, ?)",
            (biomarker_id, f"Biomarker {biomarker_id}", category),
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
    r1 = _insert_result(conn, d1, 1)
    r2 = _insert_result(conn, d1, 2)
    r3 = _insert_result(conn, d2, 1)
    by_biomarker = reads.list_lab_results(conn, biomarker_id=1, limit=10)
    assert [row["id"] for row in by_biomarker.items] == [r3, r1]
    by_draw = reads.list_lab_results(conn, lab_draw_id=d1, limit=10)
    assert {row["id"] for row in by_draw.items} == {r1, r2}
    by_lab = reads.list_lab_results(conn, lab_id=2, limit=10)
    assert [row["id"] for row in by_lab.items] == [r3]
    windowed = reads.list_lab_results(
        conn, biomarker_id=1, draw_to="2024-03-01", limit=10
    )
    assert [row["id"] for row in windowed.items] == [r1]


def test_superseded_rows_invisible(conn: sqlcipher3.Connection) -> None:
    draw = _insert_draw(conn, "2024-03-14T13:30:00Z")
    old = _insert_result(conn, draw, 1, value_num=100.0)
    new = _insert_result(conn, draw, 2, value_num=105.0)
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


def test_catalog_reads(conn: sqlcipher3.Connection) -> None:
    labs = reads.list_labs(conn, limit=10)
    assert [row["name"] for row in labs.items] == ["LabCorp", "Quest"]  # name asc
    lab = reads.get_lab(conn, 1)
    assert lab is not None
    assert lab["name"] == "Quest"
    lipids = reads.list_biomarkers(conn, category="lipids", limit=10)
    assert [row["id"] for row in lipids.items] == [1]
    marker = reads.get_biomarker(conn, 3)
    assert marker is not None
    assert marker["category"] is None


def test_catalog_cursor_walk(conn: sqlcipher3.Connection) -> None:
    """Catalog listings paginate by name through the same keyset machinery."""
    conn.execute("BEGIN IMMEDIATE")
    for biomarker_id in range(4, 10):
        conn.execute(
            "INSERT INTO biomarkers (id, canonical_name) VALUES (?, ?)",
            (biomarker_id, f"Biomarker {biomarker_id}"),
        )
    conn.execute("COMMIT")
    names: list[str] = []
    cursor: str | None = None
    for _ in range(20):
        page = reads.list_biomarkers(conn, limit=2, cursor=cursor)
        assert len(page.items) <= 2
        names.extend(str(row["canonical_name"]) for row in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert names == sorted(names)
    assert len(names) == 9  # 3 fixture + 6 inserted, each exactly once


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
