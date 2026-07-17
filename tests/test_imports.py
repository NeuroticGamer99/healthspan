"""Bulk import engine: validation, conflict resolution, and audit
(ADR-0004/0027/0052).

The mutation-matrix two-shape contract (testing-strategy.md): the bulk-import
path writes exactly one ``import`` audit row per (batch, table) with summary
counts and **zero** per-row ``insert`` rows; every mutation of an existing row
(a value supersession, a metadata repair) writes one per-row image audit; a
rolled-back import (dry-run, or a ``reject`` conflict) writes nothing. Plus the
reconciliation property: the four summary counts partition every input row and
reconcile against the real table deltas.
"""

import json
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
import sqlcipher3
from hypothesis import given, settings
from hypothesis import strategies as st

from healthspan import audit, db, imports, migrate
from healthspan.kdf import DbKey

Row = Mapping[str, object]


def _key() -> DbKey:
    return DbKey(bytearray(range(1, 33)))


def _build_db(directory: Path) -> sqlcipher3.Connection:
    """A migrated database with a seeded catalog (labs 1, biomarkers 1-6).

    Migration 0004 (Phase 3 WI-2) now seeds its own generic reference catalog
    (labs, biomarkers) with autoincrement ids on every fresh database. This
    suite's tests hardcode ``lab_id=1`` and ``biomarker_id`` 1-6 pervasively,
    so replace the migration's seed with this fixed test catalog rather than
    add to it — inserting id 1 alongside the migration's own id-1 rows would
    collide on both the primary key and the natural-key unique index.
    """
    path = directory / "healthspan.db"
    db.provision(path, _key())
    migrate.migrate_database(path, _key())
    conn = db.connect(path, _key())
    conn.execute("BEGIN IMMEDIATE")
    # Clear every table referencing `biomarkers` before the catalog itself:
    # migration 0004 seeds biomarker_aliases, and 0005 (Phase 3 WI-3) seeds
    # framework_ranges, both of which carry a biomarker_id FK.
    conn.execute("DELETE FROM biomarker_aliases")
    conn.execute("DELETE FROM framework_ranges")
    conn.execute("DELETE FROM biomarkers")
    conn.execute("DELETE FROM labs")
    conn.execute("INSERT INTO labs (id, name) VALUES (1, 'Quest')")
    for biomarker_id in range(1, 7):
        conn.execute(
            "INSERT INTO biomarkers (id, canonical_name) VALUES (?, ?)",
            (biomarker_id, f"Biomarker {biomarker_id}"),
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


def _run(
    conn: sqlcipher3.Connection,
    payload: Mapping[str, Sequence[Row]],
    policy: str,
    *,
    dry_run: bool = False,
    actor: str | None = "watch-import",
    source: str = "manual",
) -> imports.ImportOutcome:
    return imports.run_import(
        conn,
        batch=imports.BatchMeta(source=source),
        payload=payload,
        conflict_policy=policy,
        actor=actor,
        dry_run=dry_run,
    )


def _draw(handle: int = 1, **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": handle,
        "lab_id": 1,
        "draw_utc": "2024-03-14T13:30:00Z",
        "draw_context": "comprehensive panel",
        "fasting": 1,
    }
    row.update(overrides)
    return row


def _result(handle: int, biomarker_id: int, **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "lab_draw_id": handle,
        "biomarker_id": biomarker_id,
        "value_num": 100.0,
        "unit": "mg/dL",
    }
    row.update(overrides)
    return row


def _new_framework(conn: sqlcipher3.Connection, name: str = "test_framework") -> int:
    """Import a fresh ``range_frameworks`` row and return its real id.

    A prerequisite for every ``framework_ranges`` test below: ``framework_id``
    is a plain external FK resolved against already-stored rows only, never a
    same-batch handle (ADR-0057 SS9, extended by ADR-0058 SS6) — a framework
    must land in its own prior import before any ``framework_ranges`` row can
    reference it.
    """
    _run(conn, {"range_frameworks": [{"name": name}]}, imports.REJECT)
    return int(_one(conn, "SELECT id FROM range_frameworks WHERE name = ?", name))


def _range_row(
    framework_id: int, biomarker_id: int, **overrides: object
) -> dict[str, object]:
    row: dict[str, object] = {
        "framework_id": framework_id,
        "biomarker_id": biomarker_id,
        "range_high": 100.0,
        "unit": "mg/dL",
    }
    row.update(overrides)
    return row


def _one(conn: sqlcipher3.Connection, sql: str, *params: object) -> Any:
    fetched = conn.execute(sql, params).fetchone()
    assert fetched is not None
    return fetched[0]


def _audit_ops(conn: sqlcipher3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT operation, count(*) FROM audit_log GROUP BY operation"
    ).fetchall()
    return {str(op): int(n) for op, n in rows}


# --------------------------------------------------------------------------
# Fresh import: batch-level insert audit, zero per-row insert rows
# --------------------------------------------------------------------------


def test_fresh_import_inserts_rows_and_batch_audits(
    conn: sqlcipher3.Connection,
) -> None:
    outcome = _run(
        conn,
        {
            "lab_draws": [_draw()],
            "lab_results": [_result(1, 1), _result(1, 2), _result(1, 3)],
        },
        imports.REJECT,
    )
    assert outcome.batch_id is not None
    assert outcome.summaries["lab_draws"].rows_inserted == 1
    assert outcome.summaries["lab_results"].rows_inserted == 3

    assert _one(conn, "SELECT count(*) FROM lab_draws_current") == 1
    assert _one(conn, "SELECT count(*) FROM lab_results_current") == 3

    # Exactly one 'import' audit row per (batch, table); no per-row inserts.
    assert _audit_ops(conn) == {"import": 2}
    import_rows = conn.execute(
        "SELECT table_name, row_id, import_batch_id, new_values FROM audit_log "
        "WHERE operation = 'import' ORDER BY table_name"
    ).fetchall()
    assert [str(r[0]) for r in import_rows] == ["lab_draws", "lab_results"]
    assert all(r[1] is None for r in import_rows)  # row_id NULL on batch rows
    assert all(int(r[2]) == outcome.batch_id for r in import_rows)
    results_summary = json.loads(
        str(next(r[3] for r in import_rows if r[0] == "lab_results"))
    )
    assert results_summary["rows_inserted"] == 3
    assert results_summary["conflict_policy"] == "reject"
    assert results_summary["source"] == "manual"

    # Every imported row carries the batch provenance (ADR-0004/0027).
    assert (
        _one(
            conn,
            "SELECT count(*) FROM lab_results WHERE import_batch_id = ?",
            outcome.batch_id,
        )
        == 3
    )
    assert _one(conn, "SELECT count(*) FROM import_batches") == 1


# --------------------------------------------------------------------------
# Dry-run: full validation and truthful counts, but nothing is written
# --------------------------------------------------------------------------


def test_dry_run_writes_nothing(conn: sqlcipher3.Connection) -> None:
    _run(conn, {"lab_draws": [_draw()], "lab_results": [_result(1, 1)]}, imports.REJECT)
    audit_before = _audit_ops(conn)
    rows_before = _one(conn, "SELECT count(*) FROM lab_results")

    outcome = _run(
        conn,
        {"lab_draws": [_draw()], "lab_results": [_result(1, 1, value_num=999.0)]},
        imports.UPSERT,
        dry_run=True,
    )
    assert outcome.batch_id is None
    assert outcome.dry_run is True
    # The would-be correction is reported ...
    assert outcome.summaries["lab_results"].rows_corrected == 1
    # ... but nothing is written: no new rows, no batch row, no audit rows.
    assert _audit_ops(conn) == audit_before
    assert _one(conn, "SELECT count(*) FROM lab_results") == rows_before
    assert _one(conn, "SELECT count(*) FROM import_batches") == 1
    assert (
        _one(conn, "SELECT value_num FROM lab_results_current WHERE biomarker_id = 1")
        == 100.0
    )


# --------------------------------------------------------------------------
# Upsert: a genuine value difference supersedes (per-row correct audit)
# --------------------------------------------------------------------------


def test_upsert_supersedes_changed_value(conn: sqlcipher3.Connection) -> None:
    first = _run(
        conn, {"lab_draws": [_draw()], "lab_results": [_result(1, 1)]}, imports.REJECT
    )
    old_id = int(
        _one(conn, "SELECT id FROM lab_results_current WHERE biomarker_id = 1")
    )

    outcome = _run(
        conn,
        {"lab_draws": [_draw()], "lab_results": [_result(1, 1, value_num=110.0)]},
        imports.UPSERT,
    )
    assert outcome.summaries["lab_results"].rows_corrected == 1
    assert outcome.summaries["lab_draws"].rows_unchanged == 1

    # Supersession chain: old row points to the new current row; value updated.
    new_id = int(
        _one(conn, "SELECT id FROM lab_results_current WHERE biomarker_id = 1")
    )
    assert new_id != old_id
    assert (
        _one(conn, "SELECT superseded_by FROM lab_results WHERE id = ?", old_id)
        == new_id
    )
    assert (
        _one(conn, "SELECT value_num FROM lab_results_current WHERE biomarker_id = 1")
        == 110.0
    )
    assert _one(conn, "SELECT count(*) FROM lab_results") == 2  # both images kept

    # Exactly one 'correct' audit for the mutated row, with both images and
    # full provenance (actor, batch, auto reason).
    correct = conn.execute(
        "SELECT row_id, old_values, new_values, actor, import_batch_id, reason "
        "FROM audit_log WHERE operation = 'correct'"
    ).fetchall()
    assert len(correct) == 1
    row_id, old_values, new_values, actor, batch_id, reason = correct[0]
    assert int(row_id) == new_id
    assert json.loads(str(old_values))["value_num"] == 100.0
    assert json.loads(str(new_values))["value_num"] == 110.0
    assert str(actor) == "watch-import"
    assert int(batch_id) == outcome.batch_id
    assert str(reason) == f"upsert re-import, batch {outcome.batch_id}"
    assert first.batch_id != outcome.batch_id


def test_upsert_identical_row_is_a_noop(conn: sqlcipher3.Connection) -> None:
    _run(conn, {"lab_draws": [_draw()], "lab_results": [_result(1, 1)]}, imports.REJECT)
    outcome = _run(
        conn, {"lab_draws": [_draw()], "lab_results": [_result(1, 1)]}, imports.UPSERT
    )
    assert outcome.summaries["lab_results"].rows_unchanged == 1
    assert outcome.summaries["lab_results"].rows_corrected == 0
    # No supersession, no per-row audit — only the second batch's 'import' rows.
    assert _one(conn, "SELECT count(*) FROM lab_results") == 1
    assert _audit_ops(conn) == {"import": 4}


# --------------------------------------------------------------------------
# Skip: conflicts are left untouched
# --------------------------------------------------------------------------


def test_skip_leaves_conflicting_rows_unchanged(conn: sqlcipher3.Connection) -> None:
    _run(conn, {"lab_draws": [_draw()], "lab_results": [_result(1, 1)]}, imports.REJECT)
    outcome = _run(
        conn,
        {"lab_draws": [_draw()], "lab_results": [_result(1, 1, value_num=999.0)]},
        imports.SKIP,
    )
    assert outcome.summaries["lab_results"].rows_skipped == 1
    assert (
        _one(conn, "SELECT value_num FROM lab_results_current WHERE biomarker_id = 1")
        == 100.0
    )
    assert _one(conn, "SELECT count(*) FROM lab_results") == 1
    # No per-row audit for a skip; no 'correct' rows.
    assert "correct" not in _audit_ops(conn)


# --------------------------------------------------------------------------
# Reject: the first conflict fails the batch, but all errors are collected
# --------------------------------------------------------------------------


def test_reject_conflict_collects_all_errors_and_writes_nothing(
    conn: sqlcipher3.Connection,
) -> None:
    _run(
        conn,
        {"lab_draws": [_draw()], "lab_results": [_result(1, 1), _result(1, 2)]},
        imports.REJECT,
    )
    audit_before = _audit_ops(conn)
    rows_before = _one(conn, "SELECT count(*) FROM lab_results")
    batches_before = _one(conn, "SELECT count(*) FROM import_batches")

    with pytest.raises(imports.ImportValidationError) as excinfo:
        _run(
            conn,
            {
                "lab_draws": [_draw()],
                "lab_results": [
                    _result(1, 1, value_num=999.0),
                    _result(1, 2, value_num=888.0),
                ],
            },
            imports.REJECT,
        )
    # Both conflicts reported, not just the first.
    result_errors = [e for e in excinfo.value.errors if e.table == "lab_results"]
    assert len(result_errors) == 2
    # Nothing written: no rows, no batch row, no audit rows.
    assert _one(conn, "SELECT count(*) FROM lab_results") == rows_before
    assert _one(conn, "SELECT count(*) FROM import_batches") == batches_before
    assert _audit_ops(conn) == audit_before


# --------------------------------------------------------------------------
# Identity rows: a draw metadata difference updates in place (no supersession)
# --------------------------------------------------------------------------


def test_draw_metadata_upsert_updates_in_place(conn: sqlcipher3.Connection) -> None:
    _run(conn, {"lab_draws": [_draw()]}, imports.REJECT)
    draw_id = int(_one(conn, "SELECT id FROM lab_draws_current"))

    outcome = _run(
        conn,
        {"lab_draws": [_draw(fasting=0, draw_context="revised")]},
        imports.UPSERT,
    )
    assert outcome.summaries["lab_draws"].rows_corrected == 1

    # In place: same row id, no supersession, metadata updated.
    assert _one(conn, "SELECT count(*) FROM lab_draws") == 1
    assert _one(conn, "SELECT id FROM lab_draws_current") == draw_id
    assert (
        _one(conn, "SELECT superseded_by FROM lab_draws WHERE id = ?", draw_id) is None
    )
    assert _one(conn, "SELECT fasting FROM lab_draws_current") == 0
    assert _one(conn, "SELECT draw_context FROM lab_draws_current") == "revised"

    # One 'update' audit (not 'correct'), with both images.
    update = conn.execute(
        "SELECT row_id, old_values, new_values FROM audit_log "
        "WHERE operation = 'update'"
    ).fetchall()
    assert len(update) == 1
    assert int(update[0][0]) == draw_id
    assert json.loads(str(update[0][1]))["fasting"] == 1
    assert json.loads(str(update[0][2]))["fasting"] == 0
    assert "correct" not in _audit_ops(conn)


# --------------------------------------------------------------------------
# Validation collects every error before any write
# --------------------------------------------------------------------------


def test_validation_collects_all_errors(conn: sqlcipher3.Connection) -> None:
    with pytest.raises(imports.ImportValidationError) as excinfo:
        _run(
            conn,
            {
                "lab_draws": [_draw()],
                "lab_results": [
                    _result(1, 99),  # unknown biomarker (FK)
                    {"lab_draw_id": 1, "biomarker_id": 2},  # no value_num/value_text
                    _result(1, 3, comparator="<", value_num=None),  # comparator w/o num
                    _result(1, 4, bogus_col=1),  # unknown column
                    _result(2, 5),  # unresolvable draw handle
                ],
            },
            imports.REJECT,
        )
    messages = " ".join(e.message for e in excinfo.value.errors)
    assert "does not exist in biomarkers" in messages
    assert "value_num or value_text" in messages
    assert "comparator requires" in messages
    assert "unknown column" in messages
    assert "matches no lab_draws row" in messages
    assert len(excinfo.value.errors) >= 5
    # Nothing was written.
    assert _one(conn, "SELECT count(*) FROM lab_results") == 0
    assert _one(conn, "SELECT count(*) FROM import_batches") == 0


def test_within_batch_duplicate_natural_key_rejected(
    conn: sqlcipher3.Connection,
) -> None:
    with pytest.raises(imports.ImportValidationError) as excinfo:
        _run(
            conn,
            {
                "lab_draws": [_draw(handle=1), _draw(handle=2)]
            },  # same (lab_id, draw_utc)
            imports.REJECT,
        )
    assert any("duplicate natural key" in e.message for e in excinfo.value.errors)


def test_results_require_an_in_batch_draw_handle(conn: sqlcipher3.Connection) -> None:
    with pytest.raises(imports.ImportValidationError) as excinfo:
        _run(conn, {"lab_results": [_result(1, 1)]}, imports.REJECT)
    assert any("matches no lab_draws row" in e.message for e in excinfo.value.errors)


# --------------------------------------------------------------------------
# Value fidelity round-trips through import (ADR-0030)
# --------------------------------------------------------------------------


def test_censored_and_qualitative_values_round_trip(
    conn: sqlcipher3.Connection,
) -> None:
    _run(
        conn,
        {
            "lab_draws": [_draw()],
            "lab_results": [
                _result(1, 1, value_num=0.1, comparator="<"),  # below detection
                {"lab_draw_id": 1, "biomarker_id": 2, "value_text": "indeterminate"},
            ],
        },
        imports.REJECT,
    )
    censored = conn.execute(
        "SELECT value_num, comparator, value_text FROM lab_results_current "
        "WHERE biomarker_id = 1"
    ).fetchone()
    assert censored == (0.1, "<", None)  # comparator preserved, never a bare 0.1
    qualitative = conn.execute(
        "SELECT value_num, comparator, value_text FROM lab_results_current "
        "WHERE biomarker_id = 2"
    ).fetchone()
    assert qualitative == (None, None, "indeterminate")


# --------------------------------------------------------------------------
# The natural-key invariant is real schema (migration 0003)
# --------------------------------------------------------------------------


def test_partial_unique_index_blocks_a_duplicate_current_row(
    conn: sqlcipher3.Connection,
) -> None:
    _run(conn, {"lab_draws": [_draw()], "lab_results": [_result(1, 1)]}, imports.REJECT)
    draw_id = int(_one(conn, "SELECT lab_draw_id FROM lab_results_current LIMIT 1"))
    conn.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlcipher3.IntegrityError):
            conn.execute(
                "INSERT INTO lab_results (lab_draw_id, biomarker_id, value_num) "
                "VALUES (?, 1, 5)",
                (draw_id,),
            )
    finally:
        conn.execute("ROLLBACK")


# --------------------------------------------------------------------------
# Identity rows under reject / skip
# --------------------------------------------------------------------------


def test_draw_metadata_reject_conflict_fails(conn: sqlcipher3.Connection) -> None:
    _run(conn, {"lab_draws": [_draw()]}, imports.REJECT)
    with pytest.raises(imports.ImportValidationError) as excinfo:
        _run(conn, {"lab_draws": [_draw(fasting=0)]}, imports.REJECT)
    assert any(
        e.table == "lab_draws" and "already exists" in e.message
        for e in excinfo.value.errors
    )
    # Unchanged: the in-place update never ran.
    assert _one(conn, "SELECT fasting FROM lab_draws_current") == 1
    assert "update" not in _audit_ops(conn)


def test_draw_metadata_skip_keeps_existing(conn: sqlcipher3.Connection) -> None:
    _run(conn, {"lab_draws": [_draw()]}, imports.REJECT)
    outcome = _run(conn, {"lab_draws": [_draw(fasting=0)]}, imports.SKIP)
    assert outcome.summaries["lab_draws"].rows_skipped == 1
    assert _one(conn, "SELECT fasting FROM lab_draws_current") == 1  # kept
    assert _one(conn, "SELECT count(*) FROM lab_draws") == 1
    assert "update" not in _audit_ops(conn)


# --------------------------------------------------------------------------
# Atomicity: a failure mid-apply rolls the whole batch back (ADR-0004)
# --------------------------------------------------------------------------


def test_mid_apply_failure_rolls_everything_back(
    conn: sqlcipher3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(conn, {"lab_draws": [_draw()], "lab_results": [_result(1, 1)]}, imports.REJECT)
    audit_before = _audit_ops(conn)
    rows_before = _one(conn, "SELECT count(*) FROM lab_results")
    batches_before = _one(conn, "SELECT count(*) FROM import_batches")

    # Inject a failure after the rows are written but before commit: the
    # batch-audit write is the last step of applying a table.
    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("injected mid-apply failure")

    monkeypatch.setattr(audit, "record_import", _boom)
    with pytest.raises(RuntimeError, match="injected"):
        _run(
            conn,
            {
                "lab_draws": [_draw(handle=9, draw_utc="2024-09-09T00:00:00Z")],
                "lab_results": [_result(9, 2)],
            },
            imports.REJECT,
        )
    # Nothing from the failed batch survives: rows, batch row, audit all intact.
    assert _one(conn, "SELECT count(*) FROM lab_results") == rows_before
    assert _one(conn, "SELECT count(*) FROM import_batches") == batches_before
    assert _audit_ops(conn) == audit_before


# --------------------------------------------------------------------------
# Server-owned columns in a payload row are ignored (ADR-0052)
# --------------------------------------------------------------------------


def test_reserved_payload_columns_are_ignored(conn: sqlcipher3.Connection) -> None:
    outcome = _run(
        conn,
        {
            "lab_draws": [_draw()],
            # import_batch_id / superseded_by are server-owned: a client value
            # must not leak into the stored row.
            "lab_results": [
                _result(1, 1, import_batch_id=999, superseded_by=42),
            ],
        },
        imports.REJECT,
    )
    stored = conn.execute(
        "SELECT import_batch_id, superseded_by FROM lab_results_current "
        "WHERE biomarker_id = 1"
    ).fetchone()
    assert stored == (
        outcome.batch_id,
        None,
    )  # server batch id, not 999; not superseded


# --------------------------------------------------------------------------
# Reconciliation property (testing-strategy.md): the four summary counts
# partition every input row and reconcile against the real table deltas
# --------------------------------------------------------------------------


@settings(deadline=None, max_examples=30)
@given(
    policy=st.sampled_from([imports.REJECT, imports.SKIP, imports.UPSERT]),
    batch=st.lists(
        st.tuples(
            st.integers(min_value=1, max_value=6),
            st.sampled_from([100.0, 777.0]),
        ),
        unique_by=lambda pair: pair[0],  # one row per biomarker: no dup key
        max_size=6,
    ),
)
def test_import_summary_reconciles(policy: str, batch: list[tuple[int, float]]) -> None:
    with tempfile.TemporaryDirectory() as directory:
        conn = _build_db(Path(directory))
        try:
            # Baseline: biomarkers 1-3 exist at value 100.0.
            _run(
                conn,
                {
                    "lab_draws": [_draw()],
                    "lab_results": [_result(1, b) for b in (1, 2, 3)],
                },
                imports.REJECT,
            )
            current_before = int(_one(conn, "SELECT count(*) FROM lab_results_current"))
            total_before = int(_one(conn, "SELECT count(*) FROM lab_results"))

            # Expected partition, derived independently from the batch.
            baseline = {1: 100.0, 2: 100.0, 3: 100.0}
            inserted = corrected = unchanged = 0
            for biomarker, value in batch:
                if biomarker not in baseline:
                    inserted += 1
                elif value == baseline[biomarker]:
                    unchanged += 1
                elif policy == imports.UPSERT:
                    corrected += 1
                # SKIP leaves a differing row untouched; REJECT fails the batch.
            differing = sum(1 for b, v in batch if b in baseline and v != baseline[b])
            skipped = len(batch) - inserted - corrected - unchanged

            rows = [_result(1, b, value_num=v) for b, v in batch]
            payload = {"lab_draws": [_draw()], "lab_results": rows}

            if policy == imports.REJECT and differing > 0:
                # Any conflict fails the whole batch and writes nothing.
                with pytest.raises(imports.ImportValidationError):
                    _run(conn, payload, policy)
                after = int(_one(conn, "SELECT count(*) FROM lab_results"))
                assert after == total_before
                return

            outcome = _run(conn, payload, policy)
            summary = outcome.summaries.get("lab_results", imports.TableSummary())

            assert summary.rows_inserted == inserted
            assert summary.rows_corrected == corrected
            assert summary.rows_skipped == skipped
            assert summary.rows_unchanged == unchanged
            # Every input row lands in exactly one bucket.
            assert (
                summary.rows_inserted
                + summary.rows_corrected
                + summary.rows_skipped
                + summary.rows_unchanged
                == len(batch)
            )

            # Table deltas reconcile: a correction adds a superseding row, an
            # insert adds a current row, a skip/no-op adds nothing.
            current_after = int(_one(conn, "SELECT count(*) FROM lab_results_current"))
            total_after = int(_one(conn, "SELECT count(*) FROM lab_results"))
            superseded = int(
                _one(
                    conn,
                    "SELECT count(*) FROM lab_results WHERE superseded_by IS NOT NULL",
                )
            )
            assert current_after == current_before + inserted
            assert total_after == total_before + inserted + corrected
            assert superseded == corrected
        finally:
            db.close(conn)


@settings(deadline=None, max_examples=30)
@given(
    policy=st.sampled_from([imports.REJECT, imports.SKIP, imports.UPSERT]),
    batch=st.lists(
        st.tuples(
            st.sampled_from(
                ["prop_a", "prop_b", "prop_c", "prop_d", "prop_e", "prop_f"]
            ),
            st.sampled_from(["base", "changed"]),
        ),
        unique_by=lambda pair: pair[0],  # one row per name: no dup key in a batch
        max_size=6,
    ),
)
def test_catalog_import_summary_reconciles(
    policy: str, batch: list[tuple[str, str]]
) -> None:
    # The reconciliation property (testing-strategy.md) on the catalog
    # (``has_supersession=False``) path: the four counts partition every input
    # row and reconcile against the table delta — but a catalog table never
    # supersedes, so a genuine difference is an in-place UPDATE (the row count
    # grows only by inserts) and every row must hold its expected post-reconcile
    # value. ``categories`` stands in for the four catalog tables — they all
    # travel the identical generalized ``_reconcile`` branch.
    with tempfile.TemporaryDirectory() as directory:
        conn = _build_db(Path(directory))
        try:
            baseline = {"prop_a": "base", "prop_b": "base", "prop_c": "base"}
            _run(
                conn,
                {
                    "categories": [
                        {"name": n, "description": d} for n, d in baseline.items()
                    ]
                },
                imports.REJECT,
            )
            count_before = int(_one(conn, "SELECT count(*) FROM categories"))

            # Expected partition, derived independently from the batch.
            inserted = corrected = unchanged = 0
            for name, desc in batch:
                if name not in baseline:
                    inserted += 1
                elif desc == baseline[name]:
                    unchanged += 1
                elif policy == imports.UPSERT:
                    corrected += 1
                # SKIP leaves a differing row untouched; REJECT fails the batch.
            differing = sum(1 for n, d in batch if n in baseline and d != baseline[n])
            skipped = len(batch) - inserted - corrected - unchanged

            payload = {"categories": [{"name": n, "description": d} for n, d in batch]}

            if policy == imports.REJECT and differing > 0:
                # Any conflict fails the whole batch and writes nothing.
                with pytest.raises(imports.ImportValidationError):
                    _run(conn, payload, policy)
                assert (
                    int(_one(conn, "SELECT count(*) FROM categories")) == count_before
                )
                return

            outcome = _run(conn, payload, policy)
            summary = outcome.summaries.get("categories", imports.TableSummary())

            assert summary.rows_inserted == inserted
            assert summary.rows_corrected == corrected
            assert summary.rows_skipped == skipped
            assert summary.rows_unchanged == unchanged
            # Every input row lands in exactly one bucket.
            assert inserted + corrected + skipped + unchanged == len(batch)

            # No supersession: the row count grows only by inserts, and each row
            # holds the value its bucket implies (UPSERT applied the change,
            # SKIP kept the old value, an insert took the new one).
            assert (
                int(_one(conn, "SELECT count(*) FROM categories"))
                == count_before + inserted
            )
            for name, desc in batch:
                stored = _one(
                    conn, "SELECT description FROM categories WHERE name = ?", name
                )
                if name not in baseline:
                    assert stored == desc
                elif desc == baseline[name] or policy == imports.SKIP:
                    assert stored == baseline[name]
                else:  # UPSERT on a genuine difference
                    assert stored == desc
        finally:
            db.close(conn)


# --------------------------------------------------------------------------
# Catalog tables (Phase 3 WI-2, ADR-0055/0054): insert/update/skip/reject,
# no supersession, catalog updates audit as 'update' not 'correct', no
# per-row 'insert' audit rows (batch-level only, like every other table).
# --------------------------------------------------------------------------


def test_category_import_inserts_and_batch_audits_only(
    conn: sqlcipher3.Connection,
) -> None:
    outcome = _run(
        conn,
        {"categories": [{"name": "cat_new", "description": "a new category"}]},
        imports.REJECT,
    )
    assert outcome.summaries["categories"].rows_inserted == 1
    assert _one(conn, "SELECT count(*) FROM categories WHERE name = 'cat_new'") == 1
    # No superseded_by/import_batch_id columns exist on a catalog table; a
    # fresh insert is audited at batch level only, same as every other table.
    assert _audit_ops(conn) == {"import": 1}


def test_category_upsert_updates_in_place_not_correct(
    conn: sqlcipher3.Connection,
) -> None:
    _run(
        conn,
        {"categories": [{"name": "cat_new", "description": "old text"}]},
        imports.REJECT,
    )
    cat_id = int(_one(conn, "SELECT id FROM categories WHERE name = 'cat_new'"))

    outcome = _run(
        conn,
        {"categories": [{"name": "cat_new", "description": "new text"}]},
        imports.UPSERT,
    )
    assert outcome.summaries["categories"].rows_corrected == 1
    # Same row id: an in-place repair, never a supersession (there is no
    # superseded_by column on categories to supersede into).
    assert _one(conn, "SELECT id FROM categories WHERE name = 'cat_new'") == cat_id
    assert (
        _one(conn, "SELECT description FROM categories WHERE id = ?", cat_id)
        == "new text"
    )
    ops = _audit_ops(conn)
    assert ops.get("correct", 0) == 0
    update_rows = conn.execute(
        "SELECT row_id, old_values, new_values FROM audit_log "
        "WHERE table_name = 'categories' AND operation = 'update'"
    ).fetchall()
    assert len(update_rows) == 1
    assert int(update_rows[0][0]) == cat_id
    assert json.loads(str(update_rows[0][1]))["description"] == "old text"
    assert json.loads(str(update_rows[0][2]))["description"] == "new text"


def test_category_skip_leaves_existing_row(conn: sqlcipher3.Connection) -> None:
    _run(
        conn,
        {"categories": [{"name": "cat_new", "description": "old text"}]},
        imports.REJECT,
    )
    outcome = _run(
        conn,
        {"categories": [{"name": "cat_new", "description": "new text"}]},
        imports.SKIP,
    )
    assert outcome.summaries["categories"].rows_skipped == 1
    assert (
        _one(conn, "SELECT description FROM categories WHERE name = 'cat_new'")
        == "old text"
    )
    assert "update" not in _audit_ops(conn)


def test_category_reject_conflict_fails_and_writes_nothing(
    conn: sqlcipher3.Connection,
) -> None:
    _run(
        conn,
        {"categories": [{"name": "cat_new", "description": "old text"}]},
        imports.REJECT,
    )
    with pytest.raises(imports.ImportValidationError) as excinfo:
        _run(
            conn,
            {"categories": [{"name": "cat_new", "description": "new text"}]},
            imports.REJECT,
        )
    assert any(
        e.table == "categories" and "already exists" in e.message
        for e in excinfo.value.errors
    )
    assert (
        _one(conn, "SELECT description FROM categories WHERE name = 'cat_new'")
        == "old text"
    )


def test_labs_catalog_import_insert_and_update(conn: sqlcipher3.Connection) -> None:
    outcome = _run(
        conn, {"labs": [{"name": "New Lab", "description": None}]}, imports.REJECT
    )
    assert outcome.summaries["labs"].rows_inserted == 1
    lab_id = int(_one(conn, "SELECT id FROM labs WHERE name = 'New Lab'"))

    outcome = _run(
        conn,
        {"labs": [{"name": "New Lab", "description": "now with a description"}]},
        imports.UPSERT,
    )
    assert outcome.summaries["labs"].rows_corrected == 1
    assert _one(conn, "SELECT id FROM labs WHERE name = 'New Lab'") == lab_id
    assert "correct" not in _audit_ops(conn)


def test_biomarker_catalog_import_defaults_category_to_reserved(
    conn: sqlcipher3.Connection,
) -> None:
    outcome = _run(
        conn, {"biomarkers": [{"canonical_name": "New Marker"}]}, imports.REJECT
    )
    assert outcome.summaries["biomarkers"].rows_inserted == 1
    assert (
        _one(
            conn,
            "SELECT category_id FROM biomarkers WHERE canonical_name = ?",
            "New Marker",
        )
        == 0
    )


def test_biomarker_catalog_import_rejects_unknown_category(
    conn: sqlcipher3.Connection,
) -> None:
    with pytest.raises(imports.ImportValidationError) as excinfo:
        _run(
            conn,
            {"biomarkers": [{"canonical_name": "New Marker", "category_id": 999999}]},
            imports.REJECT,
        )
    assert any(
        e.table == "biomarkers" and "does not exist in categories" in e.message
        for e in excinfo.value.errors
    )


# --------------------------------------------------------------------------
# biomarker_aliases: server-derived alias_normalized/created_utc
# --------------------------------------------------------------------------


def test_biomarker_alias_import_derives_normalized_and_timestamp(
    conn: sqlcipher3.Connection,
) -> None:
    outcome = _run(
        conn,
        {"biomarker_aliases": [{"biomarker_id": 1, "alias": "  Total   Chol  "}]},
        imports.REJECT,
    )
    assert outcome.summaries["biomarker_aliases"].rows_inserted == 1
    row = conn.execute(
        "SELECT alias, alias_normalized, created_utc, source "
        "FROM biomarker_aliases WHERE biomarker_id = 1"
    ).fetchone()
    assert row is not None
    assert row[0] == "  Total   Chol  "
    assert row[1] == "total chol"  # NFKC -> casefold -> trim -> collapse
    assert row[2] is not None
    assert row[3] is None
    # No per-row 'insert' audit row; batch level only.
    assert _audit_ops(conn) == {"import": 1}


def test_biomarker_alias_import_ignores_client_supplied_derived_fields(
    conn: sqlcipher3.Connection,
) -> None:
    _run(
        conn,
        {
            "biomarker_aliases": [
                {
                    "biomarker_id": 1,
                    "alias": "Alt Name",
                    "alias_normalized": "malicious-override",
                    "created_utc": "1999-01-01T00:00:00Z",
                    "source": "import test",
                }
            ]
        },
        imports.REJECT,
    )
    row = conn.execute(
        "SELECT alias_normalized, created_utc, source FROM biomarker_aliases "
        "WHERE biomarker_id = 1"
    ).fetchone()
    assert row is not None
    assert row[0] == "alt name"  # server-derived, not the client's override
    assert row[1] != "1999-01-01T00:00:00Z"
    assert row[2] == "import test"  # a legitimate client-supplyable column


def test_biomarker_alias_missing_alias_is_a_validation_error(
    conn: sqlcipher3.Connection,
) -> None:
    with pytest.raises(imports.ImportValidationError) as excinfo:
        _run(conn, {"biomarker_aliases": [{"biomarker_id": 1}]}, imports.REJECT)
    assert any(
        e.table == "biomarker_aliases" and "required column 'alias'" in e.message
        for e in excinfo.value.errors
    )


def test_biomarker_alias_missing_biomarker_id_is_a_validation_error(
    conn: sqlcipher3.Connection,
) -> None:
    with pytest.raises(imports.ImportValidationError) as excinfo:
        _run(conn, {"biomarker_aliases": [{"alias": "Some Alias"}]}, imports.REJECT)
    assert any(
        e.table == "biomarker_aliases"
        and "missing required column 'biomarker_id'" in e.message
        for e in excinfo.value.errors
    )


def test_biomarker_alias_reimport_is_unchanged_and_preserves_created_utc(
    conn: sqlcipher3.Connection,
) -> None:
    # created_utc is server-derived but must be stamped once and preserved on
    # reconcile (a server_owned column, not compared) — otherwise an identical
    # re-import (the WI-4 confirm-and-record flow re-recording a known alias)
    # would be a spurious conflict/correction instead of `unchanged`.
    _run(
        conn,
        {"biomarker_aliases": [{"biomarker_id": 2, "alias": "B2 Alias"}]},
        imports.REJECT,
    )
    stamped = _one(
        conn,
        "SELECT created_utc FROM biomarker_aliases WHERE alias_normalized = ?",
        "b2 alias",
    )

    # A byte-identical re-import under every policy is `unchanged`, writes no
    # correction, and leaves created_utc exactly as first stamped.
    for policy in (imports.REJECT, imports.SKIP, imports.UPSERT):
        outcome = _run(
            conn,
            {"biomarker_aliases": [{"biomarker_id": 2, "alias": "B2 Alias"}]},
            policy,
        )
        summary = outcome.summaries["biomarker_aliases"]
        assert summary.rows_unchanged == 1
        assert summary.rows_corrected == 0
        assert summary.rows_inserted == 0
    assert (
        _one(
            conn,
            "SELECT created_utc FROM biomarker_aliases WHERE alias_normalized = ?",
            "b2 alias",
        )
        == stamped
    )
    assert "correct" not in _audit_ops(conn)
    assert "update" not in _audit_ops(conn)


def test_biomarker_alias_whitespace_only_is_rejected(
    conn: sqlcipher3.Connection,
) -> None:
    # A whitespace-only alias is a truthy string but normalizes to "" — reject
    # it rather than persist an empty alias_normalized (ADR-0054 §2). A
    # client-supplied alias_normalized must not smuggle the row past this either.
    with pytest.raises(imports.ImportValidationError) as excinfo:
        _run(
            conn,
            {
                "biomarker_aliases": [
                    {
                        "biomarker_id": 1,
                        "alias": "   ",
                        "alias_normalized": "smuggled",
                    }
                ]
            },
            imports.REJECT,
        )
    assert any(
        e.table == "biomarker_aliases" and "required column 'alias'" in e.message
        for e in excinfo.value.errors
    )
    assert _one(conn, "SELECT count(*) FROM biomarker_aliases") == 0


# --------------------------------------------------------------------------
# Cross-table normalized-name uniqueness (ADR-0054 §3)
# --------------------------------------------------------------------------


def test_alias_colliding_with_existing_canonical_name_is_rejected(
    conn: sqlcipher3.Connection,
) -> None:
    with pytest.raises(imports.ImportValidationError) as excinfo:
        _run(
            conn,
            {"biomarker_aliases": [{"biomarker_id": 2, "alias": "biomarker 1"}]},
            imports.REJECT,
        )
    assert any(
        e.table == "biomarker_aliases"
        and "collides with an existing biomarker canonical_name" in e.message
        for e in excinfo.value.errors
    )


def test_alias_equal_to_own_biomarkers_exact_canonical_spelling_is_rejected(
    conn: sqlcipher3.Connection,
) -> None:
    """The explicitly-called-out redundant case: aliasing a biomarker to its
    own exact canonical spelling — a strict subset of the general
    canonical-vs-alias collision rule."""
    with pytest.raises(imports.ImportValidationError) as excinfo:
        _run(
            conn,
            {"biomarker_aliases": [{"biomarker_id": 1, "alias": "Biomarker 1"}]},
            imports.REJECT,
        )
    assert any(
        e.table == "biomarker_aliases"
        and "collides with an existing biomarker canonical_name" in e.message
        for e in excinfo.value.errors
    )


def test_new_biomarker_colliding_with_existing_alias_is_rejected(
    conn: sqlcipher3.Connection,
) -> None:
    _run(
        conn,
        {"biomarker_aliases": [{"biomarker_id": 1, "alias": "Cholesterol Alt"}]},
        imports.REJECT,
    )
    with pytest.raises(imports.ImportValidationError) as excinfo:
        _run(
            conn,
            {"biomarkers": [{"canonical_name": "Cholesterol Alt"}]},
            imports.REJECT,
        )
    assert any(
        e.table == "biomarkers"
        and "collides with an existing biomarker_aliases entry" in e.message
        for e in excinfo.value.errors
    )


def test_in_batch_biomarker_canonical_name_collision_is_rejected(
    conn: sqlcipher3.Connection,
) -> None:
    with pytest.raises(imports.ImportValidationError) as excinfo:
        _run(
            conn,
            {
                "biomarkers": [
                    {"canonical_name": "New Marker"},
                    {"canonical_name": "new   marker"},
                ]
            },
            imports.REJECT,
        )
    assert any(
        e.table == "biomarkers"
        and "normalizes the same as another biomarker in this batch" in e.message
        for e in excinfo.value.errors
    )


def test_in_batch_alias_colliding_with_in_batch_biomarker_is_rejected(
    conn: sqlcipher3.Connection,
) -> None:
    with pytest.raises(imports.ImportValidationError) as excinfo:
        _run(
            conn,
            {
                "biomarkers": [{"canonical_name": "Brand New Marker"}],
                "biomarker_aliases": [{"biomarker_id": 1, "alias": "brand new marker"}],
            },
            imports.REJECT,
        )
    assert any(
        e.table == "biomarker_aliases"
        and "collides with a biomarker's canonical_name in this batch" in e.message
        for e in excinfo.value.errors
    )


# --------------------------------------------------------------------------
# lab_results.biomarker_name resolution (ADR-0054 §4)
# --------------------------------------------------------------------------


def test_lab_result_resolves_biomarker_name_canonical_hit(
    conn: sqlcipher3.Connection,
) -> None:
    outcome = _run(
        conn,
        {
            "lab_draws": [_draw()],
            "lab_results": [
                {"lab_draw_id": 1, "biomarker_name": "  biomarker 1 ", "value_num": 5.0}
            ],
        },
        imports.REJECT,
    )
    assert outcome.summaries["lab_results"].rows_inserted == 1
    assert _one(conn, "SELECT biomarker_id FROM lab_results_current") == 1


def test_lab_result_resolves_biomarker_name_alias_hit(
    conn: sqlcipher3.Connection,
) -> None:
    _run(
        conn,
        {"biomarker_aliases": [{"biomarker_id": 2, "alias": "B2 Alias"}]},
        imports.REJECT,
    )
    outcome = _run(
        conn,
        {
            "lab_draws": [_draw()],
            "lab_results": [
                {"lab_draw_id": 1, "biomarker_name": "b2 alias", "value_num": 5.0}
            ],
        },
        imports.REJECT,
    )
    assert outcome.summaries["lab_results"].rows_inserted == 1
    assert _one(conn, "SELECT biomarker_id FROM lab_results_current") == 2


def test_lab_result_unresolved_biomarker_name_is_a_validation_error(
    conn: sqlcipher3.Connection,
) -> None:
    with pytest.raises(imports.ImportValidationError) as excinfo:
        _run(
            conn,
            {
                "lab_draws": [_draw()],
                "lab_results": [
                    {
                        "lab_draw_id": 1,
                        "biomarker_name": "Nonexistent Marker",
                        "value_num": 1.0,
                    }
                ],
            },
            imports.REJECT,
        )
    assert any(
        e.table == "lab_results" and "did not resolve" in e.message
        for e in excinfo.value.errors
    )


def test_lab_result_both_biomarker_id_and_name_is_rejected(
    conn: sqlcipher3.Connection,
) -> None:
    with pytest.raises(imports.ImportValidationError) as excinfo:
        _run(
            conn,
            {
                "lab_draws": [_draw()],
                "lab_results": [
                    {
                        "lab_draw_id": 1,
                        "biomarker_id": 1,
                        "biomarker_name": "Biomarker 1",
                        "value_num": 1.0,
                    }
                ],
            },
            imports.REJECT,
        )
    assert any(
        e.table == "lab_results" and "exactly one of biomarker_id" in e.message
        for e in excinfo.value.errors
    )


def test_lab_result_neither_biomarker_id_nor_name_is_rejected(
    conn: sqlcipher3.Connection,
) -> None:
    with pytest.raises(imports.ImportValidationError) as excinfo:
        _run(
            conn,
            {
                "lab_draws": [_draw()],
                "lab_results": [{"lab_draw_id": 1, "value_num": 1.0}],
            },
            imports.REJECT,
        )
    assert any(
        e.table == "lab_results" and "exactly one of biomarker_id" in e.message
        for e in excinfo.value.errors
    )


def test_biomarker_name_does_not_resolve_against_a_same_batch_biomarker(
    conn: sqlcipher3.Connection,
) -> None:
    # The same-batch visibility rule (ADR-0057 §9): name resolution consults
    # only already-stored catalog rows, never a biomarkers row introduced
    # earlier in the same batch. A biomarker must land in a prior import before
    # a lab_results row can reference it by name — so this batch fails loud, and
    # (rejected atomically) writes nothing, not even the new biomarker.
    with pytest.raises(imports.ImportValidationError) as excinfo:
        _run(
            conn,
            {
                "biomarkers": [{"canonical_name": "Same Batch Marker"}],
                "lab_draws": [_draw()],
                "lab_results": [
                    {
                        "lab_draw_id": 1,
                        "biomarker_name": "Same Batch Marker",
                        "value_num": 1.0,
                    }
                ],
            },
            imports.REJECT,
        )
    assert any(
        e.table == "lab_results" and "did not resolve" in e.message
        for e in excinfo.value.errors
    )
    assert (
        _one(
            conn,
            "SELECT count(*) FROM biomarkers WHERE canonical_name = ?",
            "Same Batch Marker",
        )
        == 0
    )


def test_resolve_biomarker_name_fails_loud_on_an_ambiguous_match(
    conn: sqlcipher3.Connection,
) -> None:
    # The resolver's >1-match branch (ADR-0057 §5) is guarded-unreachable through
    # the ordinary import path by the cross-table uniqueness checks, so force the
    # ambiguous state directly with raw SQL (two byte-distinct canonical names
    # that normalize identically) and assert the resolver fails loud rather than
    # silently picking one.
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO biomarkers (canonical_name) VALUES ('Ambig Marker')")
    conn.execute("INSERT INTO biomarkers (canonical_name) VALUES ('ambig  marker')")
    conn.execute("COMMIT")
    assert imports.normalize_name("Ambig Marker") == imports.normalize_name(
        "ambig  marker"
    )
    with pytest.raises(ValueError, match="did not resolve to exactly one"):
        imports.resolve_biomarker_name(conn, "Ambig Marker")


def test_validation_failure_rolls_back_and_releases_the_write_lock(
    conn: sqlcipher3.Connection,
) -> None:
    # run_import now takes BEGIN IMMEDIATE before resolution/validation (so the
    # cross-table uniqueness check reads under the write lock, ADR-0057 §6). A
    # validation failure must still roll the transaction back and release the
    # lock — proven here by a subsequent valid import succeeding on the same
    # connection (a lingering open transaction would raise "cannot start a
    # transaction within a transaction").
    with pytest.raises(imports.ImportValidationError):
        _run(
            conn,
            {
                "lab_draws": [_draw()],
                "lab_results": [_result(1, 99)],  # unknown biomarker FK
            },
            imports.REJECT,
        )
    outcome = _run(
        conn, {"lab_draws": [_draw()], "lab_results": [_result(1, 1)]}, imports.REJECT
    )
    assert outcome.batch_id is not None
    assert outcome.summaries["lab_results"].rows_inserted == 1


def test_run_import_takes_the_write_lock_before_validating(tmp_path: Path) -> None:
    # ADR-0057 §6: BEGIN IMMEDIATE is taken *before* resolution/validation so
    # the cross-table uniqueness check reads under the write lock. Prove the
    # ordering deterministically: a second connection holds the write lock, and
    # a payload that WOULD fail validation (unknown biomarker 99) instead raises
    # 'database is locked' from the up-front BEGIN IMMEDIATE — never reaching
    # validation. Under the old ordering it would raise ImportValidationError.
    conn_a = _build_db(tmp_path)
    conn_b = db.connect(tmp_path / "healthspan.db", _key())
    try:
        conn_b.execute("PRAGMA busy_timeout = 100")  # fail fast, don't wait 5s
        conn_a.execute("BEGIN IMMEDIATE")  # conn_a holds the write lock
        with pytest.raises(sqlcipher3.OperationalError, match="locked"):
            _run(
                conn_b,
                {"lab_draws": [_draw()], "lab_results": [_result(1, 99)]},
                imports.REJECT,
            )
    finally:
        conn_a.execute("ROLLBACK")
        db.close(conn_a)
        db.close(conn_b)


# --------------------------------------------------------------------------
# framework_ranges: the nullable-key lifecycle (ADR-0005, ADR-0058 SS6)
#
# `effective_date` is the registry's first `nullable_key` column: NULL means
# "always current" (the common, dateless-default row), matched with `IS`
# rather than `=` so a re-import reconciles against its own stored self
# instead of colliding with the partial unique index (migration 0005 /
# ADR-0005).
# --------------------------------------------------------------------------


def test_framework_range_dateless_row_inserts(conn: sqlcipher3.Connection) -> None:
    fw_id = _new_framework(conn)
    outcome = _run(conn, {"framework_ranges": [_range_row(fw_id, 1)]}, imports.REJECT)
    assert outcome.summaries["framework_ranges"].rows_inserted == 1
    row = conn.execute(
        "SELECT effective_date FROM framework_ranges "
        "WHERE framework_id = ? AND biomarker_id = 1",
        (fw_id,),
    ).fetchone()
    assert row is not None
    assert row[0] is None


def test_framework_range_reimport_identical_dateless_row_is_unchanged(
    conn: sqlcipher3.Connection,
) -> None:
    # The exact bug IS-matching fixes: under the old `=` match, a dateless
    # row would never match its own stored self (`x = NULL` is NULL, never
    # true), so a re-import would try to INSERT a second row and collide
    # with the ADR-0005 partial unique index instead of reconciling.
    fw_id = _new_framework(conn)
    _run(conn, {"framework_ranges": [_range_row(fw_id, 1)]}, imports.REJECT)
    outcome = _run(conn, {"framework_ranges": [_range_row(fw_id, 1)]}, imports.REJECT)
    assert outcome.summaries["framework_ranges"].rows_unchanged == 1
    assert outcome.summaries["framework_ranges"].rows_inserted == 0
    assert (
        _one(
            conn,
            "SELECT count(*) FROM framework_ranges "
            "WHERE framework_id = ? AND biomarker_id = 1",
            fw_id,
        )
        == 1
    )


def test_framework_range_omitted_and_explicit_null_effective_date_are_same_key(
    conn: sqlcipher3.Connection,
) -> None:
    fw_id = _new_framework(conn)
    _run(conn, {"framework_ranges": [_range_row(fw_id, 1)]}, imports.REJECT)  # omitted
    outcome = _run(
        conn,
        {"framework_ranges": [_range_row(fw_id, 1, effective_date=None)]},  # explicit
        imports.UPSERT,
    )
    assert outcome.summaries["framework_ranges"].rows_unchanged == 1
    assert (
        _one(
            conn,
            "SELECT count(*) FROM framework_ranges "
            "WHERE framework_id = ? AND biomarker_id = 1",
            fw_id,
        )
        == 1
    )


def test_framework_range_dateless_upsert_corrects_range_high_in_place(
    conn: sqlcipher3.Connection,
) -> None:
    fw_id = _new_framework(conn)
    _run(
        conn,
        {"framework_ranges": [_range_row(fw_id, 1, range_high=100.0)]},
        imports.REJECT,
    )
    row_id = int(
        _one(
            conn,
            "SELECT id FROM framework_ranges "
            "WHERE framework_id = ? AND biomarker_id = 1",
            fw_id,
        )
    )

    outcome = _run(
        conn,
        {"framework_ranges": [_range_row(fw_id, 1, range_high=120.0)]},
        imports.UPSERT,
    )
    assert outcome.summaries["framework_ranges"].rows_corrected == 1

    # In place: same row id, no supersession (catalog tables have no
    # `superseded_by` column, ADR-0057), still exactly one row for the key.
    assert (
        _one(
            conn,
            "SELECT id FROM framework_ranges "
            "WHERE framework_id = ? AND biomarker_id = 1",
            fw_id,
        )
        == row_id
    )
    assert (
        _one(conn, "SELECT range_high FROM framework_ranges WHERE id = ?", row_id)
        == 120.0
    )
    assert (
        _one(
            conn,
            "SELECT count(*) FROM framework_ranges "
            "WHERE framework_id = ? AND biomarker_id = 1",
            fw_id,
        )
        == 1
    )
    assert "correct" not in _audit_ops(conn)


def test_framework_range_dated_row_is_distinct_from_dateless_default(
    conn: sqlcipher3.Connection,
) -> None:
    # ADR-0005's versioning model for free: a dated row is a distinct natural
    # key from the dateless default, so both coexist rather than reconcile.
    fw_id = _new_framework(conn)
    _run(conn, {"framework_ranges": [_range_row(fw_id, 1)]}, imports.REJECT)
    outcome = _run(
        conn,
        {"framework_ranges": [_range_row(fw_id, 1, effective_date="2025-01-01")]},
        imports.REJECT,
    )
    assert outcome.summaries["framework_ranges"].rows_inserted == 1
    assert (
        _one(
            conn,
            "SELECT count(*) FROM framework_ranges "
            "WHERE framework_id = ? AND biomarker_id = 1",
            fw_id,
        )
        == 2
    )


def test_framework_range_dateless_conflict_rejected_under_reject_policy(
    conn: sqlcipher3.Connection,
) -> None:
    fw_id = _new_framework(conn)
    _run(
        conn,
        {"framework_ranges": [_range_row(fw_id, 1, range_high=100.0)]},
        imports.REJECT,
    )
    with pytest.raises(imports.ImportValidationError) as excinfo:
        _run(
            conn,
            {"framework_ranges": [_range_row(fw_id, 1, range_high=120.0)]},
            imports.REJECT,
        )
    assert any(
        e.table == "framework_ranges" and "already exists" in e.message
        for e in excinfo.value.errors
    )
    assert (
        _one(
            conn,
            "SELECT range_high FROM framework_ranges "
            "WHERE framework_id = ? AND biomarker_id = 1",
            fw_id,
        )
        == 100.0
    )


# --------------------------------------------------------------------------
# framework_ranges: domain validation (_framework_range_errors, ADR-0005)
# --------------------------------------------------------------------------


def test_framework_range_missing_unit_is_rejected(conn: sqlcipher3.Connection) -> None:
    fw_id = _new_framework(conn)
    with pytest.raises(imports.ImportValidationError) as excinfo:
        _run(
            conn,
            {"framework_ranges": [_range_row(fw_id, 1, unit=None)]},
            imports.REJECT,
        )
    assert any(
        e.table == "framework_ranges" and "unit" in e.message
        for e in excinfo.value.errors
    )


@pytest.mark.parametrize(
    "bad_effective_date",
    [
        "2025-01-01T00:00:00Z",  # a timestamp, not a date-only value
        "2025-1-1",  # not zero-padded
        "not-a-date",
        20250101,  # not a string at all
    ],
)
def test_framework_range_invalid_effective_date_formats_are_rejected(
    conn: sqlcipher3.Connection, bad_effective_date: object
) -> None:
    fw_id = _new_framework(conn)
    with pytest.raises(imports.ImportValidationError) as excinfo:
        _run(
            conn,
            {
                "framework_ranges": [
                    _range_row(fw_id, 1, effective_date=bad_effective_date)
                ]
            },
            imports.REJECT,
        )
    assert any(
        e.table == "framework_ranges"
        and "effective_date must be an ISO-8601 date" in e.message
        for e in excinfo.value.errors
    )


def test_framework_range_valid_effective_date_is_accepted(
    conn: sqlcipher3.Connection,
) -> None:
    fw_id = _new_framework(conn)
    outcome = _run(
        conn,
        {"framework_ranges": [_range_row(fw_id, 1, effective_date="2025-01-01")]},
        imports.REJECT,
    )
    assert outcome.summaries["framework_ranges"].rows_inserted == 1


def test_framework_range_missing_all_of_low_high_text_is_rejected(
    conn: sqlcipher3.Connection,
) -> None:
    fw_id = _new_framework(conn)
    with pytest.raises(imports.ImportValidationError) as excinfo:
        _run(
            conn,
            {
                "framework_ranges": [
                    {"framework_id": fw_id, "biomarker_id": 1, "unit": "mg/dL"}
                ]
            },
            imports.REJECT,
        )
    assert any(
        e.table == "framework_ranges"
        and "at least one of range_low, range_high, or range_text" in e.message
        for e in excinfo.value.errors
    )


def test_framework_range_low_greater_than_high_is_rejected(
    conn: sqlcipher3.Connection,
) -> None:
    fw_id = _new_framework(conn)
    with pytest.raises(imports.ImportValidationError) as excinfo:
        _run(
            conn,
            {
                "framework_ranges": [
                    _range_row(fw_id, 1, range_low=150.0, range_high=100.0)
                ]
            },
            imports.REJECT,
        )
    assert any(
        e.table == "framework_ranges" and "must be <= range_high" in e.message
        for e in excinfo.value.errors
    )


def test_framework_range_text_only_is_valid(conn: sqlcipher3.Connection) -> None:
    # Both bounds NULL, range_text set: a legitimate non-numeric target.
    fw_id = _new_framework(conn)
    outcome = _run(
        conn,
        {
            "framework_ranges": [
                {
                    "framework_id": fw_id,
                    "biomarker_id": 1,
                    "unit": "mg/dL",
                    "range_text": "see practitioner notes",
                }
            ]
        },
        imports.REJECT,
    )
    assert outcome.summaries["framework_ranges"].rows_inserted == 1
    assert (
        _one(
            conn,
            "SELECT range_text FROM framework_ranges "
            "WHERE framework_id = ? AND biomarker_id = 1",
            fw_id,
        )
        == "see practitioner notes"
    )


# --------------------------------------------------------------------------
# framework_ranges: FKs and the ADR-0057 SS9 same-batch constraint
# --------------------------------------------------------------------------


def test_framework_range_unknown_framework_id_is_rejected(
    conn: sqlcipher3.Connection,
) -> None:
    with pytest.raises(imports.ImportValidationError) as excinfo:
        _run(conn, {"framework_ranges": [_range_row(999999, 1)]}, imports.REJECT)
    assert any(
        e.table == "framework_ranges"
        and "does not exist in range_frameworks" in e.message
        for e in excinfo.value.errors
    )


def test_framework_range_unknown_biomarker_id_is_rejected(
    conn: sqlcipher3.Connection,
) -> None:
    fw_id = _new_framework(conn)
    with pytest.raises(imports.ImportValidationError) as excinfo:
        _run(conn, {"framework_ranges": [_range_row(fw_id, 999999)]}, imports.REJECT)
    assert any(
        e.table == "framework_ranges" and "does not exist in biomarkers" in e.message
        for e in excinfo.value.errors
    )


def test_framework_range_same_batch_framework_reference_is_rejected(
    conn: sqlcipher3.Connection,
) -> None:
    # ADR-0057 SS9 (extended by ADR-0058 SS6): both framework_ranges FKs
    # resolve only against already-stored rows, never a row introduced
    # earlier in the same batch. There is no batch-local handle for a catalog
    # row (only lab_draws/lab_results get that), so a client attempting this
    # in one call can only guess at the not-yet-assigned id — which fails,
    # even when the guess is the exact id the row would receive.
    next_id = int(_one(conn, "SELECT COALESCE(MAX(id), 0) + 1 FROM range_frameworks"))
    with pytest.raises(imports.ImportValidationError) as excinfo:
        _run(
            conn,
            {
                "range_frameworks": [{"name": "same_batch_framework"}],
                "framework_ranges": [_range_row(next_id, 1)],
            },
            imports.REJECT,
        )
    assert any(
        e.table == "framework_ranges"
        and "does not exist in range_frameworks" in e.message
        for e in excinfo.value.errors
    )
    # Rejected atomically: not even the new framework survives.
    assert (
        _one(
            conn,
            "SELECT count(*) FROM range_frameworks WHERE name = 'same_batch_framework'",
        )
        == 0
    )

    # The identical reference succeeds once the framework lands in its own
    # PRIOR import — proving the failure above was the same-batch rule, not a
    # generically broken FK.
    _run(conn, {"range_frameworks": [{"name": "same_batch_framework"}]}, imports.REJECT)
    fw_id = int(
        _one(
            conn, "SELECT id FROM range_frameworks WHERE name = 'same_batch_framework'"
        )
    )
    assert fw_id == next_id
    outcome = _run(conn, {"framework_ranges": [_range_row(fw_id, 1)]}, imports.REJECT)
    assert outcome.summaries["framework_ranges"].rows_inserted == 1


# --------------------------------------------------------------------------
# nullable_key must not loosen any other table's required natural-key columns
# --------------------------------------------------------------------------


def test_nullable_key_does_not_loosen_categories_required_name(
    conn: sqlcipher3.Connection,
) -> None:
    with pytest.raises(imports.ImportValidationError) as excinfo:
        _run(conn, {"categories": [{"description": "no name"}]}, imports.REJECT)
    assert any(
        e.table == "categories" and "missing required column 'name'" in e.message
        for e in excinfo.value.errors
    )


def test_nullable_key_does_not_loosen_labs_required_name(
    conn: sqlcipher3.Connection,
) -> None:
    with pytest.raises(imports.ImportValidationError) as excinfo:
        _run(conn, {"labs": [{"description": "no name"}]}, imports.REJECT)
    assert any(
        e.table == "labs" and "missing required column 'name'" in e.message
        for e in excinfo.value.errors
    )


def test_nullable_key_does_not_loosen_lab_results_required_biomarker_id(
    conn: sqlcipher3.Connection,
) -> None:
    # lab_results has no nullable_key columns of its own; its natural key
    # (lab_draw_id, biomarker_id) minus the parent-fk-skipped lab_draw_id must
    # still require biomarker_id, exactly as before the framework_ranges
    # nullable_key addition.
    with pytest.raises(imports.ImportValidationError) as excinfo:
        _run(
            conn,
            {
                "lab_draws": [_draw()],
                "lab_results": [{"lab_draw_id": 1, "value_num": 1.0}],
            },
            imports.REJECT,
        )
    assert any(
        e.table == "lab_results"
        and "missing required column 'biomarker_id'" in e.message
        for e in excinfo.value.errors
    )
