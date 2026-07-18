"""The read/query endpoints (ADR-0027, ADR-0053) — Phase 2 WI-4, extended
with reference-data resources in Phase 3 WI-2 (ADR-0055/ADR-0057) and
opt-in range-comparison enrichment in Phase 3 WI-3 (ADR-0058). ``?framework=``
on the lab-results list/get routes adds a ``range_comparison`` object to each
result row; absent the parameter, rows serialize exactly as before (the
ADR-0053 contract is unchanged). An unknown framework name is a `422`,
deliberately unlike `?category=`'s empty-page rule — see
:class:`healthspan.reads.FrameworkNotFoundError`.

Every route is a ``read``-scoped GET: list/get pairs over the current-state
views for the import-populated tables and over the catalog tables a client
needs to resolve their foreign keys and browse reference data — the
``*_PATH`` constants below are the authoritative resource set. List
responses are ``{"items": [...], "next_cursor": ...}``; pagination is
keyset, bounded by the server-enforced page cap (``service.page_cap``,
default 100) — the single enforcement point every client inherits
(api-reference.md, MCP tool-convention rule 3). A ``limit`` above the cap
clamps to it; the cap can only shrink a page, never grow one.

Handlers are synchronous (ADR-0037): FastAPI runs them on the AnyIO worker
threadpool, where the thread-affine pool hands each its thread's
connection. Reads write no audit rows — ``audit_log`` records mutations
(ADR-0027), ``auth_audit`` records authentication outcomes (ADR-0050).
"""

from typing import Literal, cast

from fastapi import APIRouter, HTTPException, Request

from healthspan import reads
from healthspan.api_security import require
from healthspan.service_runtime import ServiceRuntime

router = APIRouter()

LAB_DRAWS_PATH = "/v1/lab-draws"
LAB_RESULTS_PATH = "/v1/lab-results"
LABS_PATH = "/v1/labs"
BIOMARKERS_PATH = "/v1/biomarkers"
CATEGORIES_PATH = "/v1/categories"
BIOMARKER_ALIASES_PATH = "/v1/biomarker-aliases"
RANGE_FRAMEWORKS_PATH = "/v1/range-frameworks"
FRAMEWORK_RANGES_PATH = "/v1/framework-ranges"

Order = Literal["asc", "desc"]


def _effective_limit(request: Request, limit: int | None) -> int:
    """Clamp the requested page size to the server-enforced cap (ADR-0053)."""
    cap = _runtime(request).cfg.service.page_cap
    if limit is None:
        return cap
    if limit < 1:
        raise HTTPException(status_code=422, detail="limit must be >= 1")
    return min(limit, cap)


def _runtime(request: Request) -> ServiceRuntime:
    return cast(ServiceRuntime, request.app.state.runtime)


def _page(page: reads.Page) -> dict[str, object]:
    return {"items": page.items, "next_cursor": page.next_cursor}


def _found(row: reads.Row | None, what: str, row_id: int) -> reads.Row:
    if row is None:
        # Absent and superseded ids answer identically: the current view has
        # no such row (ADR-0027 readers consume current state by name).
        raise HTTPException(status_code=404, detail=f"no current {what} {row_id}")
    return row


@router.get(LAB_DRAWS_PATH, dependencies=[require("read")])
def list_lab_draws(
    request: Request,
    lab_id: int | None = None,
    draw_from: str | None = None,
    draw_to: str | None = None,
    order: Order = "desc",
    limit: int | None = None,
    cursor: str | None = None,
) -> dict[str, object]:
    try:
        page = reads.list_lab_draws(
            _runtime(request).pool.connection(),
            lab_id=lab_id,
            draw_from=draw_from,
            draw_to=draw_to,
            order=order,
            limit=_effective_limit(request, limit),
            cursor=cursor,
        )
    except reads.CursorError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _page(page)


@router.get(LAB_DRAWS_PATH + "/{row_id}", dependencies=[require("read")])
def get_lab_draw(request: Request, row_id: int) -> reads.Row:
    row = reads.get_lab_draw(_runtime(request).pool.connection(), row_id)
    return _found(row, "lab draw", row_id)


@router.get(LAB_RESULTS_PATH, dependencies=[require("read")])
def list_lab_results(
    request: Request,
    biomarker_id: int | None = None,
    lab_draw_id: int | None = None,
    lab_id: int | None = None,
    draw_from: str | None = None,
    draw_to: str | None = None,
    order: Order = "desc",
    limit: int | None = None,
    cursor: str | None = None,
    framework: str | None = None,
) -> dict[str, object]:
    try:
        page = reads.list_lab_results(
            _runtime(request).pool.connection(),
            biomarker_id=biomarker_id,
            lab_draw_id=lab_draw_id,
            lab_id=lab_id,
            draw_from=draw_from,
            draw_to=draw_to,
            order=order,
            limit=_effective_limit(request, limit),
            cursor=cursor,
            framework=framework,
        )
    except reads.CursorError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except reads.FrameworkNotFoundError as exc:
        raise HTTPException(
            status_code=422, detail=f"unknown framework: {exc}"
        ) from exc
    return _page(page)


@router.get(LAB_RESULTS_PATH + "/{row_id}", dependencies=[require("read")])
def get_lab_result(
    request: Request, row_id: int, framework: str | None = None
) -> reads.Row:
    try:
        row = reads.get_lab_result(
            _runtime(request).pool.connection(), row_id, framework=framework
        )
    except reads.FrameworkNotFoundError as exc:
        raise HTTPException(
            status_code=422, detail=f"unknown framework: {exc}"
        ) from exc
    return _found(row, "lab result", row_id)


@router.get(LABS_PATH, dependencies=[require("read")])
def list_labs(
    request: Request,
    order: Order = "asc",
    limit: int | None = None,
    cursor: str | None = None,
) -> dict[str, object]:
    try:
        page = reads.list_labs(
            _runtime(request).pool.connection(),
            order=order,
            limit=_effective_limit(request, limit),
            cursor=cursor,
        )
    except reads.CursorError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _page(page)


@router.get(LABS_PATH + "/{row_id}", dependencies=[require("read")])
def get_lab(request: Request, row_id: int) -> reads.Row:
    row = reads.get_lab(_runtime(request).pool.connection(), row_id)
    return _found(row, "lab", row_id)


@router.get(BIOMARKERS_PATH, dependencies=[require("read")])
def list_biomarkers(
    request: Request,
    category: str | None = None,
    order: Order = "asc",
    limit: int | None = None,
    cursor: str | None = None,
) -> dict[str, object]:
    try:
        page = reads.list_biomarkers(
            _runtime(request).pool.connection(),
            category=category,
            order=order,
            limit=_effective_limit(request, limit),
            cursor=cursor,
        )
    except reads.CursorError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _page(page)


@router.get(BIOMARKERS_PATH + "/{row_id}", dependencies=[require("read")])
def get_biomarker(request: Request, row_id: int) -> reads.Row:
    row = reads.get_biomarker(_runtime(request).pool.connection(), row_id)
    return _found(row, "biomarker", row_id)


@router.get(CATEGORIES_PATH, dependencies=[require("read")])
def list_categories(
    request: Request,
    order: Order = "asc",
    limit: int | None = None,
    cursor: str | None = None,
) -> dict[str, object]:
    try:
        page = reads.list_categories(
            _runtime(request).pool.connection(),
            order=order,
            limit=_effective_limit(request, limit),
            cursor=cursor,
        )
    except reads.CursorError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _page(page)


@router.get(CATEGORIES_PATH + "/{row_id}", dependencies=[require("read")])
def get_category(request: Request, row_id: int) -> reads.Row:
    row = reads.get_category(_runtime(request).pool.connection(), row_id)
    return _found(row, "category", row_id)


@router.get(BIOMARKER_ALIASES_PATH, dependencies=[require("read")])
def list_biomarker_aliases(
    request: Request,
    biomarker_id: int | None = None,
    order: Order = "asc",
    limit: int | None = None,
    cursor: str | None = None,
) -> dict[str, object]:
    try:
        page = reads.list_biomarker_aliases(
            _runtime(request).pool.connection(),
            biomarker_id=biomarker_id,
            order=order,
            limit=_effective_limit(request, limit),
            cursor=cursor,
        )
    except reads.CursorError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _page(page)


@router.get(BIOMARKER_ALIASES_PATH + "/{row_id}", dependencies=[require("read")])
def get_biomarker_alias(request: Request, row_id: int) -> reads.Row:
    row = reads.get_biomarker_alias(_runtime(request).pool.connection(), row_id)
    return _found(row, "biomarker alias", row_id)


@router.get(RANGE_FRAMEWORKS_PATH, dependencies=[require("read")])
def list_range_frameworks(
    request: Request,
    order: Order = "asc",
    limit: int | None = None,
    cursor: str | None = None,
) -> dict[str, object]:
    try:
        page = reads.list_range_frameworks(
            _runtime(request).pool.connection(),
            order=order,
            limit=_effective_limit(request, limit),
            cursor=cursor,
        )
    except reads.CursorError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _page(page)


@router.get(RANGE_FRAMEWORKS_PATH + "/{row_id}", dependencies=[require("read")])
def get_range_framework(request: Request, row_id: int) -> reads.Row:
    row = reads.get_range_framework(_runtime(request).pool.connection(), row_id)
    return _found(row, "range framework", row_id)


@router.get(FRAMEWORK_RANGES_PATH, dependencies=[require("read")])
def list_framework_ranges(
    request: Request,
    framework_id: int | None = None,
    biomarker_id: int | None = None,
    order: Order = "asc",
    limit: int | None = None,
    cursor: str | None = None,
) -> dict[str, object]:
    try:
        page = reads.list_framework_ranges(
            _runtime(request).pool.connection(),
            framework_id=framework_id,
            biomarker_id=biomarker_id,
            order=order,
            limit=_effective_limit(request, limit),
            cursor=cursor,
        )
    except reads.CursorError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _page(page)


@router.get(FRAMEWORK_RANGES_PATH + "/{row_id}", dependencies=[require("read")])
def get_framework_range(request: Request, row_id: int) -> reads.Row:
    row = reads.get_framework_range(_runtime(request).pool.connection(), row_id)
    return _found(row, "framework range", row_id)
