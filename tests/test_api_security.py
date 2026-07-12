"""Per-route scope declaration and the liveness rate cap (ADR-0026/0040/0049)."""

import pytest
from fastapi import APIRouter, FastAPI

from healthspan.api_security import (
    LIVENESS_MAX_REQUESTS,
    LIVENESS_WINDOW_SECONDS,
    PUBLIC,
    LivenessRateLimiter,
    assert_all_routes_declared,
    require,
)


def test_rate_limiter_allows_within_cap() -> None:
    now = [0.0]
    limiter = LivenessRateLimiter(
        max_requests=3, window_seconds=1.0, clock=lambda: now[0]
    )
    assert all(limiter.allow("addr-a") for _ in range(3))
    assert not limiter.allow("addr-a")  # 4th exceeds the cap
    assert limiter.allow("addr-b")  # a different address has its own bucket


def test_rate_limiter_evicts_after_window() -> None:
    now = [0.0]
    limiter = LivenessRateLimiter(
        max_requests=2, window_seconds=1.0, clock=lambda: now[0]
    )
    assert limiter.allow("a")
    assert limiter.allow("a")
    assert not limiter.allow("a")
    now[0] = 1.5  # window elapsed
    assert limiter.allow("a")


async def _handler() -> dict[str, str]:
    return {"status": "ok"}


def test_undeclared_route_is_a_hard_error() -> None:
    app = FastAPI(openapi_url=None)
    app.add_api_route("/leaky", _handler, methods=["GET"])
    with pytest.raises(RuntimeError, match="declares no scope"):
        assert_all_routes_declared(app)


def test_public_route_reported_through_included_router() -> None:
    app = FastAPI(openapi_url=None)
    router = APIRouter()
    router.add_api_route(
        "/v1/health", _handler, methods=["GET"], dependencies=[require(PUBLIC)]
    )
    app.include_router(router)
    assert assert_all_routes_declared(app) == ["/v1/health"]


def test_require_rejects_unknown_scopes_at_declaration() -> None:
    # A typo'd scope would deny every caller forever; it must fail at app
    # assembly, not at request time (ADR-0026 declare-every-route rule).
    with pytest.raises(ValueError, match="unknown scope"):
        require("reed")


def test_scoped_route_is_not_public() -> None:
    app = FastAPI(openapi_url=None)
    app.add_api_route(
        "/v1/data", _handler, methods=["GET"], dependencies=[require("read")]
    )
    assert assert_all_routes_declared(app) == []


def test_plain_non_api_route_is_rejected() -> None:
    # A default FastAPI() registers plain Starlette Routes for /openapi.json,
    # /docs, /redoc — undeclared and unauthenticated. The guard must reject
    # them so re-enabling docs cannot silently ship an unauthenticated route.
    with pytest.raises(RuntimeError, match="undeclared non-API HTTP route"):
        assert_all_routes_declared(FastAPI())  # openapi_url defaults to enabled


def test_liveness_rate_cap_defaults_match_adr_0049() -> None:
    assert LIVENESS_MAX_REQUESTS == 30
    assert LIVENESS_WINDOW_SECONDS == 1.0
    # The no-arg construction create_app uses must enforce those defaults.
    limiter = LivenessRateLimiter()
    assert all(limiter.allow("addr") for _ in range(30))
    assert not limiter.allow("addr")  # the 31st request exceeds the cap
