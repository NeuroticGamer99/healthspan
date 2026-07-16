"""Core Service startup, app assembly, and the direct-start entry (Phase 2 WI-1).

``healthspan service start`` runs the Core Service in the foreground
(ADR-0039 direct-start): it collects the master passphrase over a sanctioned
channel — TTY prompt, stdin pipe, or an OS-secret ``passphrase_file``, never
argv or an environment variable — derives and *retains* the database key for
the process lifetime (INV-1, ADR-0028), verifies the schema version and
refuses on a mismatch (ADR-0039), and holds the single-instance advisory
lock (ADR-0042) while it serves.

The concurrency model is ADR-0037: liveness is ``async`` and answers from a
cached flag; the AnyIO worker threadpool (where a synchronous repository
will run in later WIs) is capped to 8. The launcher, full-auto-unlock, and
supervision are later phases (ADR-0049).
"""

import getpass
import sys
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from importlib.metadata import version as _dist_version
from pathlib import Path
from typing import TextIO, cast
from uuid import uuid4

import sqlcipher3
import structlog
import uvicorn
from anyio import to_thread
from fastapi import FastAPI
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from healthspan import db, keychain, migrate, recovery_kit, rotation, token_bootstrap
from healthspan import tokens as tokens_module
from healthspan.api_health import LIVENESS_PATH
from healthspan.api_health import router as health_router
from healthspan.api_import import router as import_router
from healthspan.api_metrics import MetricsCounters, MetricsMiddleware
from healthspan.api_metrics import router as metrics_router
from healthspan.api_read import router as read_router
from healthspan.api_security import (
    AuthFailureRateLimiter,
    LivenessRateLimiter,
    assert_all_routes_declared,
)
from healthspan.api_tokens import router as tokens_router
from healthspan.config import Config
from healthspan.kdf import DbKey
from healthspan.locking import InstanceLock, InstanceLockHeldError
from healthspan.logging_setup import PROCESS_CORE_SERVICE, configure_logging, get_logger
from healthspan.pool import ConnectionPool
from healthspan.service_runtime import ServiceRuntime

# ADR-0037: a deliberately small worker threadpool — pool size equals
# connection count; more threads buy write contention, not throughput.
THREADPOOL_LIMIT = 8

_log = get_logger("healthspan.service")


class ServiceStartupError(Exception):
    """The Core Service could not start; nothing is left serving."""


# --------------------------------------------------------------------------
# Passphrase channels (ADR-0039 direct-start)
# --------------------------------------------------------------------------


def resolve_passphrase(
    cfg: Config,
    passphrase_file_flag: Path | None = None,
    *,
    stdin: TextIO | None = None,
    prompt: object = None,
) -> str:
    """Collect the master passphrase over a sanctioned channel (ADR-0039).

    ADR-0039 direct-start channel order, verbatim: an interactive **TTY
    prompt**, else a **one-line stdin pipe**, else a **``passphrase_file``**
    (the ``--passphrase-file`` flag taking precedence over the config key
    within that one file tier), else fail. The passphrase is never read from
    an environment variable. ``stdin``/``prompt`` are injectable for tests.
    """
    source = stdin if stdin is not None else sys.stdin
    ask = prompt if callable(prompt) else _tty_prompt

    if source.isatty():
        return str(ask())
    line = source.readline()
    if line != "":
        return line.rstrip("\r\n")
    passphrase_file = passphrase_file_flag or cfg.service.passphrase_file
    if passphrase_file is not None:
        return _read_passphrase_file(passphrase_file)
    raise ServiceStartupError(
        "no passphrase channel available: attach a TTY, pipe the passphrase "
        "on stdin, or set service.passphrase_file (or --passphrase-file). "
        "The passphrase is never read from an environment variable (ADR-0039)."
    )


def _tty_prompt() -> str:
    return getpass.getpass("Master passphrase: ")


def _read_passphrase_file(path: Path) -> str:
    try:
        content = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ServiceStartupError(
            f"could not read passphrase file {path}: {exc}"
        ) from exc
    return content.split("\n", 1)[0].rstrip("\r")


# --------------------------------------------------------------------------
# Startup: lock, unlock, schema check
# --------------------------------------------------------------------------


def build_runtime(
    cfg: Config, passphrase_file_flag: Path | None = None
) -> ServiceRuntime:
    """Acquire the lock, derive and retain the key, and verify the schema.

    On any failure the lock is released and the derived key zeroized, so a
    refused start leaves nothing held and nothing serving. On success the
    returned runtime owns the lock and key until the app lifespan tears them
    down.
    """
    lock = InstanceLock(cfg.database.path)
    try:
        lock.acquire()
    except InstanceLockHeldError as exc:
        _log.critical("startup aborted: database already in use", error=str(exc))
        raise ServiceStartupError(str(exc)) from exc

    key: DbKey | None = None
    try:
        for orphan in recovery_kit.sweep_orphans(cfg.database.path.parent):
            _log.warning("disposed orphaned recovery-kit plaintext", path=str(orphan))
        passphrase = resolve_passphrase(cfg, passphrase_file_flag)
        key = rotation.unlock(cfg, passphrase).key
        schema = verify_schema(cfg, key)
    except BaseException:
        if key is not None:
            key.zeroize()
        lock.release()
        raise
    return ServiceRuntime(
        cfg=cfg,
        key=key,
        lock=lock,
        pool=ConnectionPool(cfg.database.path, key),
        schema_version=schema,
    )


def verify_schema(cfg: Config, key: DbKey) -> int:
    """Refuse to serve against a schema this build does not expect (ADR-0039).

    Also asserts the ADR-0055 reserved ``not_assigned`` category row (id 0)
    is present: ``foreign_key_check`` proves every ``category_id`` points at
    an existing row but not that id 0 itself exists, so a missing reserved
    row must fail loudly here, at open, rather than surfacing as a confusing
    far-from-cause FK failure mid-import.

    Returns the verified schema version (also this build's target)."""
    conn = db.connect(cfg.database.path, key)
    try:
        current = db.schema_version(conn)
        target = migrate.target_version()
        if current is None or current != target:
            raise ServiceStartupError(
                _schema_mismatch_message(cfg.database.path, current, target)
            )
        if not db.reserved_category_present(conn):
            raise ServiceStartupError(
                f"database {cfg.database.path} is missing the reserved "
                "'not_assigned' category row (categories id 0, ADR-0055). "
                "This row must never be deleted; restore from a backup or "
                "re-seed it before starting the Core Service."
            )
        return current
    finally:
        db.close(conn)


def _schema_mismatch_message(
    database_path: Path, current: int | None, target: int | None
) -> str:
    if current is None or (target is not None and current < target):
        return (
            f"database {database_path} is at schema version {current or 0}, but "
            f"this build expects {target}. The Core Service never runs "
            "migrations; run 'healthspan db migrate' first, then start again."
        )
    return (
        f"database {database_path} is at schema version {current}, newer than "
        f"this build supports ({target}). Upgrade healthspan before starting."
    )


# --------------------------------------------------------------------------
# App assembly
# --------------------------------------------------------------------------


class RequestIDMiddleware:
    """Assign a fresh ``request_id`` per request (observability.md).

    A pure-ASGI middleware (not ``BaseHTTPMiddleware``) so the contextvar is
    bound in the same task the endpoint runs in and propagates to it. The ID
    is generated on receipt — never trusted from an inbound header — and
    echoed as ``X-Request-ID``.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = str(uuid4())
        structlog.contextvars.bind_contextvars(request_id=request_id)

        async def send_stamped(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_stamped)
        finally:
            structlog.contextvars.unbind_contextvars("request_id")


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None]:
    runtime = cast(ServiceRuntime, app.state.runtime)
    # ADR-0037: cap the worker threadpool the sync repository will run on.
    to_thread.current_default_thread_limiter().total_tokens = THREADPOOL_LIMIT
    runtime.started_monotonic = time.monotonic()
    runtime.ready = True
    _log.info(
        "core service ready",
        host=runtime.cfg.service.host,
        port=runtime.cfg.service.port,
    )
    try:
        yield
    finally:
        runtime.ready = False
        runtime.pool.close_all()  # before the key it was built on is zeroized
        runtime.lock.release()
        runtime.key.zeroize()
        _log.info("core service stopped")


def create_app(runtime: ServiceRuntime) -> FastAPI:
    """Assemble the Core Service ASGI app around a prepared runtime.

    Enforces the ADR-0026/0040 route-declaration rule: every route declares a
    scope or ``public``, and the only ``public`` route is liveness. OpenAPI
    and the docs UIs stay disabled through Phase 2 (ADR-0049 §7) — no
    unauthenticated API-surface disclosure.
    """
    app = FastAPI(
        title="Healthspan Core Service",
        version=_core_version(),
        lifespan=_lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.runtime = runtime
    app.state.liveness_limiter = LivenessRateLimiter()
    app.state.auth_limiter = AuthFailureRateLimiter(
        failure_threshold=runtime.cfg.auth.failure_threshold,
        max_backoff_seconds=float(runtime.cfg.auth.max_backoff_seconds),
    )
    app.state.metrics = MetricsCounters()
    app.add_middleware(MetricsMiddleware, counters=app.state.metrics)
    app.add_middleware(RequestIDMiddleware)
    app.include_router(health_router)
    app.include_router(metrics_router)
    app.include_router(tokens_router)
    app.include_router(import_router)
    app.include_router(read_router)

    public_paths = assert_all_routes_declared(app)
    if public_paths != [LIVENESS_PATH]:
        raise RuntimeError(
            "exactly one public route (liveness) is expected; "
            f"found public routes: {public_paths}"
        )
    return app


def _core_version() -> str:
    try:
        return _dist_version("healthspan")
    except Exception:  # pragma: no cover - packaging metadata always present
        return "0.0.0"


# --------------------------------------------------------------------------
# Direct-start entry
# --------------------------------------------------------------------------


def start_service(cfg: Config, passphrase_file_flag: Path | None = None) -> None:
    """Direct-start the Core Service in the foreground (blocks until shutdown)."""
    configure_logging(cfg.logging.level, PROCESS_CORE_SERVICE)
    runtime = build_runtime(cfg, passphrase_file_flag)
    try:
        bootstrap_tokens(runtime)
        app = create_app(runtime)
    except BaseException:
        runtime.pool.close_all()
        runtime.lock.release()
        runtime.key.zeroize()
        raise
    _run_uvicorn(app, cfg)


def bootstrap_tokens(runtime: ServiceRuntime) -> bool:
    """Mint the default token set on first start (ADR-0050 §1).

    Runs on the main thread before the app serves, over its own short-lived
    connection. The one-time MCP-secret printout goes to stderr — the
    operator's console under direct-start, never the stdout JSON log
    stream. Failure aborts startup with the table still empty, so the next
    start mints afresh.
    """
    conn = db.connect(runtime.cfg.database.path, runtime.key)
    try:
        minted = token_bootstrap.bootstrap_default_tokens(conn, _console)
    except (
        keychain.KeychainError,
        tokens_module.TokenError,
        sqlcipher3.Error,  # locked past busy_timeout, I/O error, corrupt page
        db.DatabaseError,
    ) as exc:
        raise ServiceStartupError(
            f"could not mint the default token set: {exc}"
        ) from exc
    finally:
        db.close(conn)
    if minted:
        _log.info("default token set minted (first start, ADR-0050)")
    return minted


def _console(line: str) -> None:
    print(line, file=sys.stderr)


def _run_uvicorn(app: FastAPI, cfg: Config) -> None:  # pragma: no cover - real socket
    # log_config=None: uvicorn does not install its own handlers, so its
    # loggers propagate to the structlog JSON root handler (one stream).
    uvicorn.run(
        app,
        host=cfg.service.host,
        port=cfg.service.port,
        log_config=None,
    )
