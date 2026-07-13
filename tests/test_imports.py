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
    """A migrated database with a seeded catalog (labs 1, biomarkers 1-6)."""
    path = directory / "healthspan.db"
    db.provision(path, _key())
    migrate.migrate_database(path, _key())
    conn = db.connect(path, _key())
    conn.execute("BEGIN IMMEDIATE")
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
