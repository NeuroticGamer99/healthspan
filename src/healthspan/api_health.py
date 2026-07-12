"""Liveness and health-detail endpoints (ADR-0040, observability.md).

``GET /v1/health`` is the platform's only unauthenticated route. It answers
from a cached readiness flag — never a database query (ADR-0037) — with a
status word and nothing else: no version, ``schema_version``, or uptime.

``GET /v1/health/detail`` is the authenticated rich answer (``monitor``
scope): version, ``schema_version``, database connectivity, and uptime —
fingerprinting material that has no business being free (ADR-0040). It is a
synchronous endpoint: it really queries the database (through the ADR-0037
pool, on a worker thread), which is exactly what ``db_connected`` claims.
"""

from typing import cast

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from healthspan.api_security import PUBLIC, LivenessRateLimiter, require
from healthspan.service_runtime import ServiceRuntime

router = APIRouter()

LIVENESS_PATH = "/v1/health"
HEALTH_DETAIL_PATH = "/v1/health/detail"


@router.get(LIVENESS_PATH, dependencies=[require(PUBLIC)])
async def liveness(request: Request) -> Response:
    """Report readiness as a bare status word (`200` ready / `503` not)."""
    limiter = cast(LivenessRateLimiter, request.app.state.liveness_limiter)
    address = request.client.host if request.client is not None else "unknown"
    if not limiter.allow(address):
        return JSONResponse({"status": "unavailable"}, status_code=429)
    runtime = cast(ServiceRuntime, request.app.state.runtime)
    if runtime.ready:
        return JSONResponse({"status": "ok"}, status_code=200)
    return JSONResponse({"status": "unavailable"}, status_code=503)


@router.get(HEALTH_DETAIL_PATH, dependencies=[require("monitor")])
def health_detail(request: Request) -> dict[str, object]:
    """The authenticated rich health answer (ADR-0040, observability.md)."""
    runtime = cast(ServiceRuntime, request.app.state.runtime)
    db_connected = runtime.pool.ping()
    return {
        "status": "healthy" if (runtime.ready and db_connected) else "unhealthy",
        "version": request.app.version,
        "schema_version": runtime.schema_version,
        "db_connected": db_connected,
        "uptime_seconds": runtime.uptime_seconds(),
    }
