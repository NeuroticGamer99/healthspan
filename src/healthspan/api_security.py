"""Bearer-token verification, scope enforcement, and route declaration
(ADR-0026/0040/0049).

Every Core Service route declares exactly one requirement — a named scope,
or the ``public`` marker for the unauthenticated liveness endpoint. An
undeclared route is a hard error at app assembly, so a scope-free endpoint
can never ship by omission (ADR-0026/ADR-0040).

A scoped declaration is a real FastAPI dependency: it verifies the bearer
against the ``tokens`` table (hash + ``compare_digest``), enforces the
scope, audits the outcome in ``auth_audit``, and touches ``last_used_utc``.
It is synchronous by design — FastAPI runs it on the AnyIO worker
threadpool, where the ADR-0037 thread-affine pool hands it this thread's
connection; the driver never runs on the event loop. The ``public`` marker
stays an async no-op so liveness never pays a thread hop or touches the
database (ADR-0040).

Denials are uniform (ADR-0026): every authentication failure — missing
header, malformed value, unknown token, revoked token — answers ``401``
with the same generic body; which one it was is recorded in ``auth_audit``,
never disclosed to the caller. An authenticated request lacking the
required scope answers ``403`` naming the token and the missing scope, with
no echo of request content. Failure rate limiting (the ``429`` path)
arrives with WI-2b.
"""

import time
from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Iterator
from typing import cast

from fastapi import Depends, FastAPI, HTTPException, Request, params
from fastapi.routing import APIRoute
from starlette.routing import BaseRoute, Route

from healthspan import tokens
from healthspan.service_runtime import ServiceRuntime

PUBLIC = "public"

# Uniform 401 (ADR-0026): no unknown-vs-revoked distinction leaks.
AUTH_FAILED_DETAIL = "authentication failed"
_AUTH_FAILED_HEADERS = {"WWW-Authenticate": "Bearer"}

# Liveness abuse cap (ADR-0049): 30 requests per rolling 1-second window per
# source address. Generous for every legitimate poller at one address
# combined; a bound on an unauthenticated flood against the key-holder.
LIVENESS_MAX_REQUESTS = 30
LIVENESS_WINDOW_SECONDS = 1.0


class ScopeRequirement:
    """A route's declared capability requirement (a scope name or ``public``).

    The base class carries the declaration that :func:`route_scope` and
    :func:`assert_all_routes_declared` introspect; subclasses provide the
    dependency body.
    """

    def __init__(self, scope: str) -> None:
        self.scope = scope


class _PublicMarker(ScopeRequirement):
    """The declared liveness exemption (ADR-0040): visibly, deliberately open.

    Async no-op: the one public route must stay O(1) on the event loop —
    no threadpool hop, no database.
    """

    def __init__(self) -> None:
        super().__init__(PUBLIC)

    async def __call__(self) -> None:
        return None


class _ScopedRequirement(ScopeRequirement):
    """Verify the bearer, enforce the scope, audit the outcome (ADR-0026)."""

    def __call__(self, request: Request) -> None:
        runtime = cast(ServiceRuntime, request.app.state.runtime)
        conn = runtime.pool.connection()
        source_addr = request.client.host if request.client else "unknown"
        endpoint = request.url.path
        method = request.method

        def deny_invalid() -> HTTPException:
            tokens.record_outcome(
                conn,
                token_name=tokens.INVALID_NAME,
                source_addr=source_addr,
                endpoint=endpoint,
                method=method,
                outcome=tokens.OUTCOME_DENIED_INVALID,
            )
            return HTTPException(
                status_code=401,
                detail=AUTH_FAILED_DETAIL,
                headers=_AUTH_FAILED_HEADERS,
            )

        presented = _bearer_value(request.headers.get("Authorization"))
        if presented is None:
            raise deny_invalid()
        record = tokens.look_up(conn, presented)
        if record is None:
            raise deny_invalid()
        if record.revoked:
            tokens.record_outcome(
                conn,
                token_name=record.name,
                source_addr=source_addr,
                endpoint=endpoint,
                method=method,
                outcome=tokens.OUTCOME_DENIED_REVOKED,
            )
            # Deliberately identical to the invalid-credential answer.
            raise HTTPException(
                status_code=401,
                detail=AUTH_FAILED_DETAIL,
                headers=_AUTH_FAILED_HEADERS,
            )
        if self.scope not in record.scopes:
            tokens.record_outcome(
                conn,
                token_name=record.name,
                source_addr=source_addr,
                endpoint=endpoint,
                method=method,
                outcome=tokens.OUTCOME_DENIED_SCOPE,
            )
            raise HTTPException(
                status_code=403,
                detail=(
                    f"token '{record.name}' lacks the required scope '{self.scope}'"
                ),
            )
        tokens.record_ok(
            conn, record, source_addr=source_addr, endpoint=endpoint, method=method
        )
        request.state.token = record


def _bearer_value(header: str | None) -> str | None:
    """The credential in an ``Authorization: Bearer …`` header, or ``None``."""
    if header is None:
        return None
    scheme, separator, value = header.partition(" ")
    if scheme.lower() != "bearer" or not separator or not value.strip():
        return None
    return value.strip()


def require(scope: str) -> params.Depends:
    """Declare a route's required scope; use in ``dependencies=[require(...)]``.

    Declaration-time validation: an unknown scope name is a typo that would
    otherwise deny every caller forever, so it fails at app assembly.
    """
    if scope == PUBLIC:
        return Depends(_PublicMarker())
    if scope not in tokens.SCOPES:
        raise ValueError(
            f"unknown scope {scope!r}; valid: {sorted(tokens.SCOPES)} "
            f"or the {PUBLIC!r} marker (ADR-0026)"
        )
    return Depends(_ScopedRequirement(scope))


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
