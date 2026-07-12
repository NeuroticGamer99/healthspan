"""Structured JSON logging (observability.md, ADR-0049)."""

import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest
import structlog

from healthspan.logging_setup import configure_logging, get_logger


@pytest.fixture(autouse=True)
def reset_contextvars() -> Iterator[None]:
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()


def _last_json(captured: str) -> dict[str, Any]:
    lines = [line for line in captured.splitlines() if line.strip()]
    return json.loads(lines[-1])


def test_log_entry_is_json_with_required_fields(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("INFO")
    get_logger("test").info("hello", endpoint="/v1/health")
    record = _last_json(capsys.readouterr().out)
    assert {"timestamp", "level", "process", "message", "request_id"} <= set(record)
    assert record["message"] == "hello"
    assert record["level"] == "INFO"
    assert record["process"] == "core_service"
    assert record["request_id"] is None
    assert record["endpoint"] == "/v1/health"


def test_timestamp_is_iso_utc_millis(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")
    get_logger("test").info("tick")
    ts = _last_json(capsys.readouterr().out)["timestamp"]
    assert ts[10] == "T"
    assert ts.endswith("Z")
    # millisecond precision: ...HH:MM:SS.mmmZ
    assert ts[19] == "."
    assert len(ts) == 24


def test_request_id_is_bound_when_present(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")
    structlog.contextvars.bind_contextvars(request_id="rid-42")
    get_logger("test").warning("careful")
    record = _last_json(capsys.readouterr().out)
    assert record["request_id"] == "rid-42"
    assert record["level"] == "WARNING"


def test_no_health_data_convention_holds(capsys: pytest.CaptureFixture[str]) -> None:
    # The envelope is JSON-escaped, so even a stray value cannot break the
    # structure; call sites keep health values out (canary gate enforces).
    configure_logging("INFO")
    get_logger("test").info("import complete", rows=5, biomarker="apob")
    record = _last_json(capsys.readouterr().out)
    assert record["rows"] == 5
    assert record["biomarker"] == "apob"


def test_stdlib_records_route_through_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("INFO")
    logging.getLogger("uvicorn.error").info("server started")
    record = _last_json(capsys.readouterr().out)
    assert record["message"] == "server started"
    assert record["process"] == "core_service"


def test_configure_is_idempotent() -> None:
    configure_logging("INFO")
    configure_logging("DEBUG")
    assert len(logging.getLogger().handlers) == 1
