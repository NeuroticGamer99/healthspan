"""Request metrics: counters, middleware, and ``GET /v1/metrics``
(observability.md, ADR-0040).

Basic request metrics via ASGI middleware — no external metrics
infrastructure. The counters object is shared between the middleware
(writing on the event-loop thread) and the endpoint (reading on a worker
thread), so it carries its own lock. The endpoint requires the ``monitor``
scope: the payload is operational metadata, never health data, but it
profiles usage patterns of a health database (ADR-0040).
"""

import threading
from collections import Counter
from typing import cast

from fastapi import APIRouter, Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from healthspan.api_security import require
from healthspan.service_runtime import ServiceRuntime

router = APIRouter()

METRICS_PATH = "/v1/metrics"


class MetricsCounters:
    """Thread-safe request totals and per-status counts."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_status: Counter[str] = Counter()

    def record(self, status_code: int) -> None:
        with self._lock:
            self._by_status[str(status_code)] += 1

    def snapshot(self) -> tuple[int, dict[str, int]]:
        with self._lock:
            by_status = dict(self._by_status)
        return sum(by_status.values()), by_status


class MetricsMiddleware:
    """Count every HTTP response by status code (pure ASGI, observability.md)."""

    def __init__(self, app: ASGIApp, counters: MetricsCounters) -> None:
        self.app = app
        self.counters = counters

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_counted(message: Message) -> None:
            if message["type"] == "http.response.start":
                self.counters.record(int(message["status"]))
            await send(message)

        await self.app(scope, receive, send_counted)


@router.get(METRICS_PATH, dependencies=[require("monitor")])
def metrics(request: Request) -> dict[str, object]:
    """Request counts, statement count, and uptime (observability.md).

    ``active_jobs`` is a constant ``0`` until the job system lands
    (ADR-0012, Phase 4) — the field ships now so the response shape is
    stable for monitoring clients. ``db_query_count`` counts statements
    executed through the service's connection pool since startup.
    """
    runtime = cast(ServiceRuntime, request.app.state.runtime)
    counters = cast(MetricsCounters, request.app.state.metrics)
    requests_total, by_status = counters.snapshot()
    return {
        "requests_total": requests_total,
        "requests_by_status": by_status,
        "active_jobs": 0,
        "db_query_count": runtime.pool.statements_executed,
        "uptime_seconds": runtime.uptime_seconds(),
    }
