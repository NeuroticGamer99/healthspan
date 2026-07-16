"""The bulk import endpoint (ADR-0004/0027/0052).

``POST /v1/import`` is the platform's single validated write path: an
``import``-scoped client submits a per-table batch, the engine validates the
whole batch (collecting every error), and — if clean — applies it in one
atomic transaction with the caller's explicit conflict policy, writing the
audit trail in the same transaction. ``?dry_run=true`` validates and reports
the would-be counts without writing.

The route is a synchronous FastAPI handler: FastAPI runs it on the AnyIO
worker threadpool, where the ADR-0037 thread-affine pool hands it this
thread's connection and the driver never touches the event loop. The
authenticating token name (``request.state.token``, set by the verify
dependency) is the audit ``actor``.
"""

from typing import Any, Literal, cast

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from healthspan import imports, tokens
from healthspan.api_security import require
from healthspan.logging_setup import get_logger
from healthspan.service_runtime import ServiceRuntime

router = APIRouter()

IMPORT_PATH = "/v1/import"

_log = get_logger("healthspan.api_import")


class ImportRequest(BaseModel):
    """One bulk import batch: provenance, an explicit policy, per-table rows.

    ``conflict_policy`` is required — there is no implicit default that mutates
    data (ADR-0004). Unknown top-level keys are rejected (``extra='forbid'``),
    so a mistyped field or an unregistered table name is a clean ``422`` rather
    than a silently-ignored payload. Row objects are free-form maps; the engine
    validates their columns per table (ADR-0052).
    """

    model_config = ConfigDict(extra="forbid")

    source: str
    adapter_id: str | None = None
    adapter_version: str | None = None
    note: str | None = None
    conflict_policy: Literal["reject", "skip", "upsert"]
    categories: list[dict[str, Any]] = []
    labs: list[dict[str, Any]] = []
    biomarkers: list[dict[str, Any]] = []
    biomarker_aliases: list[dict[str, Any]] = []
    lab_draws: list[dict[str, Any]] = []
    lab_results: list[dict[str, Any]] = []


@router.post(IMPORT_PATH, dependencies=[require("import")])
def import_data(
    request: Request, body: ImportRequest, dry_run: bool = False
) -> dict[str, object]:
    """Validate and apply (or dry-run) a bulk import batch (ADR-0004/0052)."""
    runtime = cast(ServiceRuntime, request.app.state.runtime)
    token = cast(tokens.TokenRecord, request.state.token)
    batch = imports.BatchMeta(
        source=body.source,
        adapter_id=body.adapter_id,
        adapter_version=body.adapter_version,
        note=body.note,
    )
    payload: dict[str, list[dict[str, Any]]] = {
        "categories": body.categories,
        "labs": body.labs,
        "biomarkers": body.biomarkers,
        "biomarker_aliases": body.biomarker_aliases,
        "lab_draws": body.lab_draws,
        "lab_results": body.lab_results,
    }
    try:
        outcome = imports.run_import(
            runtime.pool.connection(),
            batch=batch,
            payload=payload,
            conflict_policy=body.conflict_policy,
            actor=token.name,
            dry_run=dry_run,
        )
    except imports.ImportValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail=[
                {"table": e.table, "row_index": e.row_index, "message": e.message}
                for e in exc.errors
            ],
        ) from exc
    _log.info(
        "import applied",
        batch_id=outcome.batch_id,
        dry_run=outcome.dry_run,
        conflict_policy=outcome.conflict_policy,
    )
    return {
        "batch_id": outcome.batch_id,
        "dry_run": outcome.dry_run,
        "conflict_policy": outcome.conflict_policy,
        "summary": {
            table: summary.as_dict() for table, summary in outcome.summaries.items()
        },
    }
