"""Per-route scope declaration and the liveness rate cap (ADR-0026/0040/0049).

Every Core Service route declares exactly one requirement — a named scope,
or the ``public`` marker for the unauthenticated liveness endpoint. An
undeclared route is a hard error at app assembly, so a scope-free endpoint
can never ship by omission (ADR-0026/ADR-0040). WI-1 lands the declaration
machinery and the ``public`` marker; WI-2 replaces the marker's no-op body
with real bearer verification and scope enforcement.
"""

import time
from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Iterator

from fastapi import Depends, FastAPI, params
from fastapi.routing import APIRoute
from starlette.routing import BaseRoute, Route

PUBLIC = "public"

# Liveness abuse cap (ADR-0049): 30 requests per rolling 1-second window per
# source address. Generous for every legitimate poller at one address
# combined; a bound on an unauthenticated flood against the key-holder.
LIVENESS_MAX_REQUESTS = 30
LIVENESS_WINDOW_SECONDS = 1.0


class ScopeRequirement:
    """A route's declared capability requirement (a scope name or ``public``).

    In WI-1 the dependency body is a no-op: it records intent only. WI-2
    turns it into the FastAPI dependency that verifies the bearer token and
    enforces the scope (ADR-0026).
    """

    def __init__(self, scope: str) -> None:
        self.scope = scope

    def __call__(self) -> None:  # pragma: no cover - trivial WI-1 no-op
        return None


def require(scope: str) -> params.Depends:
    """Declare a route's required scope; use in ``dependencies=[require(...)]``."""
    return Depends(ScopeRequirement(scope))


def route_scope(route: APIRoute) -> str | None:
    """The scope (or ``public``) a route declares, or ``None`` if it declares none."""
    for dep in route.dependant.dependencies:
        call = dep.call
        if isinstance(call, ScopeRequirement):
            return call.scope
    return None


def _flatten_routes(routes: Iterable[BaseRoute]) -> Iterator[BaseRoute]:
    """Yield leaf routes, descending through included routers and mounts.

    Recent FastAPI wraps ``include_router`` results in an ``_IncludedRouter``
    rather than flattening into ``app.routes``; this recurses through that
    wrapper (via its ``original_router``) and ``Mount``/``Host`` sub-routers
    structurally, without importing the private wrapper class. ``APIRoute``s
    and plain Starlette ``Route``s are leaves.
    """
    for route in routes:
        included = getattr(route, "original_router", None)
        sub = getattr(included, "routes", None) if included is not None else None
        if sub is None and not isinstance(route, APIRoute):
            sub = getattr(route, "routes", None)  # Starlette Mount / Host
        if sub:
            yield from _flatten_routes(sub)
        else:
            yield route


def assert_all_routes_declared(app: FastAPI) -> list[str]:
    """Enforce that every route declares a scope or ``public``.

    Returns the paths of the ``public`` routes so the caller (and the security
    test, testing-strategy.md) can assert they are exactly the liveness
    endpoints. Raises if an ``APIRoute`` is undeclared, or if a plain Starlette
    ``Route`` (FastAPI's ``/docs``/``/redoc``/``/openapi.json`` are exactly
    this) has slipped in — those carry no scope declaration and are
    unauthenticated by construction, so none may ship (ADR-0026/0040/0049).
    """
    public_paths: list[str] = []
    for route in _flatten_routes(app.routes):
        if isinstance(route, APIRoute):
            scope = route_scope(route)
            if scope is None:
                raise RuntimeError(
                    f"route {route.path} declares no scope or 'public' marker; "
                    "every route must declare one (ADR-0026/0040)"
                )
            if scope == PUBLIC:
                public_paths.append(route.path)
        elif isinstance(route, Route):
            raise RuntimeError(
                f"undeclared non-API HTTP route {route.path}; the Core Service "
                "serves only declared APIRoutes (OpenAPI and the docs UIs stay "
                "disabled in Phase 2, ADR-0049)"
            )
    return public_paths


class LivenessRateLimiter:
    """In-memory per-source-address request-rate cap for the liveness route.

    Accessed only from the event-loop thread (the liveness handler is
    ``async``), so no lock is needed. ``clock`` is injectable for tests.
    """

    def __init__(
        self,
        max_requests: int = LIVENESS_MAX_REQUESTS,
        window_seconds: float = LIVENESS_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max = max_requests
        self._window = window_seconds
        self._clock = clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, address: str) -> bool:
        """Record a request from ``address``; ``False`` if it exceeds the cap."""
        now = self._clock()
        hits = self._hits[address]
        cutoff = now - self._window
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if len(hits) >= self._max:
            return False
        hits.append(now)
        return True
