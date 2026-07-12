"""Unauthenticated liveness endpoint (ADR-0040, observability.md).

``GET /v1/health`` is the platform's only unauthenticated route. It answers
from a cached readiness flag — never a database query (ADR-0037) — with a
status word and nothing else: no version, ``schema_version``, or uptime.
Those move behind the ``monitor`` scope in WI-2's ``/v1/health/detail``.
"""

from typing import cast

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from healthspan.api_security import PUBLIC, LivenessRateLimiter, require
from healthspan.service_runtime import ServiceRuntime

router = APIRouter()

LIVENESS_PATH = "/v1/health"


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
