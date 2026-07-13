"""Application-layer audit capture for data mutations (ADR-0027).

The Core Service's data-access layer writes the ``audit_log`` row for a
mutation *in the same transaction* as the mutation, so the integrity record
cannot drift from the data it describes. ADR-0027 rejected trigger-based
capture on the ground that triggers cannot see the provenance — actor token,
import batch, job — that lives only in the request context; this module is
that application-layer write half. Callers open the ``BEGIN IMMEDIATE``
transaction and invoke these inside it (a rolled-back mutation leaves no
audit row, which the mutation-matrix test asserts).

Three granularities, per ADR-0027:

* **batch import** (:func:`record_import`) — one ``import`` row per (batch,
  table) carrying summary counts, with **zero** per-row ``insert`` rows: bulk
  inserts are audited at batch level, and each imported row already carries
  ``import_batch_id``.
* **value correction** (:func:`record_correct`) — one per-row ``correct`` row
  with both row images, for a supersession.
* **metadata repair** (:func:`record_update`) — one per-row ``update`` row
  with both row images, for a designated in-place metadata change.

This module is deliberately table-agnostic — WI-3 wires only the import
shapes, but manual entry (Phase 3) writes ``insert``/``update``/``delete``
through the same helpers.
"""

import json
from typing import cast

import sqlcipher3

from healthspan.keyparams import utc_now_iso

# A JSON row image: column name → SQLite-native scalar (int/float/str/None).
RowImage = dict[str, object]


def row_image(conn: sqlcipher3.Connection, table: str, row_id: int) -> RowImage:
    """The full column image of one row, for an ``old_values``/``new_values``.

    ``table`` comes from the internal importable-table registry, never from
    request input, so the interpolation is not an injection surface.
    """
    cursor = conn.execute(
        f"SELECT * FROM {table} WHERE id = ?",  # noqa: S608 - table from internal registry
        (row_id,),
    )
    row = cursor.fetchone()
    if row is None:
        raise ValueError(f"no row {row_id} in {table} to image")
    columns = [str(description[0]) for description in cursor.description]
    return dict(zip(columns, cast(tuple[object, ...], row), strict=True))


def _as_json(image: RowImage) -> str:
    """Deterministic JSON for an audit image (stable column order)."""
    return json.dumps(image, sort_keys=True, ensure_ascii=False)


def record_import(
    conn: sqlcipher3.Connection,
    *,
    table_name: str,
    summary: RowImage,
    actor: str | None,
    import_batch_id: int,
) -> None:
    """One batch-level ``import`` audit row per (batch, table) (ADR-0027).

    ``row_id`` is ``NULL`` and ``new_values`` holds the summary JSON; there
    are no per-row ``insert`` rows for the inserts this batch carried.
    """
    _insert(
        conn,
        table_name=table_name,
        row_id=None,
        operation="import",
        old_values=None,
        new_values=_as_json(summary),
        actor=actor,
        import_batch_id=import_batch_id,
        reason=None,
    )


def record_correct(
    conn: sqlcipher3.Connection,
    *,
    table_name: str,
    row_id: int,
    old_image: RowImage,
    new_image: RowImage,
    actor: str | None,
    import_batch_id: int | None,
    reason: str | None,
) -> None:
    """One per-row ``correct`` audit row for a supersession (both images).

    ``row_id`` is the new (superseding, now-current) row: the live row's
    audit trail shows it corrected a prior image, and the supersession chain
    walks backward from there (ADR-0027 time-travel).
    """
    _insert(
        conn,
        table_name=table_name,
        row_id=row_id,
        operation="correct",
        old_values=_as_json(old_image),
        new_values=_as_json(new_image),
        actor=actor,
        import_batch_id=import_batch_id,
        reason=reason,
    )


def record_update(
    conn: sqlcipher3.Connection,
    *,
    table_name: str,
    row_id: int,
    old_image: RowImage,
    new_image: RowImage,
    actor: str | None,
    import_batch_id: int | None,
) -> None:
    """One per-row ``update`` audit row for an in-place metadata repair.

    The designated-metadata carve-out (ADR-0027): the row keeps its id, only
    its recorded context changes, and both images are captured.
    """
    _insert(
        conn,
        table_name=table_name,
        row_id=row_id,
        operation="update",
        old_values=_as_json(old_image),
        new_values=_as_json(new_image),
        actor=actor,
        import_batch_id=import_batch_id,
        reason=None,
    )


def _insert(
    conn: sqlcipher3.Connection,
    *,
    table_name: str,
    row_id: int | None,
    operation: str,
    old_values: str | None,
    new_values: str | None,
    actor: str | None,
    import_batch_id: int | None,
    reason: str | None,
) -> None:
    conn.execute(
        "INSERT INTO audit_log (table_name, row_id, operation, old_values, "
        "new_values, occurred_at_utc, actor, import_batch_id, job_id, reason) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
        (
            table_name,
            row_id,
            operation,
            old_values,
            new_values,
            utc_now_iso(),
            actor,
            import_batch_id,
            reason,
        ),
    )
