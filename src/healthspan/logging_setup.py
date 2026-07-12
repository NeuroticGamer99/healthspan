"""Structured JSON logging for platform processes (observability.md, ADR-0049).

``structlog`` renders every log entry as a single JSON object on stdout
carrying the required fields — ``timestamp`` (ISO-8601 UTC, millisecond
precision, ``Z``-suffixed), ``level``, ``process``, ``message``, and
``request_id`` (bound per request, ``null`` outside a request). Stdlib
logging is routed through the same formatter so uvicorn's records join the
one JSON stream instead of emitting a second, unstructured format.

Health data must never reach a log entry (security.md); this module shapes
the envelope, the log *call sites* keep values out, and the canary gate
(testing-strategy.md) mechanizes the prohibition.
"""

import logging
import sys
from datetime import UTC, datetime

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

PROCESS_CORE_SERVICE = "core_service"

# Bound per request by the request-ID middleware; merged into each entry.
_REQUEST_ID_KEY = "request_id"


def _timestamp(_logger: WrappedLogger, _name: str, event_dict: EventDict) -> EventDict:
    """ISO-8601 UTC, millisecond precision, ``Z``-suffixed (observability.md)."""
    now = datetime.now(UTC)
    event_dict["timestamp"] = f"{now:%Y-%m-%dT%H:%M:%S}.{now.microsecond // 1000:03d}Z"
    return event_dict


def _process_stamp(process: str) -> Processor:
    def stamp(_logger: WrappedLogger, _name: str, event_dict: EventDict) -> EventDict:
        event_dict["process"] = process
        return event_dict

    return stamp


def _ensure_request_id(
    _logger: WrappedLogger, _name: str, event_dict: EventDict
) -> EventDict:
    """Keep ``request_id`` a required field: ``null`` when no request is bound."""
    event_dict.setdefault(_REQUEST_ID_KEY, None)
    return event_dict


def _uppercase_level(
    _logger: WrappedLogger, _name: str, event_dict: EventDict
) -> EventDict:
    """Render level names uppercase (``INFO``), matching observability.md."""
    level = event_dict.get("level")
    if isinstance(level, str):
        event_dict["level"] = level.upper()
    return event_dict


def configure_logging(level: str = "INFO", process: str = PROCESS_CORE_SERVICE) -> None:
    """Install the JSON structlog pipeline as the sole stdout log stream.

    Idempotent: replaces the root handler set, so repeated calls (tests,
    re-entry) never stack duplicate handlers. ``level`` is one of the
    validated names from :mod:`healthspan.config`.
    """
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        _uppercase_level,
        _process_stamp(process),
        _timestamp,
        _ensure_request_id,
    ]

    structlog.configure(
        processors=[
            *shared,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        # Foreign (stdlib/uvicorn) records get the same envelope so the
        # stream is uniformly JSON — the message renamed last, matching the
        # structlog path.
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.EventRenamer("message"),
            structlog.processors.JSONRenderer(),
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.getLevelNamesMapping()[level])


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """A configured structlog logger. Call :func:`configure_logging` first."""
    return structlog.stdlib.get_logger(name)
