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
no echo of request content.

Failed authentication is rate-limited (ADR-0026 rules 1-4, defaults
ADR-0051): the limiter throttles *failures only* — a valid credential is
never delayed — in buckets keyed by (source address, advisory token-name
prefix), with a per-address aggregate cap so name-cycling does not evade
it. A throttled failure answers ``429`` with a ``Retry-After`` header and
is audited with the ``rate-limited`` outcome. State is in-memory only:
a restart clears it, and ``POST /v1/auth/reset-limits`` clears it on
demand (always reachable, because valid admin credentials never throttle).
"""

import math
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import NoReturn, cast

import sqlcipher3
from fastapi import Depends, FastAPI, HTTPException, Request, params
from fastapi.routing import APIRoute
from starlette.routing import BaseRoute, Route

from healthspan import tokens
from healthspan.service_runtime import ServiceRuntime

PUBLIC = "public"

# Uniform 401 (ADR-0026): no unknown-vs-revoked distinction leaks.
AUTH_FAILED_DETAIL = "authentication failed"
_AUTH_FAILED_HEADERS = {"WWW-Authenticate": "Bearer"}

# 429 (ADR-0026): revealing that the limiter fired discloses nothing about
# token state, so the body may say what happened — and nothing else.
RATE_LIMITED_DETAIL = "too many failed authentication attempts"

# ADR-0026 rule 3: the per-address aggregate threshold is a fixed multiple
# of the per-bucket one (ADR-0051) — high enough that only name-cycling
# trips it, never one misconfigured client.
ADDRESS_THRESHOLD_MULTIPLIER = 5

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


@dataclass
class _FailureBucket:
    failures: int = 0
    blocked_until: float = 0.0


@dataclass
class _AddressState:
    buckets: dict[str, _FailureBucket] = field(
        default_factory=dict[str, _FailureBucket]
    )
    blocked_until: float = 0.0

    def total_failures(self) -> int:
        return sum(bucket.failures for bucket in self.buckets.values())


class AuthFailureRateLimiter:
    """Exponential backoff on failed authentication (ADR-0026 rules 1-4).

    Failures only: callers consult it exclusively on the failure path, so a
    valid credential is never delayed or rejected. Buckets key on (source
    address, advisory token-name prefix) — parsed from ``hsp_<name>_…``,
    unparseable credentials sharing one ``invalid`` bucket per address — and
    a per-address aggregate cap catches name-cycling. Backoff starts after
    ``failure_threshold`` free failures at 1 s, doubles per failure, and
    caps at ``max_backoff_seconds`` (default 60, ADR-0026 rule 4).

    State is in-memory and deliberately unpersisted (ADR-0051): the cap
    bounds a misconfigured client's recovery, a restart clears everything,
    and :meth:`reset` backs ``auth reset-limits``. Thread-safe — the verify
    dependency runs on the AnyIO worker threadpool.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        max_backoff_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = failure_threshold
        self._max_backoff = max_backoff_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._addresses: dict[str, _AddressState] = {}

    def retry_after(self, address: str, name: str) -> float | None:
        """Seconds until this bucket may fail again, or ``None`` if unblocked."""
        now = self._clock()
        with self._lock:
            state = self._addresses.get(address)
            if state is None:
                return None
            bucket = state.buckets.get(name)
            blocked_until = max(
                state.blocked_until, bucket.blocked_until if bucket else 0.0
            )
        remaining = blocked_until - now
        return remaining if remaining > 0 else None

    def record_failure(self, address: str, name: str) -> None:
        """Count a failed authentication and (re-)arm the backoff windows."""
        now = self._clock()
        with self._lock:
            state = self._addresses.setdefault(address, _AddressState())
            bucket = state.buckets.setdefault(name, _FailureBucket())
            bucket.failures += 1
            delay = self._backoff(bucket.failures - self._threshold)
            if delay > 0:
                bucket.blocked_until = max(bucket.blocked_until, now + delay)
            aggregate_threshold = self._threshold * ADDRESS_THRESHOLD_MULTIPLIER
            delay = self._backoff(state.total_failures() - aggregate_threshold)
            if delay > 0:
                state.blocked_until = max(state.blocked_until, now + delay)

    def record_success(self, address: str, name: str) -> None:
        """Clear the bucket a now-valid credential was failing in (rule 1).

        The cleared failures also leave the address aggregate, so a client
        recovering from a rotated-out token stops counting against its
        neighbors at the same address.
        """
        with self._lock:
            state = self._addresses.get(address)
            if state is None:
                return
            state.buckets.pop(name, None)
            if not state.buckets and state.blocked_until <= self._clock():
                del self._addresses[address]

    def reset(self) -> None:
        """Clear all limiter state (``auth reset-limits``, ADR-0026 rule 4)."""
        with self._lock:
            self._addresses.clear()

    def _backoff(self, failures_over: int) -> float:
        """1 s doubling per failure past the threshold, capped (ADR-0051)."""
        if failures_over <= 0:
            return 0.0
        exponent = min(failures_over - 1, 63)  # 2**63 s already dwarfs any cap
        return min(float(2**exponent), self._max_backoff)


class _ScopedRequirement(ScopeRequirement):
    """Verify the bearer, enforce the scope, audit the outcome (ADR-0026)."""

    def __call__(self, request: Request) -> None:
        runtime = cast(ServiceRuntime, request.app.state.runtime)
        limiter = cast(AuthFailureRateLimiter, request.app.state.auth_limiter)
        conn = runtime.pool.connection()
        source_addr = request.client.host if request.client else "unknown"
        endpoint = request.url.path
        method = request.method

        presented = _bearer_value(request.headers.get("Authorization"))
        record = tokens.look_up(conn, presented) if presented is not None else None

        if record is None or record.revoked:
            self._deny_failure(conn, limiter, presented, record, source_addr, request)
        # A valid credential is never throttled (ADR-0026 rule 1); its bucket
        # clears so a client recovering from a stale token starts clean.
        limiter.record_success(source_addr, record.name)
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

    def _deny_failure(
        self,
        conn: sqlcipher3.Connection,
        limiter: AuthFailureRateLimiter,
        presented: str | None,
        record: tokens.TokenRecord | None,
        source_addr: str,
        request: Request,
    ) -> NoReturn:
        """Audit and answer an authentication *failure*, throttled or not.

        The bucket keys on the advisory name prefix (ADR-0026 rule 2 —
        attacker-supplied text spreads failures across buckets, which the
        address aggregate catches); the audit row's ``token_name`` never
        takes it (ADR-0050 §3): unrecognized credentials audit as
        ``invalid``, a recognized-but-revoked token under its server-side
        name.
        """
        endpoint = request.url.path
        method = request.method
        bucket = None if presented is None else tokens.parse_name(presented)
        bucket_name = bucket if bucket is not None else tokens.INVALID_NAME
        audit_name = record.name if record is not None else tokens.INVALID_NAME

        retry_after = limiter.retry_after(source_addr, bucket_name)
        limiter.record_failure(source_addr, bucket_name)
        if retry_after is not None:
            tokens.record_outcome(
                conn,
                token_name=audit_name,
                source_addr=source_addr,
                endpoint=endpoint,
                method=method,
                outcome=tokens.OUTCOME_RATE_LIMITED,
            )
            raise HTTPException(
                status_code=429,
                detail=RATE_LIMITED_DETAIL,
                headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
            )
        tokens.record_outcome(
            conn,
            token_name=audit_name,
            source_addr=source_addr,
            endpoint=endpoint,
            method=method,
            outcome=(
                tokens.OUTCOME_DENIED_REVOKED
                if record is not None
                else tokens.OUTCOME_DENIED_INVALID
            ),
        )
        # Deliberately identical for unknown and revoked (ADR-0026).
        raise HTTPException(
            status_code=401,
            detail=AUTH_FAILED_DETAIL,
            headers=_AUTH_FAILED_HEADERS,
        )


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
