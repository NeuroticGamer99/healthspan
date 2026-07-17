"""Read/query layer over the current-state views (ADR-0027, ADR-0053).

Phase 2's read path: keyset ("cursor") pagination over the ``*_current``
views for the import-populated tables, plus the catalog tables a client
needs to resolve their foreign keys and browse reference data (``labs``,
``biomarkers``, and — added in Phase 3 WI-2, ADR-0055/ADR-0057 —
``categories``, ``range_frameworks``, ``framework_ranges``). Every list
query is bounded by the server-enforced page cap — the caller (the API
layer) passes the effective limit, already clamped; this module never
fetches more than ``limit + 1`` rows (the extra row only signals that a
next page exists).

Ordering is fixed per resource (ADR-0053): clinical rows newest-first by
``draw_utc`` (with ``id`` as the tiebreak), catalog rows by name
(``framework_ranges`` by ``id`` — it has no name column). The cursor is an
opaque base64url token encoding schema version, direction, and the last
row's ``(sort key, id)`` keyset; it binds to the direction it was minted
under and is rejected (`CursorError`) if replayed against the other.

Value fidelity (ADR-0030/0031): result rows carry the explicit
``(value_num, comparator, value_text)`` triple, the UCUM ``unit`` as
reported, the lab's own reference range — and a ``display`` string that
preserves the comparator, so a censored ``<0.1`` never degrades to a bare
``0.1``.

Range-comparison enrichment (ADR-0005/ADR-0058, Phase 3 WI-3): the optional
``framework`` parameter on ``list_lab_results``/``get_lab_result`` attaches a
``range_comparison`` object (:func:`healthspan.ranges.compare`, serialized)
to each result row. It is a projection, not a filter — it changes what each
row carries, never which rows are returned or their order — so it is
resolved and applied *after* the page query and plays no part in the cursor.
"""

import base64
import binascii
import dataclasses
import json
from dataclasses import dataclass
from typing import Any, cast

import sqlcipher3

from healthspan import ranges

CURSOR_VERSION = 1

Row = dict[str, Any]


class CursorError(Exception):
    """A pagination cursor could not be decoded or does not fit the query."""


class FrameworkNotFoundError(Exception):
    """``?framework=`` named no known ``range_frameworks`` row.

    Deliberately unlike ``?category=``'s empty-page rule (ADR-0055 §1): a
    typo'd category yields an obviously-empty page, but a typo'd framework
    would silently yield a full page of plausible-looking rows all flagged
    ``no_range`` — a wrong answer that looks right, the exact failure mode
    ADR-0005/ADR-0058 exist to prevent. So this is a loud, distinct error the
    API layer turns into a 422, mirroring how :class:`CursorError` already
    becomes one.
    """


@dataclass(frozen=True)
class Page:
    """One page of serialized rows and the cursor for the next, if any."""

    items: list[Row]
    next_cursor: str | None


@dataclass(frozen=True)
class _Resource:
    """The fixed SQL shape of one readable resource (ADR-0053).

    ``select`` and the two key expressions are code constants — never
    caller-supplied — so every dynamic value in a query travels as a bound
    parameter.
    """

    select: str  # SELECT ... FROM ... (join included where needed)
    sort_expr: str  # qualified deterministic sort column
    id_expr: str  # qualified primary-key column (the keyset tiebreak)


# Result rows embed read-only draw context (draw_utc, lab_id) from the join
# (ADR-0053): a biomarker-history page is plottable without N+1 draw fetches.
# The join targets the base table: a result's FK names a real draw row
# regardless of currency (draws never supersede — ADR-0052 identity rows).
_LAB_DRAWS = _Resource(
    select="SELECT d.* FROM lab_draws_current d",
    sort_expr="d.draw_utc",
    id_expr="d.id",
)
_LAB_RESULTS = _Resource(
    select=(
        "SELECT r.*, d.draw_utc AS draw_utc, d.lab_id AS lab_id "
        "FROM lab_results_current r JOIN lab_draws d ON d.id = r.lab_draw_id"
    ),
    sort_expr="d.draw_utc",
    id_expr="r.id",
)
_LABS = _Resource(select="SELECT l.* FROM labs l", sort_expr="l.name", id_expr="l.id")
# Naive JOIN is correct because every biomarker has a category (reserved
# default id 0 — ADR-0055 §2); `category` carries the NAME (back-compat with
# the Phase-2 response shape), `category_id` the FK.
_BIOMARKERS = _Resource(
    select=(
        "SELECT b.*, c.name AS category "
        "FROM biomarkers b JOIN categories c ON c.id = b.category_id"
    ),
    sort_expr="b.canonical_name",
    id_expr="b.id",
)
_CATEGORIES = _Resource(
    select="SELECT c.* FROM categories c", sort_expr="c.name", id_expr="c.id"
)
_RANGE_FRAMEWORKS = _Resource(
    select="SELECT f.* FROM range_frameworks f", sort_expr="f.name", id_expr="f.id"
)
_FRAMEWORK_RANGES = _Resource(
    select="SELECT r.* FROM framework_ranges r", sort_expr="r.id", id_expr="r.id"
)


# --------------------------------------------------------------------------
# Cursor encoding (ADR-0053): opaque, versioned, direction-bound
# --------------------------------------------------------------------------


def encode_cursor(order: str, sort_value: str, row_id: int) -> str:
    payload = json.dumps(
        {"v": CURSOR_VERSION, "o": order, "k": [sort_value, row_id]},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def decode_cursor(raw: str, order: str) -> tuple[str, int]:
    """Decode a cursor, verifying version and direction.

    Any malformed token — bad base64, bad JSON, wrong shape, wrong types —
    is one uniform ``CursorError``; the message never echoes the token back.
    """
    try:
        payload: object = json.loads(base64.urlsafe_b64decode(raw.encode("ascii")))
    except binascii.Error, UnicodeError, ValueError:
        raise CursorError("invalid cursor") from None
    if not isinstance(payload, dict):
        raise CursorError("invalid cursor")
    fields = cast(dict[object, object], payload)
    if fields.get("v") != CURSOR_VERSION:
        raise CursorError("invalid cursor")
    if fields.get("o") != order:
        raise CursorError(
            "cursor does not match the requested order; "
            "keep 'order' constant while paginating"
        )
    key = fields.get("k")
    if not isinstance(key, list):
        raise CursorError("invalid cursor")
    parts = cast(list[object], key)
    if len(parts) != 2:
        raise CursorError("invalid cursor")
    sort_value, row_id = parts
    if (
        not isinstance(sort_value, str)
        or isinstance(row_id, bool)
        or not isinstance(row_id, int)
    ):
        raise CursorError("invalid cursor")
    return sort_value, row_id


# --------------------------------------------------------------------------
# Generic keyset list / get
# --------------------------------------------------------------------------


def _list(
    conn: sqlcipher3.Connection,
    resource: _Resource,
    filters: list[tuple[str, object]],
    *,
    order: str,
    limit: int,
    cursor: str | None,
) -> tuple[list[Row], str | None]:
    """Run one keyset page query; returns raw row dicts and the next cursor.

    ``filters`` pairs a predicate template (a code constant containing one
    ``?``) with its bound value; ``None`` values were dropped by the caller.
    """
    where = [predicate for predicate, _ in filters]
    params: list[object] = [value for _, value in filters]
    op = "<" if order == "desc" else ">"
    if cursor is not None:
        sort_value, row_id = decode_cursor(cursor, order)
        where.append(
            f"({resource.sort_expr} {op} ? OR "
            f"({resource.sort_expr} = ? AND {resource.id_expr} {op} ?))"
        )
        params.extend([sort_value, sort_value, row_id])
    direction = "DESC" if order == "desc" else "ASC"
    sql = resource.select
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += (
        f" ORDER BY {resource.sort_expr} {direction},"
        f" {resource.id_expr} {direction} LIMIT ?"
    )
    params.append(limit + 1)  # one extra row = "a next page exists", never sent
    cur = conn.execute(sql, tuple(params))
    rows = _as_dicts(cur)
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if has_more:
        last = rows[-1]
        sort_column = resource.sort_expr.split(".", 1)[1]
        next_cursor = encode_cursor(order, str(last[sort_column]), int(last["id"]))
    return rows, next_cursor


def _get(conn: sqlcipher3.Connection, resource: _Resource, row_id: int) -> Row | None:
    cur = conn.execute(f"{resource.select} WHERE {resource.id_expr} = ?", (row_id,))
    rows = _as_dicts(cur)
    return rows[0] if rows else None


def _as_dicts(cur: sqlcipher3.Cursor) -> list[Row]:
    names = [column[0] for column in cur.description]
    return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


# --------------------------------------------------------------------------
# Serialization (ADR-0030/0031/0053 value fidelity)
# --------------------------------------------------------------------------


def display_value(
    value_num: float | None, comparator: str | None, value_text: str | None
) -> str:
    """The human-readable value string, comparator preserved (ADR-0053).

    Numeric wins when both forms are present (``value_text`` still travels
    in the triple); the ADR-0030 CHECK guarantees at least one is set.
    """
    if value_num is not None:
        return f"{comparator or ''}{_format_num(value_num)}"
    return value_text if value_text is not None else ""


def _format_num(value: float) -> str:
    # SQLite REAL round-trips as float; render integral magnitudes without
    # the trailing ".0" a float str() would add ("5", not "5.0").
    if value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    return str(value)


def _serialize_current(row: Row) -> Row:
    # superseded_by is NULL by construction on every current-view row;
    # dropping it keeps the response free of always-null noise.
    return {name: value for name, value in row.items() if name != "superseded_by"}


def _serialize_result(row: Row) -> Row:
    out = _serialize_current(row)
    out["display"] = display_value(
        row["value_num"], row["comparator"], row["value_text"]
    )
    return out


# --------------------------------------------------------------------------
# Range-comparison enrichment (ADR-0005/ADR-0058, ?framework=)
# --------------------------------------------------------------------------


def _resolve_framework(conn: sqlcipher3.Connection, name: str) -> tuple[int, str]:
    """Resolve ``?framework=`` to ``(id, canonical name)``.

    The case-insensitive lookup itself is :func:`ranges.resolve_framework_id`
    (ADR-0055 §1 rule, reused per ADR-0058 §1); an unresolved name raises
    :class:`FrameworkNotFoundError` here rather than falling back to an
    empty page. The *stored* name — not the caller's own casing — is what
    travels into each row's ``range_comparison.framework``, so two requests
    differing only in case produce byte-identical enrichment (the same
    honesty-of-what-was-actually-used principle ``ranges.Comparison``
    already applies to its normalized ``range_low``/``range_high``/``unit``).
    """
    framework_id = ranges.resolve_framework_id(conn, name)
    if framework_id is None:
        raise FrameworkNotFoundError(name)
    row = conn.execute(
        "SELECT name FROM range_frameworks WHERE id = ?", (framework_id,)
    ).fetchone()
    if row is None:
        # framework_id was just resolved by the lookup above, in the same
        # connection/transaction; its row cannot have vanished in between.
        raise RuntimeError(
            f"range_frameworks id {framework_id} resolved but its row is gone"
        )
    return framework_id, str(row[0])


def _enrich_range_comparison(
    conn: sqlcipher3.Connection,
    framework_id: int,
    framework_name: str,
    rows: list[Row],
) -> list[Row]:
    """Attach a ``range_comparison`` object to each already-serialized row.

    Bounded query count regardless of page size (brief §6): one catalog
    lookup keyed by the page's distinct ``biomarker_id``s, one
    :func:`ranges.resolve_ranges` call for the whole page — never N+1.
    """
    if not rows:
        return rows

    # Static SQL text; the variable-length id list travels as a single bound
    # JSON-array parameter consumed by json_each — the same technique
    # ranges._RESOLVE_RANGES_SQL uses — rather than an interpolated,
    # dynamically-sized IN (...) placeholder list.
    biomarker_ids = sorted({int(row["biomarker_id"]) for row in rows})
    catalog = {
        int(biomarker_id): (canonical_unit, molar_mass, canonical_name)
        for biomarker_id, canonical_unit, molar_mass, canonical_name in conn.execute(
            "SELECT id, canonical_unit, molar_mass, canonical_name FROM biomarkers "
            "WHERE id IN (SELECT value FROM json_each(?))",
            (json.dumps(biomarker_ids),),
        ).fetchall()
    }

    pairs = [(int(row["biomarker_id"]), str(row["draw_utc"])[:10]) for row in rows]
    resolved = ranges.resolve_ranges(conn, framework_id, pairs)

    for row, range_row in zip(rows, resolved, strict=True):
        canonical_unit, molar_mass, canonical_name = catalog[int(row["biomarker_id"])]
        comparison = ranges.compare(
            framework=framework_name,
            biomarker=canonical_name,
            canonical_unit=canonical_unit,
            molar_mass=molar_mass,
            range_row=range_row,
            result=ranges.ResultValue(
                value_num=row["value_num"],
                comparator=row["comparator"],
                value_text=row["value_text"],
                unit=row["unit"],
            ),
        )
        row["range_comparison"] = dataclasses.asdict(comparison)
    return rows


# --------------------------------------------------------------------------
# Public per-resource surface
# --------------------------------------------------------------------------


def list_lab_draws(
    conn: sqlcipher3.Connection,
    *,
    lab_id: int | None = None,
    draw_from: str | None = None,
    draw_to: str | None = None,
    order: str = "desc",
    limit: int,
    cursor: str | None = None,
) -> Page:
    filters: list[tuple[str, object]] = []
    if lab_id is not None:
        filters.append(("d.lab_id = ?", lab_id))
    if draw_from is not None:
        filters.append(("d.draw_utc >= ?", draw_from))
    if draw_to is not None:
        filters.append(("d.draw_utc <= ?", draw_to))
    rows, next_cursor = _list(
        conn, _LAB_DRAWS, filters, order=order, limit=limit, cursor=cursor
    )
    return Page([_serialize_current(row) for row in rows], next_cursor)


def get_lab_draw(conn: sqlcipher3.Connection, row_id: int) -> Row | None:
    row = _get(conn, _LAB_DRAWS, row_id)
    return _serialize_current(row) if row is not None else None


def list_lab_results(
    conn: sqlcipher3.Connection,
    *,
    biomarker_id: int | None = None,
    lab_draw_id: int | None = None,
    lab_id: int | None = None,
    draw_from: str | None = None,
    draw_to: str | None = None,
    order: str = "desc",
    limit: int,
    cursor: str | None = None,
    framework: str | None = None,
) -> Page:
    # Resolved before the page query (brief §6): an unknown framework name
    # must fail loud without ever running the (potentially large) page
    # query, and `framework` plays no part in `filters`/the cursor — it is a
    # projection applied to the page's rows afterward, not a row filter.
    resolved_framework: tuple[int, str] | None = None
    if framework is not None:
        resolved_framework = _resolve_framework(conn, framework)

    filters: list[tuple[str, object]] = []
    if biomarker_id is not None:
        filters.append(("r.biomarker_id = ?", biomarker_id))
    if lab_draw_id is not None:
        filters.append(("r.lab_draw_id = ?", lab_draw_id))
    if lab_id is not None:
        filters.append(("d.lab_id = ?", lab_id))
    if draw_from is not None:
        filters.append(("d.draw_utc >= ?", draw_from))
    if draw_to is not None:
        filters.append(("d.draw_utc <= ?", draw_to))
    rows, next_cursor = _list(
        conn, _LAB_RESULTS, filters, order=order, limit=limit, cursor=cursor
    )
    serialized = [_serialize_result(row) for row in rows]
    if resolved_framework is not None:
        framework_id, framework_name = resolved_framework
        serialized = _enrich_range_comparison(
            conn, framework_id, framework_name, serialized
        )
    return Page(serialized, next_cursor)


def get_lab_result(
    conn: sqlcipher3.Connection, row_id: int, *, framework: str | None = None
) -> Row | None:
    # Resolved before the row fetch, mirroring list_lab_results: an unknown
    # framework name is always a 422 at the API layer, regardless of
    # whether row_id happens to exist (never masked into a 404).
    resolved_framework: tuple[int, str] | None = None
    if framework is not None:
        resolved_framework = _resolve_framework(conn, framework)

    row = _get(conn, _LAB_RESULTS, row_id)
    if row is None:
        return None
    serialized = _serialize_result(row)
    if resolved_framework is not None:
        framework_id, framework_name = resolved_framework
        (serialized,) = _enrich_range_comparison(
            conn, framework_id, framework_name, [serialized]
        )
    return serialized


def list_labs(
    conn: sqlcipher3.Connection,
    *,
    order: str = "asc",
    limit: int,
    cursor: str | None = None,
) -> Page:
    rows, next_cursor = _list(conn, _LABS, [], order=order, limit=limit, cursor=cursor)
    return Page(rows, next_cursor)


def get_lab(conn: sqlcipher3.Connection, row_id: int) -> Row | None:
    return _get(conn, _LABS, row_id)


def list_biomarkers(
    conn: sqlcipher3.Connection,
    *,
    category: str | None = None,
    order: str = "asc",
    limit: int,
    cursor: str | None = None,
) -> Page:
    filters: list[tuple[str, object]] = []
    if category is not None:
        # Case-insensitive category-NAME -> id lookup (ADR-0055 §1). An
        # unknown name resolves the subselect to NULL, so `category_id = NULL`
        # matches nothing — an empty page, never an error.
        filters.append(
            (
                "b.category_id = (SELECT id FROM categories "
                "WHERE name = ? COLLATE NOCASE)",
                category,
            )
        )
    rows, next_cursor = _list(
        conn, _BIOMARKERS, filters, order=order, limit=limit, cursor=cursor
    )
    return Page(rows, next_cursor)


def get_biomarker(conn: sqlcipher3.Connection, row_id: int) -> Row | None:
    return _get(conn, _BIOMARKERS, row_id)


def list_categories(
    conn: sqlcipher3.Connection,
    *,
    order: str = "asc",
    limit: int,
    cursor: str | None = None,
) -> Page:
    rows, next_cursor = _list(
        conn, _CATEGORIES, [], order=order, limit=limit, cursor=cursor
    )
    return Page(rows, next_cursor)


def get_category(conn: sqlcipher3.Connection, row_id: int) -> Row | None:
    return _get(conn, _CATEGORIES, row_id)


def list_range_frameworks(
    conn: sqlcipher3.Connection,
    *,
    order: str = "asc",
    limit: int,
    cursor: str | None = None,
) -> Page:
    rows, next_cursor = _list(
        conn, _RANGE_FRAMEWORKS, [], order=order, limit=limit, cursor=cursor
    )
    return Page(rows, next_cursor)


def get_range_framework(conn: sqlcipher3.Connection, row_id: int) -> Row | None:
    return _get(conn, _RANGE_FRAMEWORKS, row_id)


def list_framework_ranges(
    conn: sqlcipher3.Connection,
    *,
    framework_id: int | None = None,
    biomarker_id: int | None = None,
    order: str = "asc",
    limit: int,
    cursor: str | None = None,
) -> Page:
    filters: list[tuple[str, object]] = []
    if framework_id is not None:
        filters.append(("r.framework_id = ?", framework_id))
    if biomarker_id is not None:
        filters.append(("r.biomarker_id = ?", biomarker_id))
    rows, next_cursor = _list(
        conn, _FRAMEWORK_RANGES, filters, order=order, limit=limit, cursor=cursor
    )
    return Page(rows, next_cursor)


def get_framework_range(conn: sqlcipher3.Connection, row_id: int) -> Row | None:
    return _get(conn, _FRAMEWORK_RANGES, row_id)
